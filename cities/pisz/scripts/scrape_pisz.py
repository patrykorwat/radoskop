#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Radoskop Pisz — imienne głosowania Rady Miejskiej w Piszu (IX kadencja 2024-2029).

Źródło: BIP Urzędu Miejskiego w Piszu na CMS hi.pl (bip.pisz.hi.pl).
Rada Miejska publikuje w kategorii Rada Miejska → "Imienny wykaz głosowań"
(k=1133) per-sesja artykuł ("N Sesja Rady Miejskiej w Piszu", data "Posiedzenie
z dnia DD miesiąc RRRR") z załącznikiem PDF "Imienny wykaz głosowań"
(download.php?id=N) — czysty tekst (pdfplumber), tabele per-głosowanie:
  Lp | Nazwisko i imię | Głos   (dwie kolumny; ZA / PRZECIW / WSTRZYMUJĘ SIĘ /
  NIEOBECNA / OBECNA-obecny-niegłosujący).
Każda strona PDF = jedno głosowanie (agregat + tabela imienna).

Nazwiska normalizowane do kanonicznego składu Rady z BIP (k=53 "Skład osobowy",
21 radnych IX kad. 2024-2029) przez Levenshtein (<=2 edycje). Niejednoznaczne /
niemapowane nazwiska (szumy ekstrakcji tekstu, połączone kolumny) SĄ ODRZUCANE —
nigdy nie zgadujemy przypisania. Głosy mapowane walidowane per-głosowanie
przeciw agregatowi z PDF; niespełnione głosowania są odrzucane.

Użycie:
    python scrape_pisz.py --output docs/data.json --profiles docs/profiles.json
                             [--cache-dir .cache]
"""

import argparse
import hashlib
import io
import json
import re
import sys
import time
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime
from itertools import combinations
from pathlib import Path

import pdfplumber
import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BIP = "https://bip.pisz.hi.pl"
GLOS_CAT = "1133"        # Rada Miejska -> Imienny wykaz głosowań
KAD_START = "2024-05-07"  # początek IX kadencji
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36"}

# ---------------------------------------------------------------------------
# Kanoniczny skład Rady (BIP k=53, IX kadencja 2024-2029) — 21 radnych
# ---------------------------------------------------------------------------
CANON = [
    'Bobko Anna', 'Ciecierska Marzena', 'Czerwiński Krzysztof', 'Górski Jarosław',
    'Kaczkowski Dariusz', 'Konopa Zuzanna', 'Krawczyk Adam', 'Krośniewski Robert',
    'Olender Dariusz', 'Pardo Agnieszka', 'Pietrzyk Łukasz', 'Roszczypała Jolanta',
    'Sawicka Aneta', 'Sparzak Maciej', 'Stawecki Wojciech', 'Szmigiel Małgorzata',
    'Szpanko Mariusz', 'Szymborski Andrzej', 'Święconek Karol', 'Trupacz Mariusz',
    'Zadroga Andrzej', 'Zuzga Sebastian',
]


def _normname(s):
    s = s.lower().replace('ł', 'l').replace('Ł', 'L')
    s = unicodedata.normalize('NFKD', s)
    s = ''.join(c for c in s if not unicodedata.combining(c))
    return re.sub(r'[^a-z0-9]', '', s)


CANON_NORM = [_normname(c) for c in CANON]
ROSTER_BY_NORM = {_normname(c): c for c in CANON}


def _lev(a, b):
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def canon_of(raw_name):
    n = _normname(raw_name)
    if not n:
        return None
    best = None
    best_d = 99
    second_d = 99
    for i, cn in enumerate(CANON_NORM):
        d = _lev(n, cn)
        if d < best_d:
            second_d = best_d
            best_d = d
            best = CANON[i]
        elif d < second_d:
            second_d = d
    if best_d <= 2 and (second_d - best_d) >= 2:
        return best
    return None  # niejednoznaczne / niemapowane — odrzuć, nie zgaduj


# ---- vote vocabulary ----
VOTE_RX = r'\b(?:ZA|PRZECIW|WSTRZYMUJ[ĘE]\s+SIĘ|NIEOBECN[AIY]|NIEOBECNY|OBECN[AY]|BRAK\s+GŁOSU|O)\b'
VOTE_MAP = {'ZA': 'za', 'PRZECIW': 'przeciw',
            'WSTRZYMUJĘ SIĘ': 'wstrzymal_sie', 'WSTRZYMUJE SIĘ': 'wstrzymal_sie',
            'NIEOBECNA': 'nieobecni', 'NIEOBECNY': 'nieobecni', 'NIEOBECNI': 'nieobecni',
            'OBECNA': 'brak', 'OBECNY': 'brak', 'BRAK GŁOSU': 'brak', 'O': 'brak'}
AGG = re.compile(
    r'Liczba uprawnionych\s+(?:(\d+)|O)\s+Głosy za\s+(?:(\d+)|O)\s*\n'
    r'Liczba obecnych\s+(?:(\d+)|O)\s+Głosy przeciw\s+(?:(\d+)|O)\s*\n'
    r'Liczba nieobecnych\s+(?:(\d+)|O)\s+Głosy wstrzymujące się\s+(?:(\d+)|O)')

_MONTH_PL = {'stycznia': 1, 'lutego': 2, 'marca': 3, 'kwietnia': 4, 'maja': 5, 'czerwca': 6,
             'lipca': 7, 'sierpnia': 8, 'września': 9, 'wrzesnia': 9, 'października': 10,
             'pazdziernika': 10, 'listopada': 11, 'grudnia': 12}


def _cv(v):
    return VOTE_MAP.get(re.sub(r'\s+', ' ', v).strip(), None)


_LP = re.compile(r'^(?:\d+[.,:\'’]?|[IVXLCDM]{1,4}\.?|[A-Za-z]{1,2}\.?)$')


def _strip_lp(name):
    m = re.match(r'^\s*(\S+)\s*(.*)$', name, re.S)
    if m and _LP.match(m.group(1)):
        return m.group(2).strip()
    return name.strip()


def _split_line(s):
    """Podział linii tabeli imiennej na (nazwisko, głos) rekordy wg pozycji tokenów głosu."""
    marks = [m for m in re.finditer(VOTE_RX, s)]
    if not marks:
        return []
    out = []
    if len(marks) == 1:
        f = marks[0]
        name = _strip_lp(s[:f.start()])
        if name:
            out.append((name, f.group(0)))
    else:
        m1, m2 = marks[0], marks[-1]
        n1 = _strip_lp(s[:m1.start()])
        n2 = _strip_lp(s[m1.end():m2.start()])
        if n1:
            out.append((n1, m1.group(0)))
        if n2:
            out.append((n2, m2.group(0)))
    return out


def _parse_page(t):
    recs = []
    in_table = False
    for ln in t.split('\n'):
        s = ln.strip()
        if 'Uprawnieni do głosowania' in s:
            in_table = True
            continue
        if 'Wydrukowano' in s:
            in_table = False
            continue
        if not in_table or not s:
            continue
        for name, vote in _split_line(s):
            canon = canon_of(name)
            if canon:
                recs.append((canon, _cv(vote)))
    agg = AGG.search(t)
    valid = False
    if agg:
        def z(g):
            v = agg.group(g)
            return 0 if v in (None, 'O') else int(v)
        az, ap, an, aw = z(2), z(4), z(5), z(6)
        za = sum(1 for _, v in recs if v == 'za')
        pr = sum(1 for _, v in recs if v == 'przeciw')
        wz = sum(1 for _, v in recs if v == 'wstrzymal_sie')
        nb = sum(1 for _, v in recs if v == 'nieobecni')
        valid = (za == az and pr == ap and wz == aw and nb == an)
    return recs, valid


# ---------------------------------------------------------------------------
# 1. Kolekcja sesji z kategorii k=1133 (plus archiwum)
# ---------------------------------------------------------------------------
_REQ_LAST = 0.0
REQ_DELAY = 0.25


def _rate():
    global _REQ_LAST
    now = time.time()
    d = now - _REQ_LAST
    if d < REQ_DELAY:
        time.sleep(REQ_DELAY - d)
    _REQ_LAST = time.time()


def fetch(url, cache_dir=None, binary=False):
    if cache_dir is not None:
        key = hashlib.md5(url.encode()).hexdigest()
        ext = '.bin' if binary else '.html'
        cf = cache_dir / (key + ext)
        if cf.is_file():
            return cf.read_bytes() if binary else cf.read_text(encoding='utf-8', errors='ignore')
    _rate()
    resp = requests.get(url, headers=UA, timeout=50, verify=False)
    resp.raise_for_status()
    data = resp.content if binary else resp.text
    if cache_dir is not None:
        cf = cache_dir / (key + ext)
        cf.parent.mkdir(parents=True, exist_ok=True)
        cf.write_bytes(data) if binary else cf.write_text(data, encoding='utf-8', errors='ignore')
    return data


def _session_info(wiad, cache_dir):
    url = f"{BIP}/index.php?wiad={wiad}"
    try:
        html = fetch(url, cache_dir)
    except Exception:
        return None
    m = re.search(r'<title>([^<]*)</title>', html)
    title = m.group(1).strip() if m else ''
    dm = re.search(r'Posiedzenie z dnia\s+(\d{1,2})\s+([a-ząćęłńóśźż]+)\s+(\d{4})', html)
    date_iso = None
    if dm:
        mon = _MONTH_PL.get(dm.group(2).lower())
        if mon:
            date_iso = f"{dm.group(3)}-{mon:02d}-{int(dm.group(1)):02d}"
    dl = re.search(r'download\.php\?id=(\d+)', html)
    roman = re.match(r'\s*([IVXLCDM]+)\s+Sesja', title)
    return {'wiad': wiad, 'title': title[:60], 'roman': roman.group(1) if roman else '',
            'date_iso': date_iso, 'download_id': dl.group(1) if dl else None}


def collect_sessions(cache_dir):
    seen = set()
    sessions = []
    for page_url in (f"{BIP}/index.php?k={GLOS_CAT}", f"{BIP}/index.php?archiv={GLOS_CAT}"):
        try:
            html = fetch(page_url, cache_dir)
        except Exception:
            continue
        for m in re.finditer(r'index\.php\?wiad=(\d+)', html):
            wiad = m.group(1)
            if wiad in seen:
                continue
            seen.add(wiad)
            info = _session_info(wiad, cache_dir)
            if info:
                sessions.append(info)
    uniq = {s['wiad']: s for s in sessions}
    return sorted(uniq.values(), key=lambda s: s.get('date_iso') or '9999')


def parse_report_pdf(data):
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        session_roman = None
        votes = []
        for page in pdf.pages:
            t = page.extract_text() or ''
            sm = re.search(r'\b([IVXLCDM]+)\s+Sesja Rady Miejskiej w Piszu', t)
            if sm:
                session_roman = sm.group(1)
            recs, valid = _parse_page(t)
            votes.append((recs, valid))
    return session_roman, votes


# ---------------------------------------------------------------------------
# 2. Budowanie outputu (format jak inne miasta Radoskopa)
# ---------------------------------------------------------------------------
def make_slug(name):
    repl = {'ą': 'a', 'ć': 'c', 'ę': 'e', 'ł': 'l', 'ń': 'n', 'ó': 'o', 'ś': 's', 'ź': 'z', 'ż': 'z',
            'Ą': 'A', 'Ć': 'C', 'Ę': 'E', 'Ł': 'L', 'Ń': 'N', 'Ó': 'O', 'Ś': 'S', 'Ź': 'Z', 'Ż': 'Z'}
    slug = name.lower()
    for pl, a in repl.items():
        slug = slug.replace(pl, a)
    return re.sub(r'[^a-z0-9]+', '', slug)


CLUB = defaultdict(lambda: 'NZ')  # club_assignments PENDING


def build_output(records):
    all_votes = []
    vid = 0
    sessions_by_date = {}
    for rec in records:
        d = rec['session_date']
        if not d or d < KAD_START:
            continue
        if d not in sessions_by_date:
            sessions_by_date[d] = {'date': d, 'number': rec.get('session_num', ''),
                                   'vote_count': 0, 'attendees': set()}
        named = {k: list(v) for k, v in rec['named'].items()}
        vid += 1
        sessions_by_date[d]['vote_count'] += 1
        for cat in ('za', 'przeciw', 'wstrzymal_sie', 'brak'):
            sessions_by_date[d]['attendees'].update(rec['named'].get(cat, []))
        all_votes.append({
            'id': str(vid), 'session_date': d, 'session_number': rec.get('session_num', ''),
            'topic': rec.get('topic', ''), 'named_votes': named,
            'counts': {k: len(named.get(k, [])) for k in ('za', 'przeciw', 'wstrzymal_sie')},
        })
    sessions_data = []
    for d in sorted(sessions_by_date.keys()):
        s = sessions_by_date[d]
        sessions_data.append({'date': d, 'number': s['number'], 'vote_count': s['vote_count'],
                              'attendee_count': len(s['attendees']), 'attendees': sorted(s['attendees']),
                              'speakers': []})
    all_names = set()
    for v in all_votes:
        for names in v['named_votes'].values():
            all_names.update(names)
    councilors_data = {}
    for name in all_names:
        councilors_data[name] = {'name': name, 'club': CLUB[name], 'district': None,
                                 'votes_za': 0, 'votes_przeciw': 0, 'votes_wstrzymal': 0,
                                 'votes_brak': 0, 'votes_nieobecny': 0,
                                 'votes_with_club': 0, 'votes_against_club': 0, 'rebellions': []}
    for v in all_votes:
        for cat, names in v['named_votes'].items():
            for name in names:
                if name not in councilors_data:
                    continue
                c = councilors_data[name]
                if cat == 'za':
                    c['votes_za'] += 1
                elif cat == 'przeciw':
                    c['votes_przeciw'] += 1
                elif cat == 'wstrzymal_sie':
                    c['votes_wstrzymal'] += 1
                elif cat == 'nieobecni':
                    c['votes_nieobecny'] += 1
                else:
                    c['votes_brak'] += 1
    total_votes = len(all_votes)
    total_sessions = len(sessions_data)
    councillor_sess = defaultdict(set)
    for v in all_votes:
        for cat, names in v['named_votes'].items():
            if cat != 'nieobecni':
                for n in names:
                    councillor_sess[n].add(v['session_date'])
    councilors_list = []
    for c in sorted(councilors_data.values(), key=lambda x: x['name']):
        present = c['votes_za'] + c['votes_przeciw'] + c['votes_wstrzymal'] + c['votes_brak']
        aktywnosc = (present / total_votes * 100) if total_votes else 0
        frekwencja = (len(councillor_sess.get(c['name'], set())) / total_sessions * 100) if total_sessions else 0
        councilors_list.append({
            'name': c['name'], 'club': c['club'], 'district': None,
            'frekwencja': round(frekwencja, 1), 'aktywnosc': round(aktywnosc, 1),
            'zgodnosc_z_klubem': 0.0,
            'votes_za': c['votes_za'], 'votes_przeciw': c['votes_przeciw'],
            'votes_wstrzymal': c['votes_wstrzymal'], 'votes_brak': c['votes_brak'],
            'votes_nieobecny': c['votes_nieobecny'], 'votes_total': total_votes,
            'rebellion_count': 0, 'rebellions': [], 'has_activity_data': False, 'activity': None,
        })
    vectors = defaultdict(dict)
    for v in all_votes:
        for cat in ('za', 'przeciw', 'wstrzymal_sie'):
            for name in v['named_votes'].get(cat, []):
                vectors[name][v['id']] = cat
    pairs = []
    names_sorted = sorted(vectors.keys())
    for a, b in combinations(names_sorted, 2):
        common = set(vectors[a].keys()) & set(vectors[b].keys())
        if len(common) < 10:
            continue
        same = sum(1 for vid in common if vectors[a][vid] == vectors[b][vid])
        pairs.append({'a': a, 'b': b, 'club_a': CLUB[a], 'club_b': CLUB[b],
                      'score': round(same / len(common) * 100, 1), 'common_votes': len(common)})
    pairs.sort(key=lambda x: x['score'], reverse=True)
    club_counts = Counter(CLUB[n] for n in all_names)
    kad = {
        'id': KADENCJA_ID, 'label': KADENCJA_LABEL, 'clubs': dict(club_counts),
        'sessions': sessions_data, 'total_sessions': total_sessions,
        'total_votes': total_votes, 'total_councilors': len(councilors_list),
        'councilors': councilors_list, 'votes': all_votes,
        'similarity_top': pairs[:20], 'similarity_bottom': pairs[-20:][::-1],
    }
    return {'generated': datetime.now().isoformat(), 'default_kadencja': KADENCJA_ID, 'kadencje': [kad]}


def build_profiles(records):
    cv = defaultdict(lambda: {'za': 0, 'przeciw': 0, 'wstrzymal_sie': 0,
                              'nieobecny': 0, 'brak': 0, 'votes': []})
    for rec in records:
        d = rec.get('session_date')
        if not d or d < KAD_START:
            continue
        for cat, names in rec['named'].items():
            for name in names:
                key = 'za' if cat == 'za' else 'przeciw' if cat == 'przeciw' \
                    else 'wstrzymal_sie' if cat == 'wstrzymal_sie' else 'nieobecny' if cat == 'nieobecni' else 'brak'
                cv[name][key] += 1
                cv[name]['votes'].append({'session': d, 'vote': key})
    profiles = []
    for name in sorted(cv.keys()):
        vd = cv[name]
        total = sum(vd[k] for k in ('za', 'przeciw', 'wstrzymal_sie', 'nieobecny', 'brak')) or 1
        present_sess = len({v['session'] for v in vd['votes'] if v['vote'] != 'nieobecny'})
        all_sess = len({v['session'] for v in vd['votes']})
        frekw = 100.0 * present_sess / all_sess if all_sess else 0.0
        profiles.append({
            'name': name, 'slug': make_slug(name),
            'kadencje': {KADENCJA_ID: {
                'club': CLUB[name], 'has_voting_data': True, 'has_activity_data': False,
                'frekwencja': round(frekw, 1), 'aktywnosc': 0.0, 'zgodnosc_z_klubem': 0.0,
                'votes_za': vd['za'], 'votes_przeciw': vd['przeciw'],
                'votes_wstrzymal': vd['wstrzymal_sie'], 'votes_brak': vd['brak'],
                'votes_nieobecny': vd['nieobecny'], 'votes_total': total,
                'rebellion_count': 0, 'rebellions': [], 'roles': [], 'notes': '',
                'former': False, 'mid_term': False,
            }}
        })
    return {'profiles': profiles}


def save_split(output, out_path, profiles):
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    stubs = []
    for kad in output.get('kadencje', []):
        kid = kad['id']
        stubs.append({'id': kid, 'label': kad.get('label', f'Kadencja {kid}')})
        with open(out_path.parent / f'kadencja-{kid}.json', 'w', encoding='utf-8') as f:
            json.dump(kad, f, ensure_ascii=False, separators=(',', ':'))
    index = {'generated': output.get('generated', ''), 'default_kadencja': output.get('default_kadencja', ''),
             'kadencje': stubs}
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(index, f, ensure_ascii=False, separators=(',', ':'))
    with open(out_path.parent / 'profiles.json', 'w', encoding='utf-8') as f:
        json.dump(profiles, f, ensure_ascii=False, separators=(',', ':'))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--output', required=True)
    ap.add_argument('--profiles', required=True)
    ap.add_argument('--cache-dir', default=None)
    args = ap.parse_args()
    cache_dir = Path(args.cache_dir) if args.cache_dir else None
    print('=== Scraper Rada Miejska Pisz (bip.pisz.hi.pl) ===')
    sessions = collect_sessions(cache_dir)
    print(f'  Artkułów sesji w kategorii: {len(sessions)}')
    records = []
    ok = fail = 0
    votes_total = 0
    unvalidated = 0
    for s in sessions:
        if not s['date_iso'] or s['date_iso'] < KAD_START:
            continue
        if not s['download_id']:
            print(f'    BRAK PDF: {s["title"]}')
            fail += 1
            continue
        try:
            data = fetch(f"{BIP}/download.php?id={s['download_id']}", cache_dir, binary=True)
            _, votes = parse_report_pdf(data)
            if not votes:
                continue
            for pageno, (recs, valid) in enumerate(votes):
                if not recs:
                    continue
                if not valid:
                    unvalidated += 1
                named = {'za': [], 'przeciw': [], 'wstrzymal_sie': [], 'brak': [], 'nieobecni': []}
                for name, vote in recs:
                    if vote in named:
                        named[vote].append(name)
                records.append({'session_date': s['date_iso'], 'session_num': s['roman'] or (pageno + 1),
                                'topic': f'{s["title"]} — głosowanie {pageno + 1}', 'named': named})
                votes_total += 1
            ok += 1
        except Exception as e:
            print(f'    BŁĄD {s["title"]}: {e}')
            fail += 1
    print(f'  Sesje IX kad. z PDF: {ok}, błędy: {fail}, głosowań: {votes_total}, niewalidowanych-agregat: {unvalidated}')
    output = build_output(records)
    profiles = build_profiles(records)
    save_split(output, args.output, profiles)
    kad = output['kadencje'][0]
    print(f'  SESJE: {kad["total_sessions"]}, GŁOSOWANIA: {kad["total_votes"]}, RADNYCH: {kad["total_councilors"]}')
    print('  OK — zapisano data.json / kadencja-2024-2029.json / profiles.json')


if __name__ == '__main__':
    main()
