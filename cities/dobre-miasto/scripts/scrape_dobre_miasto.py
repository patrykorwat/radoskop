#!/usr/bin/env python3
"""Radoskop Dobre Miasto scraper — clean text-based "Imienne głosowanie" PDFs.

Source: https://bip.dobremiasto.com.pl  (custom BIP, /system/pobierz.php?id=NNN)
  Rada Miejska -> Imienne wyniki głosowania radnych -> Kadencja 2024-2029 (/10116/)
  Each session article (I..XXIX) links per-uchwała "Imienne głosowanie nad Uchwałą Nr X/Y/Z"
  PDF = clean text (one page) with:
     <NN> <ROM> sesja Rady Miejskiej w Dobrym Mieście
     Głosowanie
     <topic>
     Typ głosowania jawne Data głosowania: DD.MM.YYYY HH:MM
     Liczba uprawnionych N  ... Głosy za N / przeciw N / wstrzymujące się N / nieobecni N
     Uprawnieni do głosowania
     Lp Nazwisko i imię Głos   (two-column layout: col A rows 1..8, col B rows 9..15)
   Zupełnie clean (no OCR needed). Validation per vote: counts == aggregate.

Outputs (relative to CITY_DIR/docs): kadencja-2024-2029.json, profiles.json, data.json
"""
import os, re, json, sys, time, hashlib, unicodedata
import requests
import pdfplumber
from collections import defaultdict, Counter

UA = {'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) Radoskop/1.0'}
BASE = 'https://bip.dobremiasto.com.pl'
KAD_CAT = BASE + '/10116/Kadencja_2024-2029/'
SESSION_RE = re.compile(r'/10116/(\d+)/([A-Z]+)_sesja_Rady_Miejskiej_w_Dobrym_Miescie/')
VOTE_RE = re.compile(r'/system/pobierz\.php\?id=([0-9a-fA-F]+)')
VOTE_TITLE_RE = re.compile(r'Imienne\s+g\w*osowanie nad Uchwa\w*\s+Nr\s+(\S+)', re.I)
AGG_UP = re.compile(r'Liczba uprawnionych\s+(\d+)')
AGG_OB = re.compile(r'Liczba obecnych\s+(\d+)')
AGG_NB = re.compile(r'Liczba nieobecnych\s+(\d+)')
AGG_ZA = re.compile(r'G\w*osy za\s+(\d+)', re.I)
AGG_PR = re.compile(r'G\w*osy przeciw\s+(\d+)', re.I)
AGG_WZ = re.compile(r'G\w*osy wstrzymuj\w*ce si\w*\s+(\d+)', re.I)
DATA_RE = re.compile(r'Data g\w*osowania:\s*(\d{2})\.(\d{2})\.(\d{4})')
VOTE_LINE_RE = re.compile(r'^\s*(\d{1,2})\.\s+(.+?)\s+(ZA|PRZECIW|WSTRZYMUJ\w* SI\w*|NIEOBECNY|BRAK G\w*OSU|BRAK\s+G\w*OSU|NIE G\w*OSOWA\w*)\s*$', re.I)


def _norm(s):
    s = s.lower()
    for a, b in [('ą','a'),('ć','c'),('ę','e'),('ł','l'),('ń','n'),('ó','o'),('ś','s'),('ź','z'),('ż','z')]:
        s = s.replace(a, b)
    return s


def _roman_to_int(s):
    vals = {'I':1,'V':5,'X':10,'L':50,'C':100,'D':500,'M':1000}
    tot = 0
    prev = 0
    for ch in reversed(s.upper()):
        v = vals.get(ch, 0)
        if v < prev: tot -= v
        else: tot += v
        prev = v
    return tot


def slugify(s):
    sl = _norm(s)
    return re.sub(r'[^a-z0-9]+', '-', sl).strip('-')


