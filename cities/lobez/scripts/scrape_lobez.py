#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Radoskop Łobez — imienne głosowania Rady Miejskiej w Łobzie (IX kadencja 2024-2029).

Źródło: BIP Urzędu Miejskiego w Łobzie (bip.lobez.pl, platforma 2ClickPortal),
sekcja "Protokoły z sesji" -> lata 2024/2025/2026. Każda sesja IX kad. publikuje
per-vote karty głosowania w ZIP "karty z głosowania ...zip" (lub pojedynczy PDF
"karta z głosowania imiennego"). Każda karta to PDF TEKSTOWY (bez OCR) o formacie:
    NN XXIX sesja Rady Miejskiej w Łobzie    <- nr arabski + rzymski
    Głosowanie
    <n> <temat>
    Typ głosowania jawne Data głosowania: 24.06.2026 09:03
    Liczba uprawnionych 15 Głosy za 14
    Liczba obecnych 14 Głosy przeciw 0
    Liczba nieobecnych 1 Głosy wstrzymujące się 0
    Obecni niegłosujący 0
    Kworum zostało osiągnięte
    Uprawnieni do głosowania
    Lp Nazwisko i imię Głos Lp. Nazwisko i imię Głos
    1. Bartczak Ryszard NIEOBECNY 9. Romejko Wiesława ZA
    ...
Walidacja: count(Za)==Głosy za, count(Przeciw)==Głosy przeciw, count(Wstrzymujący)==Głosy wstrzymujące.

Nazwy źródło podaje "Nazwisko Imię" (kolumna "Nazwisko i imię") — konwertujemy na
konwencję Radoskopa "Imię Nazwisko" (swap pierwszego tokenu = nazwisko).
Kluby: BIP nie publikuje klubów radnych -> kluby NZ (PENDING).

Użycie: python scrape_lobez.py --output docs/data.json --profiles docs/profiles.json
        [--cache-dir .cache]
