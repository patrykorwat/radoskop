#!/usr/bin/env python3
"""Radoskop Mońki scraper — scanned "Imienny wykaz głosowań" PDFs on Wrota-Podlasia BIP.

Source: https://bip-ummonki.podlaskie.eu/rada_miejska/imienne_wykazy_gosowa_radnych/
  per-session scanned PDFs (I..XXI, IX kad.). Recover per-councilor votes (ZA/PRZECIW/
  wstrzymał/nieobecny) via OCR (tesseract -l pol) + roster-anchored matching +
  aggregate validation per vote (only fully-validated votes are emitted).

Outputs (relative to CITY_DIR/docs):
  kadencja-2024-2029.json, profiles.json, data.json
"""
import os, re, json, sys, subprocess, hashlib, unicodedata, time
import pdfplumber
import requests
from bs4 import BeautifulSoup
from difflib import SequenceMatcher
from datetime import datetime

UA = {'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0'}
BASE = 'https://bip-ummonki.podlaskie.eu'
ARTICLE = BASE + '/rada_miejska/imienne_wykazy_gosowa_radnych/imienne-wykazy-glosowan-radnych-2024-2029.html'

ROSTER = ['Burzyński Leszek Marek', 'Dąbrowski Paweł', 'Grygorczyk Andrzej', 'Iwanicka Barbara Iwona',
          'Iwanicki Marek', 'Jankowski Alojzy', 'Kukło-Bogdan Elżbieta', 'Markowski Leszek',
          'Niedziołko Wojciech Jacek', 'Rogowski Grzegorz', 'Sajkowska Mariola', 'Sierba Bogdan Kazimierz',
          'Skibicka Halina', 'Smółko Wojciech', 'Tekień Edward']
RN = [''.join(c for c in x.lower() if c.isalnum()) for x in ROSTER]
MONTHS = {'stycznia': 1, 'lutego': 2, 'marca': 3, 'kwietnia': 4, 'maja': 5, 'czerwca': 6, 'lipca': 7,
          'sierpnia': 8, 'września': 9, 'października': 10, 'listopada': 11, 'grudnia': 12}
_FALLBACK_DATE = {6: '2024-09-10', 7: '2024-09-26', 15: '2025-10-09'}

IND_AGG = re.compile(r'G[ŁL]OSOWA[ŁL]O?\s*[:]', re.I)
IND_ZA = re.compile(r'g[łlŁL]osowa[łlŁL]o\s+Z[A-Za-zŻżŹźĄąÀàÂâÄäÅå\.]+\s*[:]', re.I)
IND_PRZ = re.compile(r'g[łlŁL]osowa[łlŁL]o\s+PRZECIW\s*[:]', re.I)
IND_WSTR = re.compile(r'Wstrzyma[łlŁL]o\s+si[ęe]\s*[:]', re.I)
VOTE_LINE_RE = re.compile(r'^([ilIoLO0-9]{1,3})[\.\)\-\s]\s*([^\d].+)$')
VOTE_TOKEN_RE = re.compile(
    r'(g[łlŁL]osowa[łlŁL]\w*\s+\w+|'
    r'(?:g[łlŁL]osowa[łlŁL]\w*\s+)?(?:za\b|ża\b|przeciw\b|przeciwko\b|wstrzyma\w*(?:\s+si[ęe])?|nieobecn\w*))'
    r'\s*$', re.I)


def _nt(s):
    s = s.lower()
    for a, b in [('ą', 'a'), ('ż', 'z'), ('ź', 'z'), ('ę', 'e'), ('ł', 'l'), ('ś', 's'), ('ć', 'c'), ('ń', 'n'), ('ó', 'o')]:
        s = s.replace(a, b)
    return s


def _last_int(line):
    nums = re.findall(r'\d+', line)
    return int(nums[-1]) if nums else None


def parse_agg(lines):
    agg = {}
    for l in lines[:8]:
        if IND_AGG.search(l):
            v = _last_int(l)
            if v is not None: agg['glosowalo'] = v
        if IND_ZA.search(l):
            v = _last_int(l)
            if v is not None: agg['za'] = v
        if IND_PRZ.search(l):
            v = _last_int(l)
            if v is not None: agg['przeciw'] = v
        if IND_WSTR.search(l):
            v = _last_int(l)
            if v is not None: agg['wstrzymal'] = v
    return agg


def is_vote_row(line):
    line = line.strip()
    m = VOTE_LINE_RE.match(line)
    if not m: return None
    rest = m.group(2)
    vm = VOTE_TOKEN_RE.search(rest)
    if not vm: return None
    name = rest[:vm.start()].strip().strip('-_|·. ').strip()
    if len(name) < 3 or not re.search(r'[a-ząćęłńóśźż]', name, re.I): return None
    return (name, vm.group(1).strip())


def vote_cat(vote):
    v = _nt(vote)
    words = re.findall(r'[a-z]+', v)
    last = words[-1] if words else ''
    if last.startswith('z'): return 'za'
    if 'przeciw' in last: return 'przeciw'
    if 'wstr' in last: return 'wstrzymal'
    if last.startswith('nieo'): return 'nieobecny'
    if last.startswith('obec'): return 'brak'
    return None


