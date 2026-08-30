#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop Lwówek Śląski — imienne głosowania Rady Miejskiej (DSSS PRINT w protokołach BIP).

Źródło: https://bip.lwowekslaski.pl (platforma 'Organy' idcom-jst). Rada Miejska
publikuje per-sesja protokół PDF (idcom-jst files) zawierający per-głosowanie:
    Głosowanie w sprawie: {temat}
    Upoważnionych N / Głosujących N / Głosów ZA N / Głosów PRZECIW N /
    Głosów wstrzymujących się N
    Lista imienna:
    1 {Nazwisko Imię}  TAK
    2 ...  NIE
    ...
Radni to 15 osób; urzędnicy (Burmistrz, Z-ca, Skarbnik) mają token 'brak uprawnień'
i są odfiltrowywani po rosterze (mapowanie imię+nazwisko → pełne nazwisko z rosteru).
Format TEKSTOWY (warstwa tekstowa PDF). Każde głosowanie walidowane vs agregat
(Za+Przeciw+Wstrzym == liczba radnych na liście).

Użycie: python scrape_lwowek_slaski.py --city-dir cities/lwowek-slaski
"""
import argparse, json, re, time, urllib.request, ssl, zipfile, io
from collections import defaultdict
from datetime import datetime
from pathlib import Path
import pymupdf

_CTX = ssl.create_default_context(); _CTX.check_hostname = False; _CTX.verify_mode = ssl.CERT_NONE
HEADERS = {'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) Chrome/120 Radoskop/1.0'}
BASE = 'https://bip.lwowekslaski.pl'
KAD_START = '2024-05-07'
KADENCJA_ID = '2024-2029'
KADENCJA_LABEL = 'IX kadencja (2024\u20132029)'
_REQ = 0.5; _LAST = 0.0

# 15 radnych IX kadencji (z BIP /organy/808/1124)
ROSTER = [
    'Magdalena Wioleta Lewandowska', 'Tadeusz Koblak', 'Rafał Krzysztof Kościelny',
    'Mateusz Jan Rusinek', 'Michał Piotr Biegacz', 'Agnieszka Izabela Nieratka',
    'Franciszek Eugeniusz Pawłowicz', 'Jacek Skrucha', 'Jarosław Mateusz Działa',
    'Grzegorz Ślusarczyk', 'Michał Bronisław Kamieński', 'Rafał Zieliński',
    'Zbigniew Eck', 'Marta Małgorzata Butrymowicz', 'Marek Szramowiat',
]
VOTE_TOKEN = {'TAK': 'za', 'NIE': 'przeciw', 'WSTRZ': 'wstrzymal_sie',
              'WSTRZYMUJE SIĘ': 'wstrzymal_sie',
              'WSTRZYMUJĘ SIĘ': 'wstrzymal_sie', 'WSTRZYMAŁ SIĘ': 'wstrzymal_sie',
              'WSTRZYMAL SIE': 'wstrzymal_sie'}

def _rate():
    global _LAST
    now = time.time()
    if now - _LAST < _REQ:
        time.sleep(_REQ - (now - _LAST))
    _LAST = time.time()

def _fetch(url, binary=False):
    _rate()
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=90, context=_CTX) as r:
        return r.read() if binary else r.read().decode('utf-8', 'replace')

def _roster_key(full):
    parts = full.split()
    return (parts[0], parts[-1]) if parts else ('', '')

ROSTER_KEYS = {_roster_key(n): n for n in ROSTER}

def date_from_page(last, first_pg):
    # "Przeprowadzonego w dniu 29 maja 2026 r." or "z dnia 9 lipca 2025 r."
    m = re.search(r'(?:przeprowadzonego\s+w dniu|w dniu|z dnia)\s+(\d{1,2})[ .]([a-ząćęłńóśźż]+)[ .](\d{4})',
                  first_pg, re.I)
    if m:
        mon = {'stycznia':1,'lutego':2,'marca':3,'kwietnia':4,'maja':5,'czerwca':6,'lipca':7,
               'sierpnia':8,'września':9,'października':10,'listopada':11,'grudnia':12}
        mm = mon.get(m.group(2).lower())
        if mm:
            return f"{m.group(3)}-{mm:02d}-{int(m.group(1)):02d}"
    # fallback: from URL wiadomosc title has 'z dnia' sometimes
    return None

# ---- odkrywanie sesji ----
def discover_sessions():
    prots = {}
    for y in ['2024', '2025', '2026']:
        for page in range(1, 20):
            u = f'{BASE}/organy/808/dokumenty/1370/lista/{page}/{y}'
            b = _fetch(u)
            found = {}
            for m in re.finditer(r'href="[^"]*dokumenty/1370/wiadomosc/(\d+)/[^"]*"[^>]*>(.*?)</a>', b, re.S):
                t = re.sub(r'<[^>]+>', '', m.group(2)).strip()
                if t:
                    found[m.group(1)] = t
            if not found:
                break
            prots.update(found)
            if not re.search(r'href="[^"]*lista/%d/%s' % (page + 1, y), b):
                break
            if page > 18:
                break
    # dla każdej sesji: pdf + data
    out = []
    for wid, title in prots.items():
        b = _fetch(f'{BASE}/organy/808/dokumenty/1370/wiadomosc/{wid}/')
        m = re.search(r'(https://bip-v1-files\.idcom-jst\.pl/sites/\d+/wiadomosci/\d+/files/[^"\']+\.pdf)', b)
        if not m:
            continue
        out.append({'id': wid, 'title': re.sub(r'\s+', ' ', title)[:90], 'pdf': m.group(1)})
    out.sort(key=lambda s: s['id'])
    return out

# ---- parser protokołu ----
def _page_rows(pg):
    rows = defaultdict(list)
    for x0, y0, x1, y1, w, *_ in pg.get_text('words'):
        rows[round(y0 / 5)].append((x0, w))
    out = []
    for y in sorted(rows):
        ws = sorted(rows[y], key=lambda z: z[0])
        out.append(' '.join(w for _, w in ws).strip())
    return out

_NAMED_RE = re.compile(r'^(?:(\d+)\s+)?(.+?)\s+(TAK|NIE|WSTRZ|WSTRZYMUJE SIĘ|WSTRZYMUJĘ SIĘ|WSTRZYMAŁ SIĘ|WSTRZYMAL SIE|brak uprawnień)\s*$', re.I)

def parse_pdf(pdf_bytes):
    """Zwraca listę głosowań: {topic, agg{za,przeciw,wstrzym}, named[ (name, token) ]}.

    Kotwicą każdego głosowania jest wiersz "Upoważnionych <N>" (początek bloku
    agregatów). Po nim idą Głosujących / Głosów ZA / PRZECIW / wstrzymujących się
    i "Lista imienna:" + lista radnych. Temat głosowania = wiersze pomiędzy końcem
    poprzedniej listy imiennej a tym "Upoważnionych". Bloki mogą przechodzić
    między stronami.
    """
    d = pymupdf.open(stream=pdf_bytes, filetype='pdf')
    rows = []
    for p in d:
        rows.extend(_page_rows(p))
    rows.append('<<EOF>>')

    _IS_END = ('Ad. pkt', 'Ad pkt', 'Realizacja porządku obrad', 'Zamknięcie sesji',
               'Wolny mikrofon', 'Protokół został', 'Sprawy sołeckie')

    _UPO = re.compile(r'Upoważnionych\s*\d')
    _GLOS = re.compile(r'Głosów\s+ZA\s+(\d+)', re.I)
    _GPR = re.compile(r'Głosów\s+PRZECIW\s+(\d+)', re.I)
    _GWZ = re.compile(r'Głosów\s+wstrzymujących\s+się\s+(\d+)', re.I)

    votes = []
    n = len(rows)
    i = 0
    section_start = 0  # początek regionu tematowego (po poprzedniej liście / na początku)
    while i < n:
        r = rows[i]
        if _UPO.match(r):
            # znajdź "Lista imienna:" w tym bloku
            k = i + 1
            li = None
            while k < n:
                if 'Lista imienna:' in rows[k]:
                    li = k
                    break
                if rows[k].startswith(_IS_END) or _UPO.match(rows[k]) or rows[k] == '<<EOF>>':
                    break  # blok bez listy imiennej (pomijamy)
                k += 1
            if li is not None:
                # agregaty z tego bloku
                block_txt = ' '.join(rows[i:li])
                agg = {}
                mza = _GLOS.search(block_txt); mpr = _GPR.search(block_txt); mwz = _GWZ.search(block_txt)
                if mza: agg['za'] = int(mza.group(1))
                if mpr: agg['przeciw'] = int(mpr.group(1))
                if mwz: agg['wstrzym'] = int(mwz.group(1))
                # temat z regionu (section_start .. i)
                topic = ''
                for row in rows[section_start:i]:
                    clean = re.sub(r'^Głosowanie\s+(?:w sprawie|nr \d+)[:.\-]*\s*', '', row).strip()
                    if clean and not re.match(r'^Głosów', clean) and clean != 'Lista imienna:' \
                       and not re.match(r'^(Upoważnionych|Głosujących|Uprawnionych)', clean):
                        topic = (topic + ' ' + clean).strip()
                # lista imienna
                j = li + 1
                named = []
                while j < n:
                    rj = rows[j]
                    if ('Lista imienna:' in rj) or rj.startswith(_IS_END) or _UPO.match(rj) or rj == '<<EOF>>':
                        break
                    mm = _NAMED_RE.match(rj)
                    if mm:
                        token = mm.group(3).upper()
                        cat = 'skip' if token.startswith('BRAK') else VOTE_TOKEN.get(token, 'skip')
                        if mm.group(2).strip():
                            named.append((mm.group(2).strip(), cat))
                    j += 1
                votes.append({'topic': topic[:200], 'agg': agg, 'named': named})
                section_start = j
                i = j
            else:
                i += 1
        else:
            i += 1
    return votes

# ---- roster-mapowanie + walidacja ----
def _norm_vote_name(name):
    parts = name.split()
    if not parts:
        return None
    return (parts[0], parts[-1])

def map_named(named_list):
    """named -> {za:[full], przeciw:[], wstrzymal_sie:[]} tylko dla radnych z rosteru."""
    out = {'za': [], 'przeciw': [], 'wstrzymal_sie': []}
    for name, cat in named_list:
        if cat == 'skip':
            continue
        key = _norm_vote_name(name)
        full = ROSTER_KEYS.get(key)
        if full:
            out[cat].append(full)
    return out

def reconcile(vote):
    counts = {k: len(v) for k, v in vote['named'].items()}
    agg = vote['agg']
    if not agg:
        return False, counts
    exp = (agg.get('za', 0), agg.get('przeciw', 0), agg.get('wstrzym', 0))
    got = (counts['za'], counts['przeciw'], counts['wstrzymal_sie'])
    return (exp == got), counts

# ---- output ----
def make_slug(name):
    repl = {'ą':'a','ć':'c','ę':'e','ł':'l','ń':'n','ó':'o','ś':'s','ź':'z','ż':'z'}
    slug = name.lower()
    for pl, a in repl.items():
        slug = slug.replace(pl, a)
    return re.sub(r'[^a-z0-9]+', '', slug)

def build(city_dir: Path, sessions, all_votes, session_dates):
    docs = city_dir / 'docs'
    docs.mkdir(parents=True, exist_ok=True)
    cfg = json.loads((city_dir / 'config.json').read_text(encoding='utf-8'))

    total_votes = 0
    votes_out = []
    by_date = defaultdict(lambda: {'vc': 0, 'att': set()})
    vid = 0
    for sess in sessions:
        sd = session_dates.get(sess['id'])
        if not sd or sd < KAD_START:
            continue
        for v in sess['votes']:
            vid += 1
            total_votes += 1
            by_date[sd]['vc'] += 1
            for names in v['named'].values():
                by_date[sd]['att'].update(names)
            votes_out.append({'id': str(vid), 'session_date': sd, 'session_number': sd,
                              'topic': v['topic'], 'named_votes': v['named'],
                              'counts': {k: len(lst) for k, lst in v['named'].items()}})
    sessions_data = []
    for sd in sorted(by_date):
        sessions_data.append({'date': sd, 'number': sd, 'vote_count': by_date[sd]['vc'],
                              'attendee_count': len(by_date[sd]['att']),
                              'attendees': sorted(by_date[sd]['att']), 'speakers': []})
    # councilors z rosteru + statystyki
    cv = defaultdict(lambda: {'za':0,'przeciw':0,'wstrzymal_sie':0, 'sess':set()})
    for v in votes_out:
        for cat, names in v['named_votes'].items():
            for nm in names:
                cv[nm][cat] += 1
                cv[nm]['sess'].add(v['session_date'])
    n_sessions = len(sessions_data)
    councilors_list = []
    for full in ROSTER:
        c = cv[full]
        present = c['za'] + c['przeciw'] + c['wstrzymal_sie']
        aktywnosc = (present / total_votes * 100) if total_votes else 0
        frekwencja = (len(c['sess']) / n_sessions * 100) if n_sessions else 0
        councilors_list.append({
            'name': full, 'club': '', 'district': None,
            'frekwencja': round(frekwencja, 1), 'aktywnosc': round(aktywnosc, 1),
            'zgodnosc_z_klubem': 0.0, 'votes_za': c['za'], 'votes_przeciw': c['przeciw'],
            'votes_wstrzymal': c['wstrzymal_sie'], 'votes_brak': 0, 'votes_nieobecny': 0,
            'votes_total': total_votes, 'rebellion_count': 0, 'rebellions': [],
            'has_activity_data': False, 'activity': None})
    kad = {'id': KADENCJA_ID, 'label': KADENCJA_LABEL, 'clubs': {},
           'sessions': sessions_data, 'total_sessions': n_sessions,
           'total_votes': total_votes, 'total_councilors': len(councilors_list),
           'councilors': councilors_list, 'votes': votes_out,
           'similarity_top': [], 'similarity_bottom': []}
    data = {'generated': datetime.now().isoformat(), 'default_kadencja': KADENCJA_ID,
            'kadencje': [kad]}
    (docs / f'kadencja-{KADENCJA_ID}.json').write_text(json.dumps(kad, ensure_ascii=False), encoding='utf-8')
    (docs / 'data.json').write_text(json.dumps({'generated': data['generated'],
                                                 'default_kadencja': KADENCJA_ID,
                                                 'kadencje':[{'id': KADENCJA_ID, 'label': KADENCJA_LABEL}]},
                                                ensure_ascii=False), encoding='utf-8')
    # profiles
    profiles = []
    for full in ROSTER:
        c = cv[full]
        total = c['za'] + c['przeciw'] + c['wstrzymal_sie'] or 1
        sess = len(c['sess'])
        aktywn = (total / total_votes * 100) if total_votes else 0
        profiles.append({'name': full, 'slug': make_slug(full),
                         'kadencje': {KADENCJA_ID: {'club': '', 'has_voting_data': True,
                             'has_activity_data': False,
                             'frekwencja': round(sess / max(1, n_sessions) * 100, 1),
                             'aktywnosc': round(aktywn, 1), 'zgodnosc_z_klubem': 0.0,
                             'votes_za': c['za'], 'votes_przeciw': c['przeciw'],
                             'votes_wstrzymal': c['wstrzymal_sie'], 'votes_brak': 0,
                             'votes_nieobecny': 0, 'votes_total': total,
                             'rebellion_count': 0, 'rebellions': [], 'roles': [],
                             'notes': '', 'former': False, 'mid_term': False}}})
    (docs / 'profiles.json').write_text(json.dumps({'profiles': profiles, 'total': len(profiles)},
                                                    ensure_ascii=False), encoding='utf-8')
    (docs / 'config.json').write_text(json.dumps(cfg, ensure_ascii=False), encoding='utf-8')
    return total_votes, n_sessions, len(councilors_list)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--city-dir', required=True)
    args = ap.parse_args()
    city_dir = Path(args.city_dir)
    sessions = discover_sessions()
    print(f'[lwowek-slaski] sesje (protokoły z PDF): {len(sessions)}')
    session_dates = {}
    all_votes = []
    recons = 0; total = 0; skipped = 0
    for s in sessions:
        try:
            pdf = _fetch(s['pdf'], binary=True)
        except Exception as e:
            print(f'  [ERR fetch {s["id"]}] {e}'); continue
        vs = parse_pdf(pdf)
        # data sesji z pierwszej strony
        d = pymupdf.open(stream=pdf, filetype='pdf')
        first = d[0].get_text()
        dt = date_from_page(s['id'], first)
        if dt and dt >= KAD_START:
            session_dates[s['id']] = dt
        mapped = []
        for v in vs:
            named = map_named(v['named'])
            v['named'] = named
            ok, _ = reconcile(v)
            total += 1
            if ok:
                recons += 1
            else:
                skipped += 1
                continue
            mapped.append(v)
        s['votes'] = mapped
        all_votes += mapped
        print(f"  {s['id']} date={dt} votes={len(vs)} ok={len(mapped)}")
    tv, ns, nc = build(city_dir, sessions, all_votes, session_dates)
    print(f'[lwowek-slaski] total_votes={tv} sessions={ns} councilors={nc} '
          f'reconciled={recons}/{total} skipped={skipped}')

if __name__ == '__main__':
    main()