"""

import argparse
import json
import os
import re
import sys
import time
import zipfile
from collections import Counter, defaultdict
from datetime import datetime
from io import BytesIO
from itertools import combinations
from pathlib import Path

import requests
import urllib3
import pdfplumber
from bs4 import BeautifulSoup

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE = "https://bip.lobez.pl"
YEAR_PAGES = ["https://bip.lobez.pl/1533-2026.html", "https://bip.lobez.pl/1314-2025.html", "https://bip.lobez.pl/rok-2024.html"]
KAD_START = "2024-05-07"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36"}

_ROMAN = {"I":1,"V":5,"X":10,"L":50,"C":100,"D":500,"M":1000}
def _rm(x):
    v=0;prev=0
    for ch in reversed(x.upper()):
        cur=_ROMAN[ch]; v=v-cur if cur<prev else v+cur; prev=cur
    return v

def _date_iso(s):
    m=re.search(r'(\d{1,2})\.(\d{1,2})\.(\d{4})', s)
    if m:
        d,mo,y=m.groups(); return f"{y}-{int(mo):02d}-{int(d):02d}"
    return None

def make_slug(name):
    repl={'ą':'a','ć':'c','ę':'e','ł':'l','ń':'n','ó':'o','ś':'s','ź':'z','ż':'z'}
    sn=str(name or "").lower()
    for pl,a in repl.items(): sn=sn.replace(pl,a)
    sn=re.sub(r"[^a-z0-9]+","-",sn); return sn.strip("-")

def get(url, retries=3, stream=False, timeout=60):
    for i in range(retries):
        try:
            r=requests.get(url, headers=UA, timeout=timeout, verify=False, stream=stream)
            if r.status_code==200: return r
            elif r.status_code in (429,500,503):
                time.sleep(2+i); continue
        except Exception:
            time.sleep(1+i)
    return None

def discover_zip_links():
    """Return {zip_url: (session_title_date_str)} from all year pages."""
    out={}
    for pg in YEAR_PAGES:
        r=get(pg)
        if not r: 
            print(f"  [warn] year page {pg} failed"); continue
        bs=BeautifulSoup(r.text,"lxml")
        for a in bs.find_all('a', href=True):
            h=a['href']
            lh=(h+" "+a.get_text(' ',strip=True)).lower()
            # match kart-y/karta-z-glosowania zips AND single karta PDFs (incl. misspelled 'katy')
            if 'file_add/download' in h:
                fn=os.path.basename(h.split('download/')[-1]).lower()
                is_zip=fn.endswith('.zip')
                is_single_pdf=fn.endswith('.pdf') and ('karta' in fn or 'katy' in fn or 'glosowania' in fn or 'gloswania' in fn)
                if (is_zip and ('glos' in fn or 'kart' in fn or 'katy' in fn)) or is_single_pdf:
                    full = h if h.startswith('http') else (BASE+h if h.startswith('/') else BASE+'/'+h)
                    title=a.get_text(' ',strip=True)
                    key=full.split('download/')[-1].split('?')[0]
                    out[full]=(title, key)
    return out

def iter_card_pdfs(zip_url):
    """Yield (pdf_filename, pdfbytes) for every card in a karty-zip (or single PDF)."""
    if zip_url.lower().endswith('.zip'):
        r=get(zip_url)
        if not r: return
        try:
            z=zipfile.ZipFile(BytesIO(r.content))
            for n in sorted(z.namelist()):
                if n.lower().endswith('.pdf'):
                    yield os.path.basename(n), z.read(n)
        except Exception as e:
            print(f"  [zip-err] {zip_url}: {e}")
    else:
        r=get(zip_url)
        if r:
            yield os.path.basename(zip_url), r.content

def parse_card(text):
    """Return dict with session roman/num, date, topic, rows [(name_imi_nazw, vote)], agg counts.
    Returns None if not a parseable imienne card."""
    lines=[l.strip() for l in text.split('\n') if l.strip()]
    if not lines: return None
    # session marker: "29 XXIX sesja Rady Miejskiej w Łobzie" OR "1 Pierwsza sesja..." (session 1)
    num=roman=None
    m=re.match(r'^(\d+)\s+(?:([IVXLCDM]+)\s+)?[Ss]\w*esja', lines[0])
    if m:
        num=int(m.group(1)); roman=m.group(2) or f"{m.group(1)}"
    else:
        m2=re.match(r'^(\d+)\s+', lines[0])
        if m2: num=int(m2.group(1)); roman=f"{num}"
    if num is None: return None
    # date
    d=None
    for l in lines:
        mm=re.search(r'Data\s+głosowania:\s*([\d.]+)', l)
        if mm: d=_date_iso(mm.group(1)); break
    # topic: line(s) between 'Głosowanie' and 'Typ głosowania'
    topic=""; in_topic=False
    for l in lines:
        if l=='Głosowanie' or l.startswith('Głosowanie'):
            in_topic=True; topic=""; continue
        if 'Typ głosowania' in l: in_topic=False; break
        if in_topic:
            topic=(topic+' '+l).strip()
    # aggregate counts
    agg={'za':None,'przeciw':None,'wstrzymal':None,'uprawnieni':None,'obecni':None,'nieobecni':None}
    for l in lines:
        mm=re.search(r'Liczba\s+uprawnionych\s+(\d+)\s+Głosy\s+za\s+(\d+)', l)
        if mm: agg['uprawnieni']=int(mm.group(1)); agg['za']=int(mm.group(2))
        mm=re.search(r'Liczba\s+obecnych\s+(\d+)\s+Głosy\s+przeciw\s+(\d+)', l)
        if mm: agg['obecni']=int(mm.group(1)); agg['przeciw']=int(mm.group(2))
        mm=re.search(r'Liczba\s+nieobecnych\s+(\d+)\s+Głosy\s+wstrzymujące\s+się\s+(\d+)', l) or \
           re.search(r'Liczba\s+nieobecnych\s+(\d+)\s+Głosy\s+wstrzymujące\s+(\d+)', l)
        if mm: agg['nieobecni']=int(mm.group(1)); agg['wstrzymal']=int(mm.group(2))
    # rows: "1. Bartczak Ryszard NIEOBECNY 9. Romejko Wiesława ZA"
    rows=[]
    # split on vote tokens (multi-token WSTRZYMUJĘ SIĘ handled by its own branch)
    vre=r'(ZA|PRZECIW|WSTRZYMUJĘ\s+SIĘ|WSTRZYMUJĄCY|WSTRZYMUJĄCA|NIEOBECNY|NIEOBECNA|OBECNY|OBECNA|NIE\s*G[ŁL]OSOWA[ŁL])'
    in_table=False
    for l in lines:
        if l.startswith('Uprawnieni do głosowania'):
            in_table=True; continue
        if in_table:
            if 'Wydrukowano' in l or 'Lp Nazwisko' in l: 
                if 'Wydrukowano' in l: in_table=False
                continue
            if 'Lp' in l and ('Nazwisko' in l or 'Głos' in l): continue
            # split by numbered entries
            # each line may have multiple "X. Name VOTE" entries
            parts=re.split(r'(?=\d+\.\s)', l)
            for p in parts:
                pm=re.match(r'(\d+)\.\s+(.+?)\s+('+vre+r')$', p.strip())
                if pm:
                    name=pm.group(2).strip(); vote=pm.group(3)
                    rows.append((name, vote))
            if not parts or not re.search(r'\)?\d+\.', l):
                # maybe table continued but no vote -> skip
                pass
        else:
            # detect imienne table even without header (some 2024 cards)
            pm=re.match(r'(\d+)\.\s+(.+?)\s+('+vre+r')$', l)
            if pm:
                rows.append((pm.group(2).strip(), pm.group(3)))
    if not rows:
        return None
    return {'num':num,'roman':roman,'date':d,'topic':topic,'rows':rows,'agg':agg}

VOTE_MAP={'ZA':'za','PRZECIW':'przeciw','WSTRZYMUJĘ SIĘ':'wstrzymal_sie','WSTRZYMUJĄCY':'wstrzymal_sie',
          'WSTRZYMUJĄCA':'wstrzymal_sie','NIEOBECNY':'nieobecny','NIEOBECNA':'nieobecny',
          'OBECNY':'brak','OBECNA':'brak','NIE GŁOSOWAŁ':'brak','NIE GŁOSOWAŁA':'brak'}

def name_to_radoskop(nm):
    """'Nazwisko Imię' -> 'Imię Nazwisko' (swap first token)."""
    parts=nm.split()
    if len(parts)>=2:
        return (' '.join(parts[1:])).strip()+' '+parts[0]
    return nm

def collect_all(cache_dir):
    zips=discover_zip_links()
    print(f"[lobez] znaleziono {len(zips)} plikow karty-glosowania")
    records=[]  # list of card dicts
    seen_topic_in_zip={}  # (zip_key, num) -> session date to dedupe double-listed zips
    cache=Path(cache_dir); cache.mkdir(parents=True, exist_ok=True)
    for zi,(title,key) in sorted(zips.items(), key=lambda x:x[1][1]):
        cardn=0
        for fn, data in iter_card_pdfs(zi):
            # cache
            cf=cache/(key.replace('/','_')+'_'+fn)
            if cf.exists():
                text=cf.read_text(encoding='utf-8',errors='replace')
            else:
                try:
                    with pdfplumber.open(BytesIO(data)) as pdf:
                        text="".join((p.extract_text() or '')+'\n' for p in pdf.pages)
                except Exception as e:
                    print(f"  [pdf-err] {fn}: {e}"); continue
                try: cf.write_text(text, encoding='utf-8')
                except Exception: pass
            card=parse_card(text)
            if not card:
                continue
            if not card['date'] or card['date']<KAD_START:
                continue
            card['cardsource']=key
            records.append(card); cardn+=1
        if cardn:
            print(f"  [ok] {key[:50]} -> {cardn} kart", flush=True)
    # dedupe identical (date, num, topic-position) — some zips listed on 2 year pages
    dedup={}
    for rc in records:
        dk=(rc['date'], rc['num'], rc['topic'])
        if dk not in dedup:
            dedup[dk]=rc
    records=list(dedup.values())
    records.sort(key=lambda r:(r['date'] or '', r['num']))
    return records

def build_output(records):
    all_votes=[]; vid=0; sessions_by_date={}; validated={'ok':0,'mismatch':0,'noagg':0,'emptytable':0}
    for rec in records:
        d=rec['date']
        if not d or d<KAD_START: continue
        if not rec['rows']:
            validated['emptytable']+=1; continue
        named={'za':[],'przeciw':[],'wstrzymal_sie':[],'brak':[],'nieobecny':[]}
        for name_raw,v in rec['rows']:
            nm=name_to_radoskop(name_raw)
            key=VOTE_MAP.get(v,'brak')
            named[key].append(nm)
        for k in named: named[k]=list(dict.fromkeys(named[k]))
        za=len(named['za']); pr=len(named['przeciw']); wz=len(named['wstrzymal_sie'])
        ag=rec.get('agg') or {}
        ok=True
        if ag.get('za') is not None and ag.get('przeciw') is not None and ag.get('wstrzymal') is not None:
            if za==ag['za'] and pr==ag['przeciw'] and wz==ag['wstrzymal']:
                validated['ok']+=1
            else:
                validated['mismatch']+=1; ok=False
        else:
            validated['noagg']+=1
        if d not in sessions_by_date:
            sessions_by_date[d]={'date':d,'number':str(rec['num'] or ''),'vote_count':0,'attendees':set()}
        vid+=1
        sessions_by_date[d]['vote_count']+=1
        for k in ('za','przeciw','wstrzymal_sie','nieobecny','brak'):
            sessions_by_date[d]['attendees'].update(named[k])
        all_votes.append({
            'id':str(vid),'source_url':'',
            'session_date':d,'session_number':str(rec.get('num') or ''),
            'topic':rec['topic'].strip() or 'Głosowanie',
            'druk':'','resolution':'',
            'counts':{'za':za,'przeciw':pr,'wstrzymal_sie':wz},
            'named_votes':named,
        })
    sessions_data=[]
    for d in sorted(sessions_by_date.keys()):
        s=sessions_by_date[d]
        sessions_data.append({'date':d,'number':s['number'],'vote_count':s['vote_count'],
                              'attendee_count':len(s['attendees']),'attendees':sorted(s['attendees']),'speakers':[]})
    all_names=set()
    for v in all_votes:
        for ns in v['named_votes'].values(): all_names.update(ns)
    cdata={n:{'name':n,'club':'','votes_za':0,'votes_przeciw':0,'votes_wstrzymal':0,'votes_brak':0,'votes_nieobecny':0} for n in all_names}
    for v in all_votes:
        for cat,ns in v['named_votes'].items():
            for n in ns:
                if n not in cdata: continue
                c=cdata[n]
                if cat=='za': c['votes_za']+=1
                elif cat=='przeciw': c['votes_przeciw']+=1
                elif cat=='wstrzymal_sie': c['votes_wstrzymal']+=1
                else: c['votes_brak']+=1
    total_votes=len(all_votes); total_sessions=len(sessions_data)
    counc_sess=defaultdict(set)
    for v in all_votes:
        for cat,ns in v['named_votes'].items():
            for n in ns: counc_sess[n].add(v['session_date'])
    councilors_list=[]
    for c in sorted(cdata.values(), key=lambda x:x['name']):
        present=c['votes_za']+c['votes_przeciw']+c['votes_wstrzymal']+c['votes_brak']
        aktywnosc=present/total_votes*100 if total_votes else 0
        frekwencja=len(counc_sess[c['name']])/total_sessions*100 if total_sessions else 0
        councilors_list.append({'name':c['name'],'club':c['club'],'frekwencja':round(frekwencja,1),
            'aktywnosc':round(aktywnosc,1),'zgodnosc_z_klubem':0.0,'votes_za':c['votes_za'],
            'votes_przeciw':c['votes_przeciw'],'votes_wstrzymal':c['votes_wstrzymal'],
            'votes_brak':c['votes_brak'],'votes_nieobecny':c['votes_nieobecny'],'votes_total':total_votes,
            'rebellion_count':0,'rebellions':[],'has_activity_data':False,'activity':None})
    vectors=defaultdict(dict)
    for v in all_votes:
        for cat in ('za','przeciw','wstrzymal_sie'):
            for n in v['named_votes'].get(cat,[]): vectors[n][v['id']]=cat
    pairs=[]; names_sorted=sorted(vectors.keys())
    for a,b in combinations(names_sorted,2):
        common=set(vectors[a].keys())&set(vectors[b].keys())
        if len(common)<10: continue
        same=sum(1 for v in common if vectors[a][v]==vectors[b][v])
        pairs.append({'a':a,'b':b,'club_a':'','club_b':'','score':round(same/len(common)*100,1),'common_votes':len(common)})
    pairs.sort(key=lambda x:x['score'],reverse=True)
    kad={'id':KADENCJA_ID,'label':KADENCJA_LABEL,'clubs':{},'sessions':sessions_data,
         'total_sessions':total_sessions,'total_votes':total_votes,'total_councilors':len(councilors_list),
         'councilors':councilors_list,'votes':all_votes,
         'similarity_top':pairs[:20],'similarity_bottom':pairs[-20:][::-1]}
    return {'generated':datetime.now().isoformat(),'default_kadencja':KADENCJA_ID,'kadencje':[kad]}, validated

def build_profiles(records):
    cv=defaultdict(lambda:{'za':0,'przeciw':0,'wstrzymal_sie':0,'nieobecny':0,'brak':0,'votes':[]})
    for rec in records:
        d=rec.get('date')
        if not d or d<KAD_START: continue
        if not rec.get('rows'): continue
        named={'za':[],'przeciw':[],'wstrzymal_sie':[],'brak':[],'nieobecny':[]}
        for name_raw,v in rec['rows']:
            nm=name_to_radoskop(name_raw)
            named[VOTE_MAP.get(v,'brak')].append(nm)
        for k in named: named[k]=list(dict.fromkeys(named[k]))
        for cat,ns in named.items():
            for n in ns:
                cv[n][cat]+=1; cv[n]['votes'].append({'session':d,'vote':cat})
    profiles=[]
    total_all_votes = max(len({(r['date'],r['num'],r['topic']) for r in records if r.get('date') and r['date']>=KAD_START}), 1)
    for name in sorted(cv.keys()):
        vd=cv[name]
        total=sum(vd[k] for k in ('za','przeciw','wstrzymal_sie','nieobecny','brak')) or 1
        present_sess=len({v['session'] for v in vd['votes'] if v['vote']!='nieobecny'})
        all_sess=len({v['session'] for v in vd['votes']})
        frekw=100.0*present_sess/all_sess if all_sess else 0.0
        aktywnosc=100.0*(vd['za']+vd['przeciw']+vd['wstrzymal_sie'])/total_all_votes if total_all_votes else 0.0
        profiles.append({'name':name,'slug':make_slug(name),'kadencje':{KADENCJA_ID:{
            'club':'','has_voting_data':True,'has_activity_data':False,'frekwencja':round(frekw,1),
            'aktywnosc':round(aktywnosc,1), 'zgodnosc_z_klubem':0.0,
            'votes_za':vd['za'],'votes_przeciw':vd['przeciw'],
            'votes_wstrzymal':vd['wstrzymal_sie'],'votes_brak':vd['brak'],'votes_nieobecny':vd['nieobecny'],
            'votes_total':total,'rebellion_count':0,'rebellions':[],'roles':[],'notes':''}}})
    return {'profiles':profiles,'total':len(profiles)}

def save_split(output, out_path, profiles):
    out_path.parent.mkdir(parents=True,exist_ok=True)
    index={'generated':output.get('generated',''),'default_kadencja':output.get('default_kadencja',''),'kadencje':[]}
    for kad in output['kadencje']:
        kid=kad['id']
        with open(out_path.parent/f'kadencja-{kid}.json','w',encoding='utf-8') as f:
            json.dump(kad,f,ensure_ascii=False,separators=(",",":"))
        index['kadencje'].append({'id':kid,'label':kad.get('label','')})
    with open(out_path,'w',encoding='utf-8') as f:
        json.dump(index,f,ensure_ascii=False,separators=(",",":"))
    with open(out_path.parent/'profiles.json','w',encoding='utf-8') as f:
        json.dump(profiles,f,ensure_ascii=False,separators=(",",":"))

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--output',required=True); ap.add_argument('--profiles',required=True)
    ap.add_argument('--cache-dir',default='.cache'); args=ap.parse_args()
    records=collect_all(args.cache_dir)
    dedup={}
    for rc in records:
        dedup[(rc['date'],rc['num'],rc['topic'])]=rc
    records=list(dedup.values())
    output,validated=build_output(records)
    k=output['kadencje'][0]
    print(f"[lobez] sesje: {k['total_sessions']}, glosowania: {k['total_votes']}, radni: {k['total_councilors']}")
    print(f"[lobez] walidacja: ok={validated['ok']} mismatch={validated['mismatch']} noagg={validated['noagg']} emptytable={validated['emptytable']}")
    profiles=build_profiles(records)
    save_split(output, Path(args.output), profiles)
    print("[lobez] OK")

if __name__=='__main__':
    main()