def match_roster(name):
    nn = ''.join(c for c in name.lower() if c.isalnum())
    best = None; bs = 0
    for i, r in enumerate(RN):
        s = SequenceMatcher(None, nn, r).ratio()
        if s > bs: bs = s; best = i
    return best, bs


def slugify(s):
    sl = _nt(s)
    return re.sub(r'[^a-z0-9]+', '-', sl).strip('-')


def to_display(name):
    """Source lists 'Nazwisko Imię'; Radoskop convention = 'Imię Nazwisko' (swap first token)."""
    parts = name.split()
    if len(parts) >= 2:
        return ' '.join(parts[1:]) + ' ' + parts[0]
    return name


# ---------- fetch + OCR ----------
def fetch_attachments(cache_dir):
    """Return list of (title, local_path) for the 21 session PDFs."""
    adir = os.path.join(cache_dir, 'attachments')
    os.makedirs(adir, exist_ok=True)
    r = requests.get(ARTICLE, headers=UA, timeout=30)
    soup = BeautifulSoup(r.text, 'lxml')
    items = []
    for a in soup.find_all('a', href=True):
        h = a['href']; txt = ' '.join(a.get_text(' ', strip=True).split())
        if '/resource/' in h and ('sesja' in txt.lower() or 'wykaz' in txt.lower()) and txt:
            items.append((txt, h if h.startswith('http') else BASE + h))
    out = []
    for i, (title, url) in enumerate(items):
        fname = f"s{i+1:02d}.pdf"
        path = os.path.join(adir, fname)
        if not os.path.exists(path) or os.path.getsize(path) == 0:
            rr = requests.get(url, headers=UA, timeout=120)
            open(path, 'wb').write(rr.content)
            time.sleep(0.3)
        out.append((title, path))
    return out


def ocr_pdf(path, cache_dir):
    base = os.path.splitext(os.path.basename(path))[0]
    ocrdir = os.path.join(cache_dir, 'ocr')
    os.makedirs(ocrdir, exist_ok=True)
    texts = []
    with pdfplumber.open(path) as pdf:
        for i, p in enumerate(pdf.pages):
            cache = os.path.join(ocrdir, f"{base}_p{i}.txt")
            if os.path.exists(cache) and os.path.getsize(cache) > 0:
                texts.append(open(cache, encoding='utf-8').read()); continue
            png = cache + "_tmp.png"
            p.to_image(resolution=220).save(png)
            try:
                r = subprocess.run(['tesseract', png, 'stdout', '-l', 'pol', '--psm', '6'],
                                   capture_output=True, text=True, timeout=120)
                txt = r.stdout
            except Exception:
                txt = ''
            try: os.remove(png)
            except Exception: pass
            open(cache, 'w', encoding='utf-8').write(txt)
            texts.append(txt)
    return texts


def parse_votes(attachments, cache_dir):
    """attachments: list of (title, path). Return list of validated vote dicts + info."""
    votes = []
    for idx, (title, path) in enumerate(attachments):
        pages = ocr_pdf(path, cache_dir)
        lines = "\n".join(pages).split("\n")
        agg_idx = [i for i, l in enumerate(lines) if IND_AGG.search(l)]
        for k, i in enumerate(agg_idx):
            end = agg_idx[k + 1] if k + 1 < len(agg_idx) else len(lines)
            window = lines[i:end]
            agg = parse_agg(window)
            rows = []
            for l in window:
                r = is_vote_row(l)
                if r: rows.append(r)
            if not rows or 'glosowalo' not in agg or 'za' not in agg: continue
            # title
            tl = []
            j = i - 1
            while j >= 0 and len(tl) < 4:
                ln = lines[j].strip()
                if ln and not IND_AGG.search(ln) and not IND_ZA.search(ln) and not IND_PRZ.search(ln) \
                        and not IND_WSTR.search(ln) and not re.match(r'jak|LP\.?|\.\.|~|"|Rady|Wojciech', ln, re.I) \
                        and not ln.startswith(('ul.', 'Sesja', 'sesji')) and len(ln) > 2:
                    tl.insert(0, ln)
                if re.match(r'^\s*\d+\.', ln) or 'sesji' in ln.lower(): break
                j -= 1
            # assign + validate
            assigned = []
            for name, vote in rows:
                ri, sc = match_roster(name); cat = vote_cat(vote)
                if cat is None or sc < 0.8 or ri is None: continue
                assigned.append((to_display(ROSTER[ri]), cat))
            if not assigned: continue
            counts = {'za': 0, 'przeciw': 0, 'wstrzymal': 0}
            for _, c in assigned:
                if c in counts: counts[c] += 1
            exp = (agg.get('za', 0), agg.get('przeciw', 0), agg.get('wstrzymal', 0))
            got = (counts['za'], counts['przeciw'], counts['wstrzymal'])
            if got != exp or counts['za'] + counts['przeciw'] + counts['wstrzymal'] != agg['glosowalo']:
                continue
            nv = {'za': [n for n, c in assigned if c == 'za'],
                  'przeciw': [n for n, c in assigned if c == 'przeciw'],
                  'wstrzymal_sie': [n for n, c in assigned if c == 'wstrzymal']}
            votes.append({'session': idx + 1, 'topic': ' | '.join(tl)[:200],
                          'counts': {'za': counts['za'], 'przeciw': counts['przeciw'],
                                     'wstrzymal_sie': counts['wstrzymal']}, 'named_votes': nv})
    return votes


