#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Radoskop Morąg — imienne głosowania Rady Miejskiej w Morągu (IX kadencja 2024-2029).

Źródło: BIP Urzędu Miejskiego w Morągu (bip.morag.pl), kategoria
"/10061/Glosowanie_imienne/" → per-sesja PDF "Głosowanie imienne z N sesji".
PDF-y są TEKSTOWE (bez OCR) i mają jednolitą tabelę imienną:
    Oddane głosy - podsumowanie szczegółowe
    Lp. | Imię i nazwisko | Głos (Za/Przeciw/Wstrzymał się/Nieobecny) | Data
plus zagregowane podsumowanie zbiorcze (Uprawnionych/Zagłosowało/Nieobecni/
Za/Przeciw/Wstrzymało się) do walidacji per-głosowanie.

Sesje IX kad. publikowane NIESPOJNIE: nowsze (XIX-XXVIII) tytułowane numerem
rzymskim, starsze (I-XVIII, 2024-2025) po miesiącu — datę i nr sesji bierzemy
z nagłówka PDF ("Sesja Rady Miejskiej w Morągu nr XV w dniu 30 maja 2025 r.").

Nazwiska źródło podaje "Imię [drugie] Nazwisko" (konwencja Radoskopa).
Użycie: python scrape_morag.py --output docs/data.json --profiles docs/profiles.json
        [--cache-dir .cache]
