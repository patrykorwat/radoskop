#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Radoskop Trzebiatów — imienne głosowania Rady Miejskiej w Trzebiatowie (IX kadencja 2024-2029).

Źródło: BIP bip.trzebiatow.pl (platforma gov.pl/eBOI, alias=bip_umtrzebiatow).
Głosowania imienne publikowane per-uchwała: kategorię 'Uchwały' dzielona według lat
(/<cat_2024|2025|2026>), każdy dokument uchwały ma załącznik 'Głosowanie imienne - Uchwała Nr X.pdf'
(czysty tekst, tabela 2-kolumnowa 'Lp | Nazwisko i imię | Głos' x2). Każda uchwała = jeden głos
(głosowanie nad jej przyjęciem). Sesja = grupowanie wg numeru rzymskiego sesji + daty głosowania.

Format PDF (pdfplumber extract_table):
    r0: [nr, '', 'XXVIII sesja Rady Miejskiej w Trzebiatowie IX kadencji', ...]
    r2: [nr, '', '<agenda>. <temat uchwały>', ...]
    r3: Typ głosowania | jawne | Data głosowania: DD.MM.YYYY
    r5-8: agregaty (Liczba uprawnionych/obecnych/nieobecnych, Głosy za/przeciw/wstrzymujące)
    nagłówek: Lp | Nazwisko i imię | '' | Głos | Lp. | Nazwisko i imię | Głos
    wiersze: ['1.','Adamowicz Dorota','','ZA','9.','Pokorski Włodzimierz','ZA']

