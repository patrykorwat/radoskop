#!/usr/bin/env python3
"""Radoskop Rabka-Zdrój — madopolska BIP "Protokoły głosowań" (imienne).

Source: https://bip.malopolska.pl  (Madkom SPA, entity `grabkazdroj`)
  Rada Miejska -> Protokoły głosowań (menu 312388 / target 435880), single article
  "Protokoły głosowań podczas sesji Rady Miejskiej w Rabce-Zdroju IX kadencji (2024-2029)"
  (article 2460480) lists per-session "Protokół z głosowania <ROM> sesja IX kadencji <date> r."
  each linking a PDF (`/e,pobierz,get.html?id=N`).

  Each PDF = clean text (no OCR), per-vote block:
    GŁOSOWANIE
    <n>.<n> Głosowanie <topic>
    TYP GŁOSOWANIA Jawne DATA GŁOSOWANIA YYYY-MM-DD HH:MM:SS
    LICZBA UPRAWNIONYCH 15 GŁOSY ZA 12
    LICZBA OBECNYCH 12 GŁOSY PRZECIW 0
    LICZBA NIEOBECNYCH 3 GŁOSY WSTRZYMUJĄCE SIĘ 0
    GŁOSY NIEODDANE 0
    KWORUM ZOSTAŁO OSIĄGNIĘTE
    UPRAWNIENI DO GŁOSOWANIA
    LP NAZWISKO I IMIĘ GŁOS
    1 Ciepliński Marek NIEOBECNY
    ...
  GŁOS ∈ {ZA, PRZECIW, WSTRZYMUJĄCE SIĘ, NIEODDANE, NIEOBECNY}.
  Session date/roman from the article list (authoritative).

Outputs (relative to CITY_DIR/docs): kadencja-2024-2029.json, profiles.json, data.json
"""
import os, re, json, sys, time, unicodedata
import requests
import pdfplumber
from collections import defaultdict, Counter
from pathlib import Path

UA = {'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) Chrome/120 Radoskop/1.0'}
BASE = 'https://bip.malopolska.pl'
API = BASE + '/api'
ENTITY = 'grabkazdroj'
VOTES_ARTICLE = '2460480'
KAD_START = '2024-05-01'

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[3] / "scripts"))


def _norm(s):
    s = s.lower()
    for a, b in [('ą','a'),('ć','c'),('ę','e'),('ł','l'),('ń','n'),('ó','o'),('ś','s'),('ź','z'),('ż','z')]:
        s = s.replace(a, b)
    return s


def _roman_to_int(s):
    vals = {'I':1,'V':5,'X':10,'L':50,'C':100,'D':500,'M':1000}
    tot, prev = 0, 0
    for ch in reversed(s.upper()):
        v = vals.get(ch, 0)
        if v < prev: tot -= v
        else: tot += v
        prev = v
    return tot


def slugify(s):
    return re.sub(r'[^a-z0-9]+', '-', _norm(s)).strip('-')


def _get(url, as_json=True):
    r = requests.get(url, headers=UA, timeout=30)
    if not as_json:
        return r
    return r.json()


def fetch_sessions():
    """Return list of {roman, n, date, url, id} from the article content."""
    a = _get(f'{API}/articles/{VOTES_ARTICLE}')
    c = a.get('content') or ''
    c = c.replace('&oacute;','ó').replace('&#322;','ł').replace('&nbsp;',' ')
    # scan document order: each "Protokół z głosowania <ROM> sesja IX kadencji <date> r."
    # is immediately followed (after '- pobierz') by its download href (get.html or /api/files).
    pat = re.compile(r'Protokół z głosowania\s+([IVXLCDM]+)\s+sesja IX kadencji\s+([0-9.]+)\s*r\.\s*-\s*<a[^>]*href=["\']([^"\']+)["\']')
    out = []
    for m in pat.finditer(c):
        rom, datestr, href = m.group(1), m.group(2), m.group(3)
        d, mo, y = datestr.split('.')
        iso = f"{y}-{mo}-{d}"
        if '/api/files/' in href:
            lid = href.split('/api/files/')[-1]
        else:
            lid = href.rsplit('=', 1)[-1]
        out.append({'roman': rom, 'n': _roman_to_int(rom), 'date': iso,
                    'id': lid, 'url': href if href.startswith('http') else BASE + href})
    return out


def download_pdf(url, cache_dir, key):
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, f"{key}.pdf")
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        r = requests.get(url, headers=UA, timeout=60)
        open(path, 'wb').write(r.content)
        time.sleep(0.15)
    return path


VOTE_SPLIT = re.compile(r'^\s*GŁOSOWANIE\s*$', re.M)


