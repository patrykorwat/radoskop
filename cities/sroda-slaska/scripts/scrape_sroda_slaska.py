#!/usr/bin/env python3
"""Radoskop Środa Śląska — custom BIP "Imienne wykazy głosowań radnych".

Source: https://bip.srodaslaska.pl  (MR/"bipv45" BIP)
  Rada Miejska -> Imienne wykazy głosowań radnych (id=176)
  The vote list is a jQuery DataTables server-side source:
    index.php?id=176&akcja=pobierz_dokumenty_ajax&chwila=1 -> JSON aaData rows:
      [0]=0, [1]=osoba, [2]=title "wykazy głosowań z <ROM> Sesji ... z dnia <D> roku",
      [5]=publish-date, [9]=document id (aData[9])
  Each document detail page: index.php?id=176&p1=szczegoly&p2={aData[9]} -> "Plik źródłowy (pdf)"
    link (upload/pliki/...).pdf = eSesja PRINT text format (Głosowano w sprawie / Wyniki imienne
    / ZA (n) names / PRZECIW / WSTRZYMUJĘ SIĘ / BRAK GŁOSU / NIEOBECNI) -> parsed by
    lib_voting_pdf_table.parse_voting_pdf(). IX kadencja starts 2024-05-07.

Outputs (relative to CITY_DIR/docs): kadencja-2024-2029.json, profiles.json, data.json
"""
import os, re, json, sys, time, unicodedata
import requests
from collections import defaultdict, Counter
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[3] / "scripts"))
from lib_voting_pdf_table import extract_pdf_text, parse_voting_text

# Names / headers that sneak into named lists and are NOT people (headers, footers).
STOP = [
    'uczestnictwo w głosowaniach', 'uczestnictwo', 'wyniki głosowania', 'wyniki imienne',
    'przeprowadzone głosowania', 'rada miejska', 'raport z głosowań', 'sesja w dniu',
    'sesja rady miejskiej', 'radni', 'glosowano', 'głosowano', 'imienne wykazy',
    'imienny wykaz', 'wykaz głosowań', 'sesji rady', 'w dniu', 'za (', 'przeciw (',
    'przewodniczący rady', 'przewodniczaca', 'burmistrz', 'lista obecności', 'lista obecnosci',
    'obecni', 'nieobecni', 'scanned', 'wygenerowano', 'zobacz', 'strona',
]
def _is_name(n):
    nn = n.strip()
    if not nn or len(nn) < 3:
        return False
    low = nn.lower()
    for s in STOP:
        if s in low:
            return False
    # must look like a person: at least one token with an uppercase initial
    toks = [t for t in re.split(r'[\s-]+', nn) if t]
    upper = [t for t in toks if t and t[0].isupper()]
    if not upper:
        return False
    return True


def _norm_name(n):
    # fix hyphen-line-break splits: "Wojtasińska- Żygadło" / "Wojtasińska-Żygadło" -> "Wojtasińska-Żygadło"
    n = re.sub(r'-\s+', '-', n)
    n = re.sub(r'-\s*$', '', n).strip()
    return n


def _extract_robust(path):
    """Text extraction with colon-less format it doesn't need; returns (full, first)."""
    full, first = extract_pdf_text(path, ocr_fallback=False)
    parts = full.split('\n')
    if len(full.strip()) < 100:
        # scanned PDF -> OCR via pdfplumber render + tesseract
        import subprocess
        import pdfplumber
        parts = []
        first_page = ''
        with pdfplumber.open(str(path)) as pdf:
            for i, page in enumerate(pdf.pages):
                png = f"{path}.p{i}.png"
                page.to_image(resolution=150).save(png)
                out = subprocess.run(['tesseract', png, '-', '-l', 'pol'],
                                     capture_output=True, text=True)
                txt = out.stdout or ''
                if i == 0:
                    first_page = txt
                parts.append(txt)
                os.remove(png)
        full = '\n'.join(parts)
        first = first_page
    # normalize "Głosowano w sprawie" (no colon) -> "Głosowano w sprawie:" so the shared
    # parse_voting_text split regex matches (early eSesja format lacks the colon).
    full = re.sub(r'(G[łl]osowano(?:[ ]+wniosek)?[ ]+w sprawie)(?!\s*:)', r'\1:', full)
    return full, first