Użycie: python scrape_trzebiatow.py --output docs/data.json --profiles docs/profiles.json [--cache-dir .cache]
"""
import argparse, io, json, os, re, time
from collections import Counter, defaultdict
from datetime import datetime
from itertools import combinations
from pathlib import Path

import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
try:
    import pdfplumber
except ImportError:
    pdfplumber = None

BASE = "https://bip.trzebiatow.pl"
ALIAS = "?alias=bip_umtrzebiatow"
YEAR_CATS = {"2024": "9786172", "2025": "10041493", "2026": "10523613"}
KAD_START = "2024-05-07"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36"}
_last = 0.0
CACHE = []
CACHE_DIR = None

def _rate(interval=0.5):
    global _last
    el = time.time() - _last
    if el < interval:
        time.sleep(interval - el)
    _last = time.time()

def get(url, binary=False, cache_dir=None):
    global _last
    if binary and cache_dir:
        import hashlib
        key = hashlib.md5(url.encode()).hexdigest()
        cp = Path(cache_dir) / f"{key}.bin"
        if cp.exists():
            return cp.read_bytes()
    _rate()
    for i in range(4):
        try:
            r = requests.get(url, headers=UA, timeout=40, verify=False)
            if r.status_code == 200:
                out = r.content if binary else r.text
                if binary and cache_dir:
                    Path(cache_dir).mkdir(parents=True, exist_ok=True)
                    cp = Path(cache_dir) / f"{hashlib.md5(url.encode()).hexdigest()}.bin"
                    cp.write_bytes(out)
                return out
            if 'alias' not in url:
                r2 = requests.get(url + ('&' if '?' in url else '?') + 'alias=bip_umtrzebiatow',
                                   headers=UA, timeout=40, verify=False)
                if r2.status_code == 200:
                    out = r2.content if binary else r2.text
                    if binary and cache_dir:
                        Path(cache_dir).mkdir(parents=True, exist_ok=True)
                        cp = Path(cache_dir) / f"{hashlib.md5(url.encode()).hexdigest()}.bin"
                        cp.write_bytes(out)
                    return out
        except Exception:
            time.sleep(1 + i)
    return None

def make_slug(name):
    repl = {'ą':'a','ć':'c','ę':'e','ł':'l','ń':'n','ó':'o','ś':'s','ź':'z','ż':'z'}
    sn = str(name or "").lower()
    for pl, a in repl.items():
        sn = sn.replace(pl, a)
    sn = re.sub(r"[^a-z0-9]+", "-", sn)
    return sn.strip("-")

def cat_uchwaly(cat):
    docs = []
    page = 1
    while True:
        t = get(f"{BASE}/{cat}/strona/{page}{ALIAS}")
        if not t:
            break
        seg = t[t.find('<main'):] if '<main' in t else t
        found = re.findall(r'<a[^>]*href="([0-9]+/dokument/[0-9]+)"[^>]*>(.*?)</a>', seg, re.S)
        new = 0
        for href, txt in found:
            txt = re.sub(r'<[^>]+>', '', txt).strip()
            if not txt:
                continue
            if any(h == href for h, _ in docs):
                continue
            docs.append((href, txt))
            new += 1
        if f"/strona/{page+1}" in t or f"strona/{page+1}" in t:
            page += 1
            continue
        break
    return docs

def doc_gi_attachment(doc):
    t = get(f"{BASE}/{doc}")
    if not t:
        return None
    for m in re.finditer(r'href="[^"]*api/download/file\?id=(\d+)"[^>]*>\s*(.*?)\s*</a>', t, re.S):
        lbl = re.sub(r'<[^>]+>', '', m.group(2)).strip()
        if 'Głosowanie imienne' in lbl:
            return m.group(1)
    return None

def parse_vote_pdf(data):
    if pdfplumber is None:
        return None
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            text = "\n".join((p.extract_text() or '') for p in pdf.pages)
            table = pdf.pages[0].extract_table() or []
    except Exception:
        return None
    # session roman
    m = re.search(r'([IVXLCDM]+)\s+sesja Rady Miejskiej w Trzebiatowie', text)
    session = m.group(1) if m else None
    # date
    dm = re.search(r'Data głosowania:\s*(\d{2})\.(\d{2})\.(\d{4})', text)
    date = f"{dm.group(3)}-{dm.group(2)}-{dm.group(1)}" if dm else None
    # topic: text line after 'Głosowanie' (non-numeric, non-header)
    topic = None
    lines = [l.strip() for l in text.split('\n') if l.strip()]
    for i, l in enumerate(lines):
        if l == 'Głosowanie':
            for l2 in lines[i+1:]:
                if not re.match(r'^\s*\d+\s*$', l2) and 'Data głosowania' not in l2 and 'Typ głosowania' not in l2:
                    topic = re.sub(r'^\s*\d+\s*\.?\s*', '', l2).strip()
                    break
            break
    if topic is None:
        for l in lines[1:3]:
            if ('sesja' not in l and l != 'Głosowanie'
                    and not re.match(r'^\s*\d+\s*$', l) and 'Data głosowania' not in l):
                topic = re.sub(r'^\s*\d+\s*\.?\s*', '', l).strip()
                break
    # aggregates
    agg = None
    za_g = re.search(r'Głosy za\s+(\d+)', text)
    pr_g = re.search(r'Głosy przeciw\s+(\d+)', text)
    wz_g = re.search(r'Głosy wstrzymujące(?:\s+się)?\s+(\d+)', text)
    ob_g = re.search(r'Liczba obecnych\s+(\d+)', text)
    nie_g = re.search(r'Liczba nieobecnych\s+(\d+)', text)
    if za_g and pr_g and wz_g:
        agg = {
            'za': int(za_g.group(1)), 'przeciw': int(pr_g.group(1)),
            'wstrzymal_sie': int(wz_g.group(1)),
            'obecni': int(ob_g.group(1)) if ob_g else None,
            'nieobecni': int(nie_g.group(1)) if nie_g else None,
        }
    # per-councilor votes from table rows
    def _votemap(tok):
        tok = (tok or '').strip().upper()
        if tok == 'ZA':
            return 'za'
        if tok.startswith('PRZECIW'):
            return 'przeciw'
        if tok.startswith('WSTRZYM'):
            return 'wstrzymal_sie'
        if tok.startswith('NIEOBEC'):
            return 'nieobecny'
        if tok == 'BRAK' or tok.startswith('BRAK GŁOSU') or 'NIEGŁOSUJĄCY' in tok or 'NIEGLOSUJACY' in tok:
            return 'brak'
        return None
    rows = []
    for row in table:
        # row: [lp, name1, '', vote1, lp2, name2, vote2]
        if not row or len(row) < 4:
            continue
        c = [ (x or '').strip() for x in row ]
        if not re.match(r'^\d+\.$', c[0]):
            continue
        name1 = c[1].strip().replace('\n',' ')
        v1 = _votemap(c[3])
        if name1 and v1:
            rows.append((name1, v1))
        if len(c) >= 7 and re.match(r'^\d+\.$', c[4].strip()) and c[5].strip():
            name2 = c[5].strip().replace('\n',' ')
            v2 = _votemap(c[6])
            if name2 and v2:
                rows.append((name2, v2))
    return {'session': session, 'topic': topic, 'date': date, 'agg': agg, 'rows': rows}

def rev_name(nm):
    # 'Adamowicz Dorota' -> 'Dorota Adamowicz' (Radoskop convention Imię Nazwisko)
    parts = nm.split()
    return ' '.join([parts[-1]] + parts[:-1]) if len(parts) > 1 else nm

def collect_all():
    docs = []
    for yr, cat in YEAR_CATS.items():
        dd = cat_uchwaly(cat)
        print(f"[trzebiatow] {yr}: {len(dd)} uchwały docs", flush=True)
        docs.extend((d, yr) for d in dd)
    records = []
    seen_doc = set()
    stats = {'docs': 0, 'with_gi': 0, 'parsed': 0, 'nogi': 0, 'date_lt_start': 0, 'empty_rows': 0}
    for (href, title), yr in docs:
        stats['docs'] += 1
        gi = doc_gi_attachment(href)
        if not gi:
            stats['nogi'] += 1
            continue
        stats['with_gi'] += 1
        data = get(f"{BASE}/api/download/file?id={gi}", binary=True, cache_dir=CACHE_DIR)
        if not data:
            continue
        p = parse_vote_pdf(data)
        if not p or not p['rows']:
            stats['empty_rows'] += 1
            continue
        stats['parsed'] += 1
        if not p['date'] or p['date'] < KAD_START:
            stats['date_lt_start'] += 1
            continue
        records.append({
            'session': p['session'], 'date': p['date'], 'topic': p['topic'],
            'agg': p['agg'], 'rows': [(rev_name(n), v) for n, v in p['rows']],
        })
    print(f"[trzebiatow] stats: {stats}", flush=True)
    # dedupe identical votes (same session+date+exact topic) just in case
    seen = set()
    dedup = []
    for r in records:
        key = (r['session'], r['date'], (r['topic'] or '').strip())
        if key in seen:
            continue
        seen.add(key)
        dedup.append(r)
    return dedup

def build_output(records):
    all_votes = []; vid = 0
    sessions_by_date = {}
    validated = {'ok':0,'mismatch':0,'noagg':0}
    for rec in records:
        d = rec['date']
        named = {'za':[],'przeciw':[],'wstrzymal_sie':[],'brak':[],'nieobecny':[]}
        for name, v in rec['rows']:
            named[v].append(name)
        for k in named:
            named[k] = list(dict.fromkeys(named[k]))
        za, pr, wz = len(named['za']), len(named['przeciw']), len(named['wstrzymal_sie'])
        if rec['agg'] and rec['agg']['za'] is not None:
            if za == rec['agg']['za'] and pr == rec['agg']['przeciw'] and wz == rec['agg']['wstrzymal_sie']:
                validated['ok'] += 1
            else:
                validated['mismatch'] += 1
                print(f"  [mismatch] sesja {rec['session']} {d} topic={(rec['topic'] or '')[:60]!r} "
                      f"named=({za},{pr},{wz}) agg=({rec['agg']['za']},{rec['agg']['przeciw']},{rec['agg']['wstrzymal_sie']})")
        else:
            validated['noagg'] += 1
        if d not in sessions_by_date:
            sessions_by_date[d] = {'date': d, 'number': rec['session'], 'vote_count': 0, 'attendees': set()}
        vid += 1
        sessions_by_date[d]['vote_count'] += 1
        for k in ('za','przeciw','wstrzymal_sie','nieobecny','brak'):
            sessions_by_date[d]['attendees'].update(named[k])
        all_votes.append({'id': str(vid), 'source_url': '',
            'session_date': d, 'session_number': rec['session'],
            'topic': (rec['topic'] or '').strip(), 'druk': '', 'resolution': '',
            'counts': {'za': za, 'przeciw': pr, 'wstrzymal_sie': wz}, 'named_votes': named})
    sessions_data = []
    for d in sorted(sessions_by_date.keys()):
        s = sessions_by_date[d]
        sessions_data.append({'date': d, 'number': s['number'], 'vote_count': s['vote_count'],
                              'attendee_count': len(s['attendees']), 'attendees': sorted(s['attendees']),
                              'speakers': []})
    all_names = set()
    for v in all_votes:
        for ns in v['named_votes'].values():
            all_names.update(ns)
    cdata = {n: {'name':n,'club':'','votes_za':0,'votes_przeciw':0,'votes_wstrzymal':0,
                 'votes_brak':0,'votes_nieobecny':0} for n in all_names}
    for v in all_votes:
        for cat, ns in v['named_votes'].items():
            for n in ns:
                if n not in cdata: continue
                c = cdata[n]
                if cat=='za': c['votes_za']+=1
                elif cat=='przeciw': c['votes_przeciw']+=1
                elif cat=='wstrzymal_sie': c['votes_wstrzymal']+=1
                else: c['votes_brak']+=1
    total_votes = len(all_votes); total_sessions = len(sessions_data)
    counc_sess = defaultdict(set)
    for v in all_votes:
        for cat, ns in v['named_votes'].items():
            for n in ns:
                counc_sess[n].add(v['session_date'])
    councilors_list = []
    for c in sorted(cdata.values(), key=lambda x: x['name']):
        present = c['votes_za']+c['votes_przeciw']+c['votes_wstrzymal']+c['votes_brak']
        aktywnosc = present/total_votes*100 if total_votes else 0
        frekwencja = len(counc_sess[c['name']])/total_sessions*100 if total_sessions else 0
        councilors_list.append({'name':c['name'],'club':c['club'],'frekwencja':round(frekwencja,1),
            'aktywnosc':round(aktywnosc,1),'zgodnosc_z_klubem':0.0,'votes_za':c['votes_za'],
            'votes_przeciw':c['votes_przeciw'],'votes_wstrzymal':c['votes_wstrzymal'],
            'votes_brak':c['votes_brak'],'votes_nieobecny':c['votes_nieobecny'],
            'votes_total':total_votes,'rebellion_count':0,'rebellions':[],
            'has_activity_data':False,'activity':None})
    vectors = defaultdict(dict)
    for v in all_votes:
        for cat in ('za','przeciw','wstrzymal_sie'):
            for n in v['named_votes'].get(cat, []):
                vectors[n][v['id']] = cat
    pairs = []; names_sorted = sorted(vectors.keys())
    for a, b in combinations(names_sorted, 2):
        common = set(vectors[a].keys()) & set(vectors[b].keys())
        if len(common) < 10: continue
        same = sum(1 for vvid in common if vectors[a][vvid] == vectors[b][vvid])
        pairs.append({'a':a,'b':b,'club_a':'','club_b':'','score':round(same/len(common)*100,1),
                      'common_votes':len(common)})
    pairs.sort(key=lambda x: x['score'], reverse=True)
    kad = {'id':KADENCJA_ID,'label':KADENCJA_LABEL,'clubs':{},'sessions':sessions_data,
           'total_sessions':total_sessions,'total_votes':total_votes,'total_councilors':len(councilors_list),
           'councilors':councilors_list,'votes':all_votes,
           'similarity_top':pairs[:20],'similarity_bottom':pairs[-20:][::-1]}
    return {'generated':datetime.now().isoformat(),'default_kadencja':KADENCJA_ID,'kadencje':[kad]}, validated

def build_profiles(records):
    cv = defaultdict(lambda:{'za':0,'przeciw':0,'wstrzymal_sie':0,'nieobecny':0,'brak':0,'votes':[]})
    for rec in records:
        named = {'za':[],'przeciw':[],'wstrzymal_sie':[],'brak':[],'nieobecny':[]}
        for name, v in rec['rows']:
            named[v].append(name)
        for k in named:
            named[k] = list(dict.fromkeys(named[k]))
        for cat, ns in named.items():
            for n in ns:
                cv[n][cat] += 1
                cv[n]['votes'].append({'session': rec['date'], 'vote': cat})
    profiles = []
    for name in sorted(cv.keys()):
        vd = cv[name]
        total = sum(vd[k] for k in ('za','przeciw','wstrzymal_sie','nieobecny','brak')) or 1
        present_sess = len({v['session'] for v in vd['votes'] if v['vote']!='nieobecny'})
        all_sess = len({v['session'] for v in vd['votes']})
        frekw = 100.0*present_sess/all_sess if all_sess else 0.0
        profiles.append({'name':name,'slug':make_slug(name),'kadencje':{KADENCJA_ID:{
            'club':'','has_voting_data':True,'has_activity_data':False,'frekwencja':round(frekw,1),
            'aktywnosc':0.0,'zgodnosc_z_klubem':0.0,'votes_za':vd['za'],'votes_przeciw':vd['przeciw'],
            'votes_wstrzymal':vd['wstrzymal_sie'],'votes_brak':vd['brak'],'votes_nieobecny':vd['nieobecny'],
            'votes_total':total,'rebellion_count':0,'rebellions':[],'roles':[],'notes':''}}})
    return {'profiles':profiles,'total':len(profiles)}

def save_split(output, out_path, profiles):
    out_path.parent.mkdir(parents=True, exist_ok=True)
    index = {'generated':output.get('generated',''),'default_kadencja':output.get('default_kadencja',''),
             'kadencje':[]}
    for kad in output['kadencje']:
        kid = kad['id']
        with open(out_path.parent/f'kadencja-{kid}.json','w',encoding='utf-8') as f:
            json.dump(kad, f, ensure_ascii=False, separators=(",",":"))
        index['kadencje'].append({'id':kid,'label':kad.get('label','')})
    with open(out_path,'w',encoding='utf-8') as f:
        json.dump(index, f, ensure_ascii=False, separators=(",",":"))
    with open(out_path.parent/'profiles.json','w',encoding='utf-8') as f:
        json.dump(profiles, f, ensure_ascii=False, separators=(",",":"))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--output', required=True)
    ap.add_argument('--profiles', required=True)
    ap.add_argument('--cache-dir', default='.cache')
    args = ap.parse_args()
    global CACHE_DIR
    CACHE_DIR = args.cache_dir
    records = collect_all()
    output, validated = build_output(records)
    k = output['kadencje'][0]
    print(f"[trzebiatow] sesje: {k['total_sessions']}, glosowania: {k['total_votes']}, radni: {k['total_councilors']}")
    print(f"[trzebiatow] walidacja agregatow: ok={validated['ok']} mismatch={validated['mismatch']} noagg={validated['noagg']}")
    profiles = build_profiles(records)
    save_split(output, Path(args.output), profiles)
    # debug: first few sessions
    for s in k['sessions'][:5]:
        print(f"  sesja {s['number']} {s['date']} votes={s['vote_count']}")
    print("[trzebiatow] OK")

if __name__ == '__main__':
    main()