def fetch_session_articles(session):
    """Return list of (article_url, roman, id) for all sessions in kad 2024-2029 (incl. archive pages)."""
    out = {}
    # main page + archive pagination page 2,3 (page 1 of archive redirects to main)
    urls = [KAD_CAT,
            BASE+'/10116/Kadencja_2024-2029/2/',
            BASE+'/10116/Kadencja_2024-2029/3/']
    for u in urls:
        try:
            b = requests.get(u, headers=UA, timeout=30).text
        except Exception:
            continue
        for m in set(re.findall(r'href="([^"]*10116/\d+/[A-Z]+_sesja_[\w]*_w_Dobrym_Miescie/)"', b)):
            mid = SESSION_RE.search(m)
            if not mid: continue
            art_id, rom = mid.group(1), mid.group(2)
            if art_id not in out:
                out[art_id] = {'url': m if m.startswith('http') else BASE+m, 'roman': rom, 'n': _roman_to_int(rom)}
    return sorted(out.values(), key=lambda x: x['n'])


def parse_pdf(page):
    """Parse ONE imienne vote from an extracted table -> dict or None."""
    rows = page.extract_table()
    if not rows:
        return None
    flat = None  # kept unused
    text = '\n'.join(' '.join((cell or '').split()) for row in rows for cell in row)
    data = {}
    m = DATA_RE.search(text)
    if m:
        data['date'] = f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    def num(kw):
        for r in rows:
            joined = ' '.join((c or '') for c in r)
            mm = re.search(kw + r'\s+(\d+)', joined, re.I)
            if mm: return int(mm.group(1))
        return None
    data['uprawnionych'] = num(r'Liczba uprawnionych')
    data['za'] = num(r'G\w*osy za')
    data['przeciw'] = num(r'G\w*osy przeciw')
    data['wstrzymal'] = num(r'G\w*osy wstrzymuj\w*ce si\w*')
    data['nieobecni'] = num(r'Liczba nieobecnych')
    # topic: find row "Głosowanie" then next non-empty text row
    topic = ''
    for i, r in enumerate(rows):
        if ' '.join((c or '') for c in r).strip() == 'Głosowanie':
            for r2 in rows[i+1:]:
                cand = ' '.join((c or '') for c in r2).strip()
                if cand and not re.match(r'^(Typ|Liczba|Obecni|Kworum|Uprawnieni|Lp)', cand) \
                   and not re.match(r'^\d{2}\.\d{2}\.', cand):
                    topic = cand; break
            break
    # votes: rows after 'Uprawnieni do głosowania'
    votes = []
    started = False
    for r in rows:
        joined = ' '.join((c or '') for c in r)
        if 'Uprawnieni do głosowania' in joined:
            started = True; continue
        if not started: continue
        # row like ['1.', 'Mariusz Borek', None, 'ZA', '9.', 'Marcin Lisowski', 'NIEOBECNY']
        lp, name, glos = (r[0] or '').strip(), (r[1] or '').strip(), (r[3] or '').strip()
        if lp and name and glos and name not in ('Nazwisko i imię', 'Nazwisko i imie'):
            votes.append((name, glos))
        lp2, name2, glos2 = (r[4] or '').strip(), (r[5] or '').strip(), (r[6] or '').strip()
        if lp2 and name2 and glos2 and name2 not in ('Nazwisko i imię', 'Nazwisko i imie'):
            votes.append((name2, glos2))
    if not votes:
        return None
    data['votes'] = votes
    data['topic'] = topic
    return data


def validate(v):
    if not v or 'za' not in v or not v['votes']:
        return False
    counts = Counter()
    for name, vote in v['votes']:
        vv = vote.upper()
        if 'PRZECIW' in vv: counts['przeciw'] += 1
        elif 'WSTRZYMUJ' in vv: counts['wstrzymal'] += 1
        elif 'NIEOBECN' in vv: counts['nieobecny'] += 1
        elif 'ZA' in vv: counts['za'] += 1
        elif 'BRAK' in vv: counts['brak'] += 1
        else: counts['other'] += 1
    exp_za, exp_pr, exp_wz = v.get('za', 0), v.get('przeciw', 0), v.get('wstrzymal', 0)
    return counts['za'] == exp_za and counts['przeciw'] == exp_pr and counts['wstrzymal'] == exp_wz