UA = {'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) Radoskop/1.0',
      'X-Requested-With': 'XMLHttpRequest'}
BASE = 'https://bip.srodaslaska.pl'
LIST_URL = BASE + '/index.php?id=176&akcja=pobierz_dokumenty_ajax&chwila=1'
DETAIL = BASE + '/index.php?id=176&p1=szczegoly&p2={id9}'
FONT_ROMAN = re.compile(r'wykazy\s+g[łl]osowa[ńn] z ([IVXLCDM]+) Sesji Rady Miejskiej')
DATE_FROM_TITLE = re.compile(r'z dnia (\d{1,2})\s+(\S+)\s+(\d{4})\s+roku')
PDF_SRC = re.compile(r'<a[^>]+href=["\']([^"\']+\.pdf)[^"\']*["\'][^>]*>(.*?)</a>', re.I)
KAD_START = '2024-05-01'


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
    sl = _norm(s)
    return re.sub(r'[^a-z0-9]+', '-', sl).strip('-')


_MONTHS = {1:'stycznia',2:'lutego',3:'marca',4:'kwietnia',5:'maja',6:'czerwca',
           7:'lipca',8:'sierpnia',9:'września',10:'października',11:'listopada',12:'grudnia'}
def _title_date(s):
    m = DATE_FROM_TITLE.search(s)
    if not m: return None
    day, month, year = int(m.group(1)), m.group(2), m.group(3)
    for k, v in _MONTHS.items():
        if v.startswith(_norm(month)[:4]) or _norm(month).startswith(_norm(v)[:4]):
            return f"{year}-{k:02d}-{day:02d}"
    return None


def fetch_records():
    r = requests.get(LIST_URL, headers=UA, timeout=40)
    data = r.json()
    recs = []
    for row in data['aaData']:
        title = row[2] or ''
        date = row[5]  # publish date
        id9 = row[9]
        m = FONT_ROMAN.search(title)
        roman = m.group(1) if m else None
        sess_date = _title_date(title) or date
        recs.append({'title': title, 'roman': roman, 'n': _roman_to_int(roman) if roman else 0,
                     'pub_date': date, 'session_date': sess_date, 'id9': id9})
    return recs


def get_pdf_url(id9):
    r = requests.get(DETAIL.format(id9=id9), headers={'User-Agent': UA['User-Agent']}, timeout=40)
    r.encoding = r.apparent_encoding
    for m in PDF_SRC.finditer(r.text):
        href = m.group(1)
        label = re.sub(r'<[^>]+>', '', m.group(2))
        if 'plik' in label.lower() or href.lower().endswith('.pdf'):
            return href if href.startswith('http') else BASE + '/' + href.lstrip('/')
    return None


def download_pdf(url, cache_dir, key):
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, f"{key}.pdf")
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        r = requests.get(url, headers={'User-Agent': UA['User-Agent']}, timeout=90)
        open(path, 'wb').write(r.content)
        time.sleep(0.2)
    return path


