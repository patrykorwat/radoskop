#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Radoskop Staszów — imienne głosowania Rady Miejskiej w Staszowie (IX kadencja 2024-2029).

Źródło: BIP bip.staszow.pl (Joomla, com_content), kategoria
"Rada Miejska → Kadencja Rady Miejskiej 2024-2029 → Głosowania imienne" (catid=1233).
Każda sesja = artykuł "Wykaz imienny głosowań z N Sesji Rady Miejskiej w Staszowie" z
załącznikiem PDF (SKAN, bez warstwy tekstowej) pod /pliki/{rok}/materialy_z_sesji/{data} {N}.pdf
— data sesji jest w nazwie pliku.

Format PDF (zweryfikowany 2026-08, OCR tesseract -l pol --psm 6, 200 DPI):
    Staszów, dn.: 17 czerwiec 2025r
    1 Głosowanie w sprawie zatwierdzenia porządku obrad sesji.
    GŁOSOWAŁO: 20
    głosowało ZA: 20
    głosowało PRZECIW: 0
    WSTRZYMAŁO się: 0
    LP. Nazwisko i Imię jak głosował
    1. ALTENBERG Stanisław głosował ZA
    2. BEDNARCZYK Kryspin nie głosował
    ...
    <następny punkt: N Głosowanie w sprawie ...>

To format "tekstowy" (token głosu w tej samej linii co nazwisko — NIE położeniowe
X-w-kolumnach, więc parser OCR jest niezawodny). Nazwiska źródło podaje jako
"Nazwisko Imię" (LASTNAME firstname) -> konwencja Radoskopa "Imię Nazwisko"
(pierwszy token przenoszony na koniec, wg precedensu gryfice/miedzyrzecz).

Agregat (GŁOSOWAŁO / za / przeciw / wstrzymało) walidacyjnie porównywany z tabelą imienną.
NIE uruchamiać tesseracta równolegle (pułapka OCR — CPU bound, robi się wolniej/błędy).
Użycie:
    python scrape_staszow.py --output docs/data.json --profiles docs/profiles.json [--cache-dir DIR]