def parse_pdf(path):
    """Extract votes from ONE session protokół PDF. Returns list of vote dicts."""
    with pdfplumber.open(path) as pdf:
        text = '\n'.join((p.extract_text() or '') for p in pdf.pages)
    lines = [l.rstrip() for l in text.split('\n')]
    # find vote blocks: each starts at a "GŁOSOWANIE" line; next vote / EOF ends it
    starts = [i for i, l in enumerate(lines) if VOTE_SPLIT.match(l)]
    votes = []
    for k, si in enumerate(starts):
        ei = starts[k+1] if k+1 < len(starts) else len(lines)
        block = lines[si+1:ei]
        # topic: first "N.N Głosowanie ..." line
        topic = ''
        owned = False
        data = {}
        counts = {'za':0,'przeciw':0,'wstrzymal_sie':0,'brak_glosu':0,'nieobecni':0}
        roster = []  # (name, glos)
        in_table = False
        for l in block:
            mt = re.match(r'^\d+(?:\.\d+)?\s+Głosowanie\s+(.+)$', l)
            if mt and not owned:
                topic = mt.group(1).strip(); owned = True
            mD = re.search(r'DATA GŁOSOWANIA\s+(\d{4}-\d\d-\d\d)\s+(\d\d:\d\d:\d\d)', l)
            if mD:
                data['voted_at'] = f"{mD.group(1)} {mD.group(2)}"
            mZa = re.search(r'LICZBA UPRAWNIONYCH\s+(\d+)', l); 
            if mZa: counts['uprawnieni'] = int(mZa.group(1))
            mza = re.search(r'GŁOSY ZA\s+(\d+)', l)
            if mza: counts['za'] = int(mza.group(1))
            mpr = re.search(r'GŁOSY PRZECIW\s+(\d+)', l)
            if mpr: counts['przeciw'] = int(mpr.group(1))
            mwz = re.search(r'GŁOSY WSTRZYMUJĄCE SIĘ\s+(\d+)', l)
            if mwz: counts['wstrzymal_sie'] = int(mwz.group(1))
            mni = re.search(r'LICZBA NIEOBECNYCH\s+(\d+)', l)
            if mni: counts['nieobecni'] = int(mni.group(1))
            mng = re.search(r'GŁOSY NIEODDANE\s+(\d+)', l)
            if mng: counts['brak_glosu'] = int(mng.group(1))
            if re.match(r'^\s*UPRAWNIENI DO GŁOSOWANIA', l):
                in_table = True; continue
            if re.match(r'^\s*LP\s+NAZWISKO I IMIĘ\s+GŁOS', l):
                continue
            if in_table:
                mrow = re.match(r'^\s*(\d{1,2})\.?\s+([A-ZĄĆĘŁŃÓŚŹŻa-ząćęłńóśźż\-]+(?:\s+[A-ZĄĆĘŁŃÓŚŹŻa-ząćęłńóśźż\-]+)+?)\s+(ZA|PRZECIW|WSTRZYMUJĄCE SIĘ|WSTRZYMUJĄCY SIĘ|NIEODDANE|NIEODDANY|NIEOBECNY|NIEOBECNA|NIE OBECNY)$', l)
                if mrow:
                    roster.append((mrow.group(2).strip(), mrow.group(3)))
        if not topic or not roster:
            continue
        votes.append({'topic': topic, 'counts': counts, 'roster': roster,
                      'voted_at': data.get('voted_at','')})
    return votes


def scrape(cache_dir):
    sessions = [s for s in fetch_sessions() if s.get('date') and s['date'] >= KAD_START and s.get('url')]
    sessions.sort(key=lambda s: s['n'])
    print(f"[rabka-zdroj] {len(sessions)} IX-kadencja sessions", flush=True)
    all_votes = []
    for s in sessions:
        path = download_pdf(s['url'], os.path.join(cache_dir, 'pdf'), s['id'] or s['n'])
        try:
            svotes = parse_pdf(path)
        except Exception as e:
            print(f"  {s['roman']} parse ERR {e}", flush=True); continue
        for v in svotes:
            all_votes.append({'session_roman': s['roman'], 'session_n': s['n'],
                              'date': s['date'], 'topic': v['topic'],
                              'counts': v['counts'], 'roster': v['roster'],
                              'source_url': s['url']})
        print(f"  {s['roman']} (n{s['n']}) {s['date']}: {len(svotes)} votes ({len(all_votes)} tot)", flush=True)
    return all_votes


def _classify(nv, group, key):
    return [name for (name, g) in nv['roster'] if g == group]