def scrape(cache_dir):
    recs = [r for r in fetch_records() if r['session_date'] and r['session_date'] >= KAD_START]
    # dedupe by (roman) keeping last
    unique = {}
    for r in recs:
        unique[r['roman'] or r['id9']] = r
    recs = sorted(unique.values(), key=lambda r: r['n'])
    print(f"[sroda-slaska] {len(recs)} IX-kadencja session documents", flush=True)
    all_votes = []
    for rec in recs:
        try:
            pdf_url = get_pdf_url(rec['id9'])
        except Exception as e:
            print(f"  {rec['roman']} detail ERR {e}", flush=True); continue
        if not pdf_url:
            print(f"  {rec['roman']} no pdf link", flush=True); continue
        path = download_pdf(pdf_url, os.path.join(cache_dir, 'pdf'), f"{rec['id9']}")
        try:
            full, first = _extract_robust(path)
            sess = parse_voting_text(full, first, source_name=Path(path).name)
        except Exception as e:
            print(f"  {rec['roman']} parse ERR {e}", flush=True); continue
        vcount = sess.get('vote_count', 0)
        for vi, v in enumerate(sess['votes']):
            nv = {}
            for g, key in (('za','za'),('przeciw','przeciw'),('wstrzymal_sie','wstrzymal_sie'),
                           ('brak_glosu','brak_glosu'),('nieobecni','nieobecni')):
                nv[key] = [_norm_name(n) for n in v.get('named_votes', {}).get(g, []) if _is_name(n)]
            all_votes.append({
                'session_roman': rec['roman'] or sess.get('number_roman') or '?',
                'session_n': rec['n'],
                'date': rec['session_date'] or sess.get('date'),
                'session_source_date': rec['session_date'],
                'topic': v.get('topic', ''),
                'counts': v.get('counts', {}),
                'named_votes': nv,
                'source_url': pdf_url,
            })
        print(f"  {rec['roman']} (n{rec['n']}) {rec['session_date']}: {vcount} votes ({len(all_votes)} tot)", flush=True)
    return all_votes


def build_files(votes, city_dir, scraped_at):
    # roster in canonical order from config councilor_roster, else from votes
    cfg = json.loads((Path(city_dir) / 'config.json').read_text(encoding='utf-8'))
    roster_ordered = [n for n in cfg.get('councilor_roster', [])]
    seen = set()
    for v in votes:
        for n in v['named_votes'].get('za', []) + v['named_votes'].get('przeciw', []) \
                + v['named_votes'].get('wstrzymal_sie', []) + v['named_votes'].get('nieobecni', []):
            if n not in seen:
                seen.add(n)
                if n not in roster_ordered:
                    roster_ordered.append(n)
    roster = sorted(roster_ordered)

    ca = defaultdict(Counter)
    for v in votes:
        for grp, key in (('za','za'),('przeciw','przeciw'),('wstrzymal_sie','wstrzymal'),
                         ('brak_glosu','brak'),('nieobecni','nieobecny')):
            for n in v['named_votes'].get(grp, []):
                ca[n][key] += 1

    s_map = defaultdict(list)
    for v in votes:
        s_map[v['session_n']].append(v)
    sessions = []
    for n in sorted(s_map.keys()):
        sv = s_map[n]
        roman = sv[0]['session_roman']
        date = sv[0]['date'] or sv[0]['session_source_date']
        att = []
        for v in sv:
            for g in ('za','przeciw','wstrzymal_sie','brak_glosu'):
                att += v['named_votes'].get(g, [])
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
        nv = {'za': [], 'przeciw': [], 'wstrzymal_sie': []}
        for g, key in (('za','za'),('przeciw','przeciw'),('wstrzymal_sie','wstrzymal_sie')):
            nv[key] = list(v['named_votes'].get(g, []))
        kad['votes'].append({'id': str(vi), 'source_url': v['source_url'],
                             'session_date': v['date'], 'session_number': v['session_roman'],
                             'topic': v['topic'], 'druk': '', 'resolution': '',
                             'counts': {'za': v['counts'].get('za',0), 'przeciw': v['counts'].get('przeciw',0),
                                        'wstrzymal_sie': v['counts'].get('wstrzymal_sie',0)},
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
    cache_dir = os.environ.get('RADOSKOP_CACHE_DIR', '/cache/sroda-slaska')
    args = sys.argv[1:]
    if '--city-dir' in args:
        city_dir = args[args.index('--city-dir')+1]
    if '--cache-dir' in args:
        cache_dir = args[args.index('--cache-dir')+1]
    os.makedirs(cache_dir, exist_ok=True)
    scraped_at = __import__('datetime').datetime.now().astimezone().isoformat()
    votes = scrape(cache_dir)
    print(f"[sroda-slaska] {len(votes)} votes total", flush=True)
    kad = build_files(votes, city_dir, scraped_at)
    print(f"[sroda-slaska] sessions={kad['total_sessions']} votes={kad['total_votes']} councilors={kad['total_councilors']}")


if __name__ == '__main__':
    main()