def download_pdf(url, cache_dir, cache_key):
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, f"{cache_key}.pdf")
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        r = requests.get(url, headers=UA, timeout=60)
        open(path, 'wb').write(r.content)
        time.sleep(0.25)
    return path


def scrape(cache_dir):
    sessions = fetch_session_articles(None)
    print(f"[dobre-miasto] {len(sessions)} session articles", flush=True)
    all_votes = []
    for s in sessions:
        try:
            b = requests.get(s['url'], headers=UA, timeout=30).text
        except Exception as e:
            print(f"  session {s['n']}/{s['roman']} fetch ERR {e}", flush=True)
            continue
        vote_links = set(re.findall(r'href="([^"]*(?:/system/pobierz\.php\?id=)[0-9a-fA-F]+)"[^>]*>(.*?)</a>', b, re.S))
        svotes = []
        for href, atext in vote_links:
            if not re.search(r'[Ii]mienne\s+g\w*osowanie', atext, re.I) and not re.search(r'[Uu]chwa', atext, re.I):
                continue
            mid = VOTE_RE.search(href)
            if not mid: continue
            vid = mid.group(1)
            path = download_pdf(href if href.startswith('http') else BASE + href, os.path.join(cache_dir, 'pdf'), vid)
            try:
                with pdfplumber.open(path) as pdf:
                    d = None
                    for pg in pdf.pages:
                        d = parse_pdf(pg)
                        if d: break
            except Exception as e:
                print(f"  {s['roman']} vote {vid} pdf ERR {e}", flush=True)
                continue
            if not d:
                continue
            if not validate(d):
                print(f"  {s['roman']} vote {vid} NOT VALIDATED (agg za/pr/wz {d.get('za')}/{d.get('przeciw')}/{d.get('wstrzymal')} vs rows)", flush=True)
                continue
            mt = VOTE_TITLE_RE.search(atext)
            druk = mt.group(1) if mt else ''
            svotes.append({
                'date': d['date'], 'session_roman': s['roman'], 'session_n': s['n'],
                'topic': d['topic'] or (atext if 'uchwa' in atext.lower() else druk),
                'druk': druk, 'za': d['za'], 'przeciw': d['przeciw'], 'wstrzymal': d['wstrzymal'],
                'votes': d['votes'], 'source_url': BASE + href,
            })
        all_votes.extend(svotes)
        print(f"  {s['roman']} (n{s['n']}): {len(svotes)} validated votes", flush=True)
    return all_votes