"""

import argparse
import json
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from itertools import combinations
from pathlib import Path

import requests
import urllib3
import pdfplumber
from bs4 import BeautifulSoup
from io import BytesIO

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE = "https://bip.morag.pl"
VOTES_CAT = f"{BASE}/10061/Glosowanie_imienne/"
KAD_START = "2024-05-07"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36"}

VOTE_RE = re.compile(
    r'^\s*(\d+)[\s.]+(.+?)\s+'
    r'(Za|Przeciw|Wstrzyma[łl](?:a|i)? się(?: od głosu)?|Wstrzymuje się|'
    r'Wstrzymujący|Nieobecny|Nieobecna|Brak głosu|nie g[łl]osowa[łl])'
    r'\s*([\d.:\-—\s]*)$', re.I)

_MON = {'stycznia':1,'lutego':2,'marca':3,'kwietnia':4,'maja':5,'czerwca':6,
        'lipca':7,'sierpnia':8,'września':9,'października':10,'listopada':11,'grudnia':12}
_ROMAN = {"I":1,"V":5,"X":10,"L":50,"C":100,"D":500,"M":1000}
def _rm(x):
    v=0;prev=0
    for ch in reversed(x.upper()):
        cur=_ROMAN[ch]; v=v-cur if cur<prev else v+cur; prev=cur
    return v
def _date(s):
    m=re.search(r'(\d{1,2})\s*\.\s*(\d{1,2})\s*\.\s*(\d{4})', s)
    if m: d,mo,y=m.groups(); return f"{y}-{int(mo):02d}-{int(d):02d}"
    m=re.search(r'(\d{1,2})\s+(\w+)\s+(\d{4})', s)
    if m:
        mo=_MON.get(m.group(2).lower())
        if mo: return f"{m.group(3)}-{mo:02d}-{int(m.group(1)):02d}"
    return None
def _normname(s):
    s=s.lower()
    tbl=str.maketrans({"ą":"a","ć":"c","ę":"e","ł":"l","ń":"n","ó":"o","ś":"s","ź":"z","ż":"z"})
    return re.sub(r"[^a-z0-9]","",s.translate(tbl))
def make_slug(name):
    repl={'ą':'a','ć':'c','ę':'e','ł':'l','ń':'n','ó':'o','ś':'s','ź':'z','ż':'z'}
    sn=str(name or "").lower()
    for pl,a in repl.items(): sn=sn.replace(pl,a)
    sn=re.sub(r"[^a-z0-9]+","-",sn); return sn.strip("-")
def get(url, retries=3):
    for i in range(retries):
        try:
            r=requests.get(url, headers=UA, timeout=45, verify=False)
            if r.status_code==200: return r
        except Exception:
            time.sleep(1+i)
    return None

def collect_pdf_links():
    """Return list of (title, pdf_url) for ALL glosowanie-imienne PDFs across pages."""
    links={}
    empty=0
    for page in range(1, 12):
        u=f"{VOTES_CAT}{page}/" if page>1 else VOTES_CAT
        r=get(u)
        if not r: continue
        bs=BeautifulSoup(r.text,"lxml"); main=bs.find('main') or bs
        found=0
        for a in main.find_all('a', href=True):
            h=a['href']
            if 'pobierz.php' in h and '.pdf' in h.lower():
                full = h if h.startswith('http') else BASE+h
                if full not in links:
                    links[full]=a.get_text(' ',strip=True); found+=1
        if found==0:
            empty+=1
            if empty>=2: break
        else:
            empty=0
    return list(links.items())

def parse_pdf(text, title):
    """Return (session_date, session_num, votes) from PDF text."""
    # session date from header
    hdr = text[:1200]
    sdate = _date(hdr)
    # session number from header roman
    num=None
    m=re.search(r'\bnr\s*([IVXLCDM]+|\d+)(?:\s*/\s*\d+)?', hdr)
    if m:
        s=m.group(1)
        num=_rm(s) if s.isalpha() else int(s)
    else:
        m=re.search(r'\b([IVXLCDM]+)\s+(?:[Ss]esj\w+|Sesj\w+)', hdr)
        if m: num=_rm(m.group(1))
    # votes
    votes=[]; cur=None; in_table=False
    for raw in text.split('\n'):
        line=raw.strip()
        if line.startswith('Głosowanie w sprawie:'):
            if cur and cur['rows']: votes.append(cur)
            cur={'topic':line[len('Głosowanie w sprawie:'):].strip(),'rows':[],'agg':None}
            in_table=False
            continue
        if cur is None: continue
        if 'Typ głosowania' in line:
            continue
        # aggregate lines
        ag=re.search(r'Uprawnionych:\s*(\d+)\s+Za:\s*(\d+)|Zagłosowało:\s*(\d+)\s+Przeciw:\s*(\d+)|Nieobecni:\s*(\d+)\s+Wstrzymało się:\s*(\d+)', line)
        if ag:
            if cur['agg'] is None: cur['agg']={}
            kv={}
            if ag.group(1): kv['za']=int(ag.group(2))
            if ag.group(3): kv['przeciw']=int(ag.group(4))
            if ag.group(5): kv['nieobecni']=int(ag.group(5)); kv['wstrzymal_sie']=int(ag.group(6))
            cur['agg'].update(kv)
            continue
        if line.startswith('Oddane głosy - podsumowanie szczegółowe'):
            in_table=True; continue
        if in_table:
            mm=VOTE_RE.match(line)
            if mm:
                cur['rows'].append((mm.group(2).strip(), mm.group(3).strip()))
                continue
            if line and not line.startswith('Lp.') and not re.match(r'^\d', line) and 'Oddane' not in line and 'Uprawnionych' not in line:
                in_table=False
        else:
            if cur['topic'] and line and not line.startswith(('Oddane','Lp.','Głosowania','Tryb głosowania','Miejsce:')) and not re.match(r'\d', line):
                if 'Typ' not in line and 'Data głosowania' not in line:
                    cur['topic'] += ' '+line
    if cur and cur['rows']:
        votes.append(cur)
    if sdate is None:
        sdate = _date(title)
    return sdate, num, votes

_MONTH_NAMES={'styczen':'2024-01','luty':'2024-02','marzec':'2024-03','kwiecien':'2024-04','maj':'2024-05','czerwiec':'2024-06','grudzien':'2024-12','listopad':'2024-11','pazdziernik':'2024-10','wrzesien':'2024-09','sierpien':'2024-08','lipiec':'2024-07'}
def title_indicates_pre_ix(title):
    """Rough prefilter: if the title names a month before 2024-05 or an early 2024/2023
    date, we can skip the download (it cannot be an IX-kad session)."""
    t=title.lower()
    # numeric date 2023 / early 2024
    m=re.search(r'(\d{1,2})\s*\.\s*(\d{1,2})\s*\.\s*(\d{4})', t) or re.search(r'(\d{1,2})\s+(\w+)\s+(\d{4})', t)
    if m:
        y=m.group(3); mo=m.group(2) if m.lastindex==3 else None
        if int(y)<2024: return True
        if int(y)==2024 and mo and _MON.get(mo.lower()) and _MON[mo.lower()]<5: return True
    # 'I sesja 2024' / 'I sesji' often konstytuująca (07.05.2024) -> keep
    # monthname + year
    for mm,ym in _MONTH_NAMES.items():
        if mm in t:
            y=re.search(r'20\d\d', t)
            if y and int(y.group())<2024: return True
            if y and int(y.group())==2024 and int(ym[5:7])<5: return True
    return False

def collect_all():
    links=collect_pdf_links()
    records=[]
    for url, title in links:
        if title_indicates_pre_ix(title):
            continue
        r=get(url)
        if not r: 
            print(f"  [skip] {title[:40]}")
            continue
        with pdfplumber.open(BytesIO(r.content)) as pdf:
            text="".join((p.extract_text() or '')+'\n' for p in pdf.pages)
        sdate, num, votes = parse_pdf(text, title)
        if not sdate or sdate < KAD_START:
            print(f"  [pre-IX] {sdate} {title[:50]}")
            continue
        vcount=sum(len(v['rows']) for v in votes)
        print(f"  [ok] {sdate} nr={num} votes={len(votes)} rows={vcount} | {title[:45]}", flush=True)
        for v in votes:
            v['session_date']=sdate; v['session_num']=str(num or "")
            records.append(v)
    return records

VOTE_MAP={'Za':'za','PRZECIW':'przeciw','Przeciw':'przeciw','Wstrzymał się':'wstrzymal_sie',
          'Wstrzymała się':'wstrzymal_sie','Wstrzymuje się':'wstrzymal_sie','Wstrzymujący':'wstrzymal_sie',
          'Nieobecna':'nieobecny','Nieobecny':'nieobecny'}

def build_output(records):
    all_votes=[]; vid=0; sessions_by_date={}
    validated={'ok':0,'mismatch':0,'noagg':0,'emptytable':0}
    for rec in records:
        d=rec['session_date']
        if not d or d<KAD_START: continue
        if not rec['rows']:
            validated['emptytable']+=1; continue
        named={'za':[],'przeciw':[],'wstrzymal_sie':[],'brak':[],'nieobecny':[]}
        for name_raw,v in rec['rows']:
            key=VOTE_MAP.get(v,'brak')
            named[key].append(name_raw.strip())
        for k in named: named[k]=list(dict.fromkeys(named[k]))
        za=len(named['za']); pr=len(named['przeciw']); wz=len(named['wstrzymal_sie'])
        if rec.get('agg') and 'za' in rec['agg'] and 'przeciw' in rec['agg']:
            if za==rec['agg']['za'] and pr==rec['agg']['przeciw'] and wz==rec['agg'].get('wstrzymal_sie',0):
                validated['ok']+=1
            else:
                validated['mismatch']+=1
        else:
            validated['noagg']+=1
        if d not in sessions_by_date:
            sessions_by_date[d]={'date':d,'number':str(rec['session_num'] or ''),'vote_count':0,'attendees':set()}
        vid+=1
        sessions_by_date[d]['vote_count']+=1
        for k in ('za','przeciw','wstrzymal_sie','nieobecny','brak'):
            sessions_by_date[d]['attendees'].update(named[k])
        all_votes.append({
            'id':str(vid),'source_url':rec.get('source_url',''),
            'session_date':d,'session_number':str(rec.get('session_num') or ''),
            'topic':rec['topic'].strip(),'druk':'','resolution':'',
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
        same=sum(1 for vid in common if vectors[a][vid]==vectors[b][vid])
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
        d=rec.get('session_date')
        if not d or d<KAD_START: continue
        if not rec.get('rows'): continue
        named={'za':[],'przeciw':[],'wstrzymal_sie':[],'brak':[],'nieobecny':[]}
        for name_raw,v in rec['rows']:
            named[VOTE_MAP.get(v,'brak')].append(name_raw.strip())
        for k in named: named[k]=list(dict.fromkeys(named[k]))
        for cat,ns in named.items():
            for n in ns:
                cv[n][cat]+=1; cv[n]['votes'].append({'session':d,'vote':cat})
    profiles=[]
    for name in sorted(cv.keys()):
        vd=cv[name]
        total=sum(vd[k] for k in ('za','przeciw','wstrzymal_sie','nieobecny','brak')) or 1
        present_sess=len({v['session'] for v in vd['votes'] if v['vote']!='nieobecny'})
        all_sess=len({v['session'] for v in vd['votes']})
        frekw=100.0*present_sess/all_sess if all_sess else 0.0
        profiles.append({'name':name,'slug':make_slug(name),'kadencje':{KADENCJA_ID:{
            'club':'','has_voting_data':True,'has_activity_data':False,'frekwencja':round(frekw,1),
            'aktywnosc':0.0,'zgodnosc_z_klubem':0.0,'votes_za':vd['za'],'votes_przeciw':vd['przeciw'],
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
    records=collect_all()
    output,validated=build_output(records)
    k=output['kadencje'][0]
    print(f"[morag] sesje: {k['total_sessions']}, glosowania: {k['total_votes']}, radni: {k['total_councilors']}")
    print(f"[morag] walidacja agregatow: ok={validated['ok']} mismatch={validated['mismatch']} noagg={validated['noagg']} emptytable={validated['emptytable']}")
    profiles=build_profiles(records)
    save_split(output, Path(args.output), profiles)
    print("[morag] OK")

if __name__=='__main__':
    main()