def build_files(votes, city_dir, scraped_at):
    cfg = json.loads((Path(city_dir) / 'config.json').read_text(encoding='utf-8'))
    roster_ordered = [n for n in cfg.get('councilor_roster', [])]
    seen = set()
    for v in votes:
        for name, _g in v['roster']:
            if name not in seen:
                seen.add(name)
                if name not in roster_ordered:
                    roster_ordered.append(name)
    roster = sorted(roster_ordered)

    ca = defaultdict(Counter)
    for v in votes:
        for name, g in v['roster']:
            if g == 'ZA': ca[name]['za'] += 1
            elif g in ('PRZECIW',): ca[name]['przeciw'] += 1
            elif 'WSTRZYMUJ' in g: ca[name]['wstrzymal'] += 1
            elif 'NIEODD' in g: ca[name]['brak'] += 1
            else: ca[name]['nieobecny'] += 1

    s_map = defaultdict(list)
    for v in votes:
        s_map[v['session_n']].append(v)
    sessions = []
    for n in sorted(s_map.keys()):
        sv = s_map[n]
        roman = sv[0]['session_roman']; date = sv[0]['date']
        att = []
        for v in sv:
            for name, g in v['roster']:
                if 'NIEOBECN' not in g:
                    att.append(name)
        att = list(dict.fromkeys(att))
        sessions.append({'date': date, 'number': roman, 'vote_count': len(sv),
                         'attendee_count': len(att), 'attendees': att, 'speakers': []})

    def stats(n):
        c = ca[n]; za=c['za']; pr=c['przeciw']; wz=c['wstrzymal']; br=c['brak']; nb=c['nieobecny']
        total = za+pr+wz+br+nb
        frek = round(100.0*(total-nb)/total,1) if total else 0.0
        akt = round(100.0*(za+pr+wz)/total,1) if total else 0.0
        return za, pr, wz, br, nb, total, frek, akt

    councilors = []
    for n in roster:
        za, pr, wz, br, nb, total, frek, akt = stats(n)
        councilors.append({'name': n, 'club': 'NZ', 'district': None, 'frekwencja': frek, 'aktywnosc': akt,
            'zgodnosc_z_klubem': 0.0, 'votes_za': za, 'votes_przeciw': pr, 'votes_wstrzymal': wz,
            'votes_brak': br, 'votes_nieobecny': nb, 'votes_total': total, 'rebellion_count': 0,
            'rebellions': [], 'has_activity_data': False, 'activity': None})

    kad = {'id': '2024-2029', 'label': 'IX kadencja (2024–2029)', 'clubs': {}, 'sessions': sessions,
           'total_sessions': len(sessions), 'total_votes': len(votes), 'total_councilors': len(councilors),
           'councilors': councilors, 'votes': [], 'similarity_top': [], 'similarity_bottom': []}
    for vi, v in enumerate(votes):
        nv = {'za': _classify(v, 'ZA', 'za'),
              'przeciw': _classify(v, 'PRZECIW', 'przeciw'),
              'wstrzymal_sie': _classify(v, 'WSTRZYMUJĄCE SIĘ', 'wstrzymal_sie') + _classify(v, 'WSTRZYMUJĄCY SIĘ','wstrzymal_sie')}
        counts = {'za': v['counts'].get('za',0), 'przeciw': v['counts'].get('przeciw',0),
                  'wstrzymal_sie': v['counts'].get('wstrzymal_sie',0),
                  'brak_glosu': v['counts'].get('brak_glosu',0), 'nieobecni': v['counts'].get('nieobecni',0)}
        kad['votes'].append({'id': str(vi), 'source_url': v['source_url'],
                             'session_date': v['date'], 'session_number': v['session_roman'],
                             'topic': v['topic'], 'druk': '', 'resolution': '',
                             'counts': {'za': counts['za'], 'przeciw': counts['przeciw'],
                                        'wstrzymal_sie': counts['wstrzymal_sie']},
                             'named_votes': nv})

    profiles = []
    for n in roster:
        za, pr, wz, br, nb, total, frek, akt = stats(n)
        profiles.append({'name': n, 'slug': slugify(n), 'kadencje': {
            '2024-2029': {'club': 'NZ', 'has_voting_data': True, 'has_activity_data': False,
                          'frekwencja': frek, 'aktywnosc': akt, 'zgodnosc_z_klubem': 0.0,
                          'votes_za': za, 'votes_przeciw': pr, 'votes_wstrzymal': wz, 'votes_brak': br,
                          'votes_nieobecny': nb, 'votes_total': total, 'rebellion_count': 0,
                          'rebellions': [], 'roles': [], 'notes': ''}}})

    docs = os.path.join(city_dir, 'docs')
    os.makedirs(docs, exist_ok=True)
    json.dump(kad, open(os.path.join(docs, 'kadencja-2024-2029.json'), 'w'), ensure_ascii=False)
    json.dump({'profiles': profiles, 'total': len(profiles)}, open(os.path.join(docs, 'profiles.json'), 'w'), ensure_ascii=False)
    json.dump({'generated': scraped_at, 'default_kadencja': '2024-2029',
               'kadencje': [{'id': '2024-2029', 'label': 'IX kadencja (2024–2029)'}]},
              open(os.path.join(docs, 'data.json'), 'w'), ensure_ascii=False)
    return kad


def main():
    city_dir = os.getcwd()
    cache_dir = os.environ.get('RADOSKOP_CACHE_DIR', '/cache/rabka-zdroj')
    args = sys.argv[1:]
    if '--city-dir' in args:
        city_dir = args[args.index('--city-dir')+1]
    if '--cache-dir' in args:
        cache_dir = args[args.index('--cache-dir')+1]
    os.makedirs(cache_dir, exist_ok=True)
    scraped_at = __import__('datetime').datetime.now().astimezone().isoformat()
    votes = scrape(cache_dir)
    print(f"[rabka-zdroj] {len(votes)} votes total", flush=True)
    kad = build_files(votes, city_dir, scraped_at)
    print(f"[rabka-zdroj] sessions={kad['total_sessions']} votes={kad['total_votes']} councilors={kad['total_councilors']}")


if __name__ == '__main__':
    main()