def build_files(votes, city_dir, scraped_at):
    votes.sort(key=lambda v: (v['date'] or '', v['session_n'], v['druk']))
    # roster: from union of names, ordered deterministically
    names = set()
    for v in votes:
        for name, vote in v['votes']:
            names.add(name)
    # order by first appearance
    roster = []
    seen = set()
    for v in votes:
        for name, vote in v['votes']:
            if name not in seen:
                seen.add(name); roster.append(name)
    roster.sort()
    ca = defaultdict(Counter)
    for v in votes:
        for name, vote in v['votes']:
            vv = vote.upper()
            if 'PRZECIW' in vv: ca[name]['przeciw'] += 1
            elif 'WSTRZYMUJ' in vv: ca[name]['wstrzymal'] += 1
            elif 'NIEOBECNY' in vv: ca[name]['nieobecny'] += 1
            elif 'ZA' in vv: ca[name]['za'] += 1
            elif 'BRAK' in vv: ca[name]['brak'] += 1
    sessions_map = defaultdict(list)
    for v in votes:
        sessions_map[v['session_n']].append(v)
    sessions = []
    for n in sorted(sessions_map.keys()):
        sv = sessions_map[n]
        roman = sv[0]['session_roman']
        date = sv[0]['date']
        att = []
        for v in sv:
            for name, vote in v['votes']:
                if 'NIEOBECNY' not in vote.upper():
                    att.append(name)
        att = list(dict.fromkeys(att))
        sessions.append({'date': date, 'number': roman, 'vote_count': len(sv),
                         'attendee_count': len(att), 'attendees': att, 'speakers': []})
    councilors = []
    for n in roster:
        c = ca[n]
        za=c.get('za',0); pr=c.get('przeciw',0); wz=c.get('wstrzymal',0)
        br=c.get('brak',0); nb=c.get('nieobecny',0)
        total=za+pr+wz+br+nb
        frek=round(100.0*(total-nb)/total,1) if total else 0.0
        akt=round(100.0*(za+pr+wz)/total,1) if total else 0.0
        councilors.append({'name': n, 'club': 'NZ', 'district': None, 'frekwencja': frek, 'aktywnosc': akt,
            'zgodnosc_z_klubem': 0.0, 'votes_za': za, 'votes_przeciw': pr, 'votes_wstrzymal': wz,
            'votes_brak': br, 'votes_nieobecny': nb, 'votes_total': total, 'rebellion_count': 0,
            'rebellions': [], 'has_activity_data': False, 'activity': None})
    kad = {'id': '2024-2029', 'label': 'IX kadencja (2024–2029)', 'clubs': {}, 'sessions': sessions,
           'total_sessions': len(sessions), 'total_votes': len(votes), 'total_councilors': len(councilors),
           'councilors': councilors, 'votes': [], 'similarity_top': [], 'similarity_bottom': []}
    for vi, v in enumerate(votes):
        nv = {'za': [], 'przeciw': [], 'wstrzymal_sie': []}
        for name, vote in v['votes']:
            vv = vote.upper()
            if 'PRZECIW' in vv: nv['przeciw'].append(name)
            elif 'WSTRZYMUJ' in vv: nv['wstrzymal_sie'].append(name)
            elif 'ZA' in vv: nv['za'].append(name)
        kad['votes'].append({'id': str(vi), 'source_url': v['source_url'],
                             'session_date': v['date'], 'session_number': v['session_roman'],
                             'topic': v['topic'], 'druk': v['druk'], 'resolution': '',
                             'counts': {'za': v['za'], 'przeciw': v['przeciw'], 'wstrzymal_sie': v['wstrzymal']},
                             'named_votes': nv})
    profiles = []
    for n in roster:
        c = ca[n]
        za=c.get('za',0); pr=c.get('przeciw',0); wz=c.get('wstrzymal',0)
        br=c.get('brak',0); nb=c.get('nieobecny',0)
        total=za+pr+wz+br+nb
        frek=round(100.0*(total-nb)/total,1) if total else 0.0
        akt=round(100.0*(za+pr+wz)/total,1) if total else 0.0
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
    cache_dir = os.environ.get('RADOSKOP_CACHE_DIR', '/cache/dobre-miasto')
    args = sys.argv[1:]
    if '--city-dir' in args:
        city_dir = args[args.index('--city-dir')+1]
    if '--cache-dir' in args:
        cache_dir = args[args.index('--cache-dir')+1]
    os.makedirs(cache_dir, exist_ok=True)
    scraped_at = __import__('datetime').datetime.now().astimezone().isoformat()
    votes = scrape(cache_dir)
    print(f"[dobre-miasto] {len(votes)} validated votes total", flush=True)
    kad = build_files(votes, city_dir, scraped_at)
    print(f"[dobre-miasto] sessions={kad['total_sessions']} votes={kad['total_votes']} councilors={kad['total_councilors']}")


if __name__ == '__main__':
    main()