"""

import argparse
import json
import re
import subprocess
import time
import urllib.request
import ssl
from collections import Counter, defaultdict
from datetime import datetime
from itertools import combinations
from pathlib import Path

import pdfplumber
from bs4 import BeautifulSoup

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE
_UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36"}

BIP = "https://bip.staszow.pl"
CAT = "https://bip.staszow.pl/index.php?option=com_content&view=category&id=1233&Itemid=721"
KAD_START = "2024-05-07"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"

_ROMAN = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}


def roman_int(rn):
    rn = rn.upper()
    val = 0
    prev = 0
    for ch in reversed(rn):
        cur = _ROMAN[ch]
        val = val - cur if cur < prev else val + cur
        prev = cur
    return val


def get(url):
    req = urllib.request.Request(url, headers=_UA)
    for i in range(4):
        try:
            return urllib.request.urlopen(req, timeout=60, context=_CTX).read()
        except Exception:
            time.sleep(1.5 + i)
    return None


def list_sessions():
    """Fetch category 1233 -> list of {num, date, pdf_url}."""
    html = get(CAT)
    soup = BeautifulSoup(html.decode('utf-8', 'replace'), 'lxml')
    sessions = {}
    for a in soup.find_all('a', href=True):
        href = a['href']
        t = a.get_text(' ', strip=True)
        m = re.match(r'Wykaz imienny głosowań z ([IXVLCDM]+) Sesji', t)
        if not m or 'catid=1233' not in href:
            continue
        num = roman_int(m.group(1))
        ah = get('https://bip.staszow.pl' + href)
        if not ah:
            continue
        asoup = BeautifulSoup(ah.decode('utf-8', 'replace'), 'lxml')
        link = None
        for aa in asoup.find_all('a', href=True):
            if '.pdf' in aa['href'].lower():
                link = aa['href'] if aa['href'].startswith('http') else 'https://bip.staszow.pl' + aa['href']
                break
        if not link:
            continue
        mdate = re.search(r'/(\d{4}-\d{2}-\d{2})\s*%20?\s*', link) or re.search(r'/(\d{4}-\d{2}-\d{2})', link)
        date = mdate.group(1) if mdate else None
        sessions[num] = {'num': num, 'date': date, 'pdf_url': link}
    return sessions


# ---------- OCR ----------
def ocr_pdf(pdf_path, cache_dir):
    """OCR a scanned PDF to text; cached. Returns full text."""
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    co = cache_dir / (pdf_path.stem + '.txt')
    if co.exists():
        return co.read_text(encoding='utf-8', errors='replace')
    pdf = pdfplumber.open(pdf_path)
    chunks = []
    for i, page in enumerate(pdf.pages):
        png = cache_dir / (pdf_path.stem + f'_p{i+1}.png')
        page.to_image(resolution=300).save(str(png))
        r = subprocess.run(['tesseract', str(png), '-', '-l', 'pol', '--psm', '6'],
                           capture_output=True, text=True)
        chunks.append(r.stdout or '')
        png.unlink(missing_ok=True)
    text = '\n'.join(chunks)
    co.write_text(text, encoding='utf-8')
    return text


# ---------- Parsing ----------
def parse_session(text, session_date):
    """Parse OCR text into per-vote records. Returns (records, stats)."""
    lines = [l.strip() for l in text.splitlines()]
    records = []
    cur = None
    stats = {'votes': 0, 'empty': 0}
    VOTE_TOKEN = re.compile(r'(?P<v>głosował\s+ZA|głosował\s+PRZECIW|głosował\s+[Ww]strzym|nie głosował|głosował\s+WSTRZYM[^\n]*)')
    for ln in lines:
        if not ln:
            continue
        # vote block header: "<n> Głosowanie ..."
        mh = re.match(r'^(\d{1,3})\s+(Głosowanie\b.*)$', ln)
        if mh:
            if cur and not cur['rows']:
                stats['empty'] += 1
            cur = {'topic': mh.group(2), 'agg': None, 'rows': []}
            records.append(cur)
            stats['votes'] += 1
            continue
        if cur is None:
            continue
        # aggregate
        mag = re.match(r'^(?:GŁOSOWAŁO|Glosowalo)\s*:?\s*(\d+)', ln)
        if mag:
            cur['agg_total'] = int(mag.group(1))
            continue
        mza = re.match(r'^(?:głosowało|glosowalo)\s*ZA\s*:?\s*(\d+)', ln, re.I)
        if mza:
            cur['agg_za'] = int(mza.group(1)); continue
        mpr = re.match(r'^(?:głosowało|glosowalo)\s*PRZECIW\s*:?\s*(\d+)', ln, re.I)
        if mpr:
            cur['agg_przeciw'] = int(mpr.group(1)); continue
        mwz = re.match(r'^(?:WSTRZYMAŁO|Wstrzymalo)\s+się\s*:?\s*(\d+)', ln, re.I)
        if mwz:
            cur['agg_wstrzym'] = int(mwz.group(1)); continue
        if ln.lower().startswith(('lp.', 'lp ', 'lp. nazwisko')):
            continue
        # councillor row: "N. NAME <votetoken>"
        # vote token diacritic-tolerant: głosował/głosowa/glosowal/giosowal + ZA/PRZECIW/WSTRZYM...
        # or "nie głosował" (+ variants)
        mvn = re.match(
            r'^(\d{1,3})[\.\)]?\s+(.+?)\s+'
            r'(nie\s*g[łl]?[oó]sowa[łl]?|'
            r'g[łl]?[oó]sowa[łl]?\s+(?:ZA|PRZECIW|WSTRZYM[^\s]*))\s*$', ln, re.I)
        if mvn:
            name = mvn.group(2).strip()
            vote = mvn.group(3).strip().lower()
            cur['rows'].append((name, vote))
            continue
        # otherwise ignore (wrapped/OCR noise line)
    if cur and not cur['rows']:
        stats['empty'] += 1
    # trim records with no rows
    records = [r for r in records if r.get('rows')]
    return records, stats


def vote_key(v):
    v = v.lower()
    if 'za' in v:
        return 'za'
    if 'przeciw' in v:
        return 'przeciw'
    if 'wstrzym' in v:
        return 'wstrzymal_sie'
    return 'brak'


def reverse_name(name):
    # strip stray leading/trailing punctuation/symbols (OCR noise: "_", "."), collapse spaces
    name = name.strip(' \t._-,')
    name = re.sub(r'\s+', ' ', name)
    toks = name.split()
    if len(toks) >= 2:
        return ' '.join(toks[1:] + [toks[0]])
    return name


def make_slug(name):
    repl = {'ą': 'a', 'ć': 'c', 'ę': 'e', 'ł': 'l', 'ń': 'n', 'ó': 'o', 'ś': 's', 'ź': 'z', 'ż': 'z'}
    sn = str(name or '').lower()
    for pl, a in repl.items():
        sn = sn.replace(pl, a)
    sn = re.sub(r'[^a-z0-9]+', '-', sn)
    return sn.strip('-')


def collect_all(cache_dir):
    Path(cache_dir).mkdir(parents=True, exist_ok=True)
    sessions = list_sessions()
    ix = {n: s for n, s in sessions.items() if s['date'] and s['date'] >= KAD_START}
    results = []
    validated = {'ok': 0, 'mismatch': 0, 'noagg': 0}
    for num in sorted(ix):
        s = ix[num]
        data = get(s['pdf_url'])
        if not data:
            print(f'[staszow] sesja {num}: download FAIL')
            continue
        pdf_path = Path(cache_dir) / f'staszow_{num}_{s["date"]}.pdf'
        pdf_path.write_bytes(data)
        try:
            text = ocr_pdf(pdf_path, cache_dir)
        except Exception as e:
            print(f'[staszow] sesja {num}: OCR FAIL {e}')
            continue
        recs, st = parse_session(text, s['date'])
        sess_records = []
        for r in recs:
            za = sum(1 for _, v in r['rows'] if vote_key(v) == 'za')
            pr = sum(1 for _, v in r['rows'] if vote_key(v) == 'przeciw')
            wz = sum(1 for _, v in r['rows'] if vote_key(v) == 'wstrzymal_sie')
            agg = (r.get('agg_total'), r.get('agg_za'), r.get('agg_przeciw'), r.get('agg_wstrzym'))
            if agg[0] is not None:
                if agg[1] == za and agg[2] == pr and agg[3] == wz:
                    validated['ok'] += 1
                else:
                    validated['mismatch'] += 1
            else:
                validated['noagg'] += 1
            sess_records.append({
                'session_date': s['date'], 'session_num': num,
                'topic': r['topic'], 'agg': agg,
                'named': {'za': [reverse_name(n) for n, v in r['rows'] if vote_key(v) == 'za'],
                          'przeciw': [reverse_name(n) for n, v in r['rows'] if vote_key(v) == 'przeciw'],
                          'wstrzymal_sie': [reverse_name(n) for n, v in r['rows'] if vote_key(v) == 'wstrzymal_sie'],
                          'brak': [reverse_name(n) for n, v in r['rows'] if vote_key(v) == 'brak']},
                'counts': {'za': za, 'przeciw': pr, 'wstrzymal_sie': wz},
            })
        if sess_records:
            results.append({'num': num, 'date': s['date'], 'records': sess_records})
        print(f'[staszow] sesja {num} ({s["date"]}): {len(sess_records)} glosowan')
    return results, validated


def build_output(results, validated):
    all_votes = []
    vid = 0
    sessions_by_date = {}
    for sess in results:
        d = sess['date']
        for rec in sess['records']:
            if d not in sessions_by_date:
                sessions_by_date[d] = {'date': d, 'number': str(sess['num']), 'vote_count': 0, 'attendees': set()}
            vid += 1
            sessions_by_date[d]['vote_count'] += 1
            for k, names in rec['named'].items():
                sessions_by_date[d]['attendees'].update(names)
            all_votes.append({
                'id': str(vid), 'session_date': d, 'session_number': str(sess['num']),
                'topic': rec['topic'], 'named_votes': rec['named'], 'counts': rec['counts'],
            })
    sessions_data = []
    for d in sorted(sessions_by_date.keys()):
        s = sessions_by_date[d]
        sessions_data.append({'date': d, 'number': s['number'], 'vote_count': s['vote_count'],
                              'attendee_count': len(s['attendees']),
                              'attendees': sorted(s['attendees']), 'speakers': []})

    all_names = set()
    for v in all_votes:
        for names in v['named_votes'].values():
            all_names.update(names)
    councilors_data = {n: {'name': n, 'club': '', 'votes_za': 0, 'votes_przeciw': 0,
                           'votes_wstrzymal': 0, 'votes_brak': 0, 'votes_nieobecny': 0}
                       for n in all_names}
    for v in all_votes:
        for cat, names in v['named_votes'].items():
            for n in names:
                c = councilors_data.get(n)
                if c is None:
                    continue
                c['votes_' + {'za': 'za', 'przeciw': 'przeciw', 'wstrzymal_sie': 'wstrzymal',
                              'brak': 'brak'}.get(cat, cat)] += 1

    total_votes = len(all_votes)
    total_sessions = len(sessions_data)
    councillor_sess = defaultdict(set)
    for v in all_votes:
        for cat, names in v['named_votes'].items():
            for n in names:
                councillor_sess[n].add(v['session_date'])

    councilors_list = []
    for c in sorted(councilors_data.values(), key=lambda x: x['name']):
        present = c['votes_za'] + c['votes_przeciw'] + c['votes_wstrzymal'] + c['votes_brak']
        aktywnosc = present / total_votes * 100 if total_votes else 0
        frekwencja = len(councillor_sess[c['name']]) / total_sessions * 100 if total_sessions else 0
        councilors_list.append({
            'name': c['name'], 'club': c['club'],
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
        pairs.append({'a': a, 'b': b, 'club_a': '', 'club_b': '',
                      'score': round(same / len(common) * 100, 1), 'common_votes': len(common)})
    pairs.sort(key=lambda x: x['score'], reverse=True)
    kad = {
        'id': KADENCJA_ID, 'label': KADENCJA_LABEL, 'clubs': {},
        'sessions': sessions_data, 'total_sessions': total_sessions,
        'total_votes': total_votes, 'total_councilors': len(councilors_list),
        'councilors': councilors_list, 'votes': all_votes,
        'similarity_top': pairs[:20], 'similarity_bottom': pairs[-20:][::-1],
    }
    return {'generated': datetime.now().isoformat(), 'default_kadencja': KADENCJA_ID,
            'kadencje': [kad]}, validated


def build_profiles(results):
    cv = defaultdict(lambda: {'za': 0, 'przeciw': 0, 'wstrzymal_sie': 0, 'brak': 0, 'votes': []})
    for sess in results:
        d = sess['date']
        for rec in sess['records']:
            for cat, names in rec['named'].items():
                for n in names:
                    cv[n][cat] += 1
                    cv[n]['votes'].append({'session': d, 'vote': cat})
    profiles = []
    for name in sorted(cv.keys()):
        vd = cv[name]
        total = sum(vd[k] for k in ('za', 'przeciw', 'wstrzymal_sie', 'brak')) or 1
        present_sess = len({v['session'] for v in vd['votes']})
        all_sess = present_sess
        frekw = 100.0 * present_sess / all_sess if all_sess else 0.0
        profiles.append({
            'name': name, 'slug': make_slug(name),
            'kadencje': {KADENCJA_ID: {
                'club': '', 'has_voting_data': True, 'has_activity_data': False,
                'frekwencja': round(frekw, 1), 'aktywnosc': 0.0, 'zgodnosc_z_klubem': 0.0,
                'votes_za': vd['za'], 'votes_przeciw': vd['przeciw'],
                'votes_wstrzymal': vd['wstrzymal_sie'], 'votes_brak': vd['brak'],
                'votes_nieobecny': 0, 'votes_total': total,
                'rebellion_count': 0, 'rebellions': [], 'roles': [], 'notes': '',
            }},
        })
    return {'profiles': profiles}


def save_split(output, out_path, profiles):
    out_path.parent.mkdir(parents=True, exist_ok=True)
    index = {'generated': output.get('generated', ''), 'default_kadencja': output.get('default_kadencja', ''),
             'kadencje': []}
    for kad in output['kadencje']:
        kid = kad['id']
        with open(out_path.parent / f'kadencja-{kid}.json', 'w', encoding='utf-8') as f:
            json.dump(kad, f, ensure_ascii=False, separators=(',', ':'))
        index['kadencje'].append({'id': kad['id'], 'label': kad.get('label', '')})
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(index, f, ensure_ascii=False, separators=(',', ':'))
    with open(out_path.parent / 'profiles.json', 'w', encoding='utf-8') as f:
        json.dump(profiles, f, ensure_ascii=False, separators=(',', ':'))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--output', required=True)
    ap.add_argument('--profiles', required=True)
    ap.add_argument('--cache-dir', default='.cache')
    args = ap.parse_args()

    results, validated = collect_all(args.cache_dir)
    output, validated = build_output(results, validated)
    k = output['kadencje'][0]
    print(f"[staszow] sesje: {k['total_sessions']}, glosowania: {k['total_votes']}, radni: {k['total_councilors']}")
    print(f"[staszow] walidacja: ok={validated['ok']} mismatch={validated['mismatch']} noagg={validated['noagg']}")
    profiles = build_profiles(results)
    save_split(output, Path(args.output), profiles)
    print('[staszow] OK')


if __name__ == '__main__':
    main()