def session_meta(i, attachments):
    title = attachments[i - 1][0]
    rom = re.search(r'([IVXLCDM]+)\s*[Ss]esja', title)
    rom = rom.group(1) if rom else str(i)
    d = re.search(r'z dnia\s+(\d{1,2})\s+(\w+)\s+(\d{4})', title)
    date = None
    if d:
        dd = int(d.group(1)); mon = MONTHS.get(_nt(d.group(2)))
        if mon: date = f"{d.group(3)}-{mon:02d}-{dd:02d}"
    if date is None: date = _FALLBACK_DATE.get(i)
    return rom, date


def build_files(votes, attachments, city_dir, scraped_at):
    from collections import defaultdict, Counter
    smap = defaultdict(list)
    for v in votes: smap[v['session']].append(v)
    ca = defaultdict(Counter)
    for v in votes:
        for cat, names in v['named_votes'].items():
            for n in names: ca[n][cat] += 1
    roster = sorted(ca.keys())
    sessions = []
    for i in sorted(smap.keys()):
        rom, date = session_meta(i, attachments)
        att = []
        for v in smap[i]:
            for cat, names in v['named_votes'].items():
                if cat != 'nieobecny': att.extend(names)
        att = list(dict.fromkeys(att))
        sessions.append({'date': date, 'number': rom, 'vote_count': len(smap[i]),
                         'attendee_count': len(att), 'attendees': att, 'speakers': []})
    councilors = []
    for n in roster:
        c = ca[n]
        za = c.get('za', 0); pr = c.get('przeciw', 0); wz = c.get('wstrzymal_sie', 0)
        br = c.get('brak', 0); nb = c.get('nieobecny', 0)
        total = za + pr + wz + br + nb
        frek = round(100.0 * (total - nb) / total, 1) if total else 0.0
        akt = round(100.0 * (za + pr + wz) / total, 1) if total else 0.0
        councilors.append({'name': n, 'club': 'NZ', 'district': None, 'frekwencja': frek, 'aktywnosc': akt,
                           'zgodnosc_z_klubem': 0.0, 'votes_za': za, 'votes_przeciw': pr, 'votes_wstrzymal': wz,
                           'votes_brak': br, 'votes_nieobecny': nb, 'votes_total': total, 'rebellion_count': 0,
                           'rebellions': [], 'has_activity_data': False, 'activity': None})
    kad = {'id': '2024-2029', 'label': 'IX kadencja (2024–2029)', 'clubs': {}, 'sessions': sessions,
           'total_sessions': len(sessions), 'total_votes': len(votes), 'total_councilors': len(councilors),
           'councilors': councilors, 'votes': [], 'similarity_top': [], 'similarity_bottom': []}
    for vi, v in enumerate(votes):
        rom, date = session_meta(v['session'], attachments)
        kad['votes'].append({'id': str(vi), 'source_url': ARTICLE,
                             'session_date': date, 'session_number': rom, 'topic': v['topic'],
                             'druk': '', 'resolution': '', 'counts': v['counts'], 'named_votes': v['named_votes']})
    profiles = []
    for n in roster:
        c = ca[n]
        za = c.get('za', 0); pr = c.get('przeciw', 0); wz = c.get('wstrzymal_sie', 0)
        br = c.get('brak', 0); nb = c.get('nieobecny', 0)
        total = za + pr + wz + br + nb
        frek = round(100.0 * (total - nb) / total, 1) if total else 0.0
        akt = round(100.0 * (za + pr + wz) / total, 1) if total else 0.0
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
    city_dir = '.'
    cache_dir = os.environ.get('RADOSKOP_CACHE_DIR', '/cache/monki')
    if len(sys.argv) > 1 and sys.argv[1] == '--city-dir':
        city_dir = sys.argv[2]
    if len(sys.argv) > 3 and sys.argv[3] == '--cache-dir':
        cache_dir = sys.argv[4]
    os.makedirs(cache_dir, exist_ok=True)
    scraped_at = datetime.now().astimezone().isoformat()
    attachments = fetch_attachments(cache_dir)
    print(f"[monki] {len(attachments)} attachments", flush=True)
    votes = parse_votes(attachments, cache_dir)
    print(f"[monki] {len(votes)} validated votes", flush=True)
    kad = build_files(votes, attachments, city_dir, scraped_at)
    print(f"[monki] sessions={kad['total_sessions']} votes={kad['total_votes']} councilors={kad['total_councilors']}")


if __name__ == '__main__':
    main()
