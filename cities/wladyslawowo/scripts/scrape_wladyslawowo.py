#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Radoskop Władysławowo — imienne głosowania Rady Miejskiej w Władysławowie (IX kadencja 2024-2029).

Źródło: AlfaTV "System Rada" dla Urzędu Miejskiego w Władysławowie
(rada.wladyslawowo.pl, /glosowania → /glosowania/posiedzenie/{id}).

Struktura posiedzenia (czysty HTML, bez OCR/PDF/JS — dane server-rendered):
    div.accordion-item  (po jednym na głosowanie)
      - <span class="w-100">TEMAT uchwały/wniosku</span>
      - <span class="badge">Przyjęto | Odrzucono</span>
      - Zakończono: DD.MM.YYYY HH:MM:SS
      - tabela agregatów: Głosy za | wstrzymujące | przeciw | nieoddane | Nieobecni
      - <p>Imienny wykaz głosowania</p>
        <table>: <tr><td><a ..radny/{id}>Imię Nazwisko</a></td><td>za|przeciw|wstrzymał się|nieobecny|nieoddany</td></tr>
Sesje: System Rada publikuje wyłącznie posiedzenia z zapisanymi wynikami
(18 posiedzeń IX kad., 2024-05..2026-07). Datę sesji bierzemy z najczęstszej
wartości "Zakończono" w obrębie posiedzenia.

Nazwiska źródło podaje "Imię Nazwisko" (konwencja Radoskopa).
Użycie: python scrape_wladyslawowo.py --output docs/data.json --profiles docs/profiles.json
        [--cache-dir .cache]
"""

import argparse
import json
import re
import time
from collections import Counter, defaultdict
from datetime import datetime
from itertools import combinations
from pathlib import Path

import requests
import urllib3
from bs4 import BeautifulSoup

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE = "https://rada.wladyslawowo.pl"
GLOS = f"{BASE}/glosowania"
KAD_START = "2024-05-07"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36"}
_t0=time.time()
def rate_limit(interval=0.8):
    global _t0
    el=time.time()-_t0
    if el<interval: time.sleep(interval-el)
    _t0=time.time()
def get(url, retries=4):
    for i in range(retries):
        try:
            rate_limit()
            r=requests.get(url, headers=UA, timeout=45, verify=False)
            if r.status_code==200: return r.text
        except Exception:
            time.sleep(1+i)
    return None
def make_slug(name):
    repl={'ą':'a','ć':'c','ę':'e','ł':'l','ń':'n','ó':'o','ś':'s','ź':'z','ż':'z'}
    sn=str(name or "").lower()
    for pl,a in repl.items(): sn=sn.replace(pl,a)
    sn=re.sub(r"[^a-z0-9]+","-",sn); return sn.strip("-")

def collect_posiedzenia():
    t=get(GLOS)
    ids=sorted(set(int(m) for m in re.findall(r'/glosowania/posiedzenie/(\d+)', t or "")))
    return ids

def parse_posiedzenie(html):
    soup=BeautifulSoup(html,'lxml')
    votes=[]
    dates=[]
    for item in soup.select('div.accordion-item'):
        topic_btn=item.select_one('.accordion-header .w-100, .accordion-header span.w-100')
        topic=topic_btn.get_text(' ',strip=True) if topic_btn else ''
        badge=item.select_one('.badge')
        resolution=badge.get_text(' ',strip=True) if badge else ''
        body=item.select_one('.accordion-body')
        if not body: continue
        body_txt=body.get_text(' ',strip=True)
        m=re.search(r'Zakończono:\s*(\d{2})\.(\d{2})\.(\d{4})', body_txt)
        date=f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else None
        if date: dates.append(date)
        # aggregates table (first table)
        tables=body.find_all('table')
        agg=None
        if tables:
            tds=[td.get_text(' ',strip=True) for td in tables[0].find_all('td')]
            if len(tds)>=5:
                try: agg=[int(x or 0) for x in tds[:5]]
                except: agg=None
        # imienny table
        rows=[]
        im_tbl=None
        for tb in tables:
            if 'Imię i nazwisko' in (tb.get_text(' ',strip=True)):
                im_tbl=tb; break
        if im_tbl is None and len(tables)>1:
            im_tbl=tables[-1]
        if im_tbl:
            for tr in im_tbl.find_all('tr'):
                tds=[td.get_text(' ',strip=True) for td in tr.find_all('td')]
                if len(tds)>=2 and tds[1]:
                    rows.append((tds[0], tds[1]))
        if not topic and not rows: 
            continue
        votes.append({'topic':topic,'resolution':resolution,'date':date,'agg':agg,'rows':rows})
    # session date = most common vote date
    sdate=None
    if dates:
        sdate=Counter(dates).most_common(1)[0][0]
    return sdate, votes

VOTE_MAP={'za':'za','przeciw':'przeciw','wstrzymał się':'wstrzymal_sie','wstrzymała się':'wstrzymal_sie',
          'nieobecny':'nieobecny','nieobecna':'nieobecny','nieoddany':'brak','nieoddał głosu':'brak',
          'nie brał udziału':'brak'}

def collect_all():
    ids=collect_posiedzenia()
    records=[]
    for pid in ids:
        u=f"{BASE}/glosowania/posiedzenie/{pid}"
        t=get(u)
        if not t:
            print(f"  [skip] posiedzenie {pid}")
            continue
        sdate, votes=parse_posiedzenie(t)
        nrows=sum(len(v['rows']) for v in votes)
        print(f"  [ok] posiedzenie {pid} date={sdate} votes={len(votes)} rows={nrows}", flush=True)
        if not sdate or sdate<KAD_START:
            continue
        for v in votes:
            v['session_date']=sdate; v['session_num']=''
            records.append(v)
    # sort records by session date then preserve order
    records.sort(key=lambda v: (v['session_date'], v['date'] or ''))
    return records

def build_output(records):
    all_votes=[]; vid=0; sessions_by_date={}
    validated={'ok':0,'mismatch':0,'noagg':0,'emptytable':0}
    for rec in records:
        d=rec['session_date']
        if not d or d<KAD_START: continue
        if not rec['rows']:
            validated['emptytable']+=1; continue
        named={'za':[],'przeciw':[],'wstrzymal_sie':[],'brak':[],'nieobecny':[]}
        for name,v in rec['rows']:
            key=VOTE_MAP.get(v,'brak'); named[key].append(name.strip())
        for k in named: named[k]=list(dict.fromkeys(named[k]))
        za=len(named['za']); pr=len(named['przeciw']); wz=len(named['wstrzymal_sie'])
        if rec.get('agg'):
            agg_za,agg_wz,agg_pr,agg_odd,agg_nie=rec['agg']
            if za==agg_za and pr==agg_pr and wz==agg_wz:
                validated['ok']+=1
            else:
                validated['mismatch']+=1
        else:
            validated['noagg']+=1
        if d not in sessions_by_date:
            sessions_by_date[d]={'date':d,'number':'','vote_count':0,'attendees':set()}
        vid+=1
        sessions_by_date[d]['vote_count']+=1
        for k in ('za','przeciw','wstrzymal_sie','nieobecny','brak'):
            sessions_by_date[d]['attendees'].update(named[k])
        all_votes.append({'id':str(vid),'source_url':'','session_date':d,'session_number':'',
            'topic':rec['topic'].strip(),'druk':'','resolution':rec.get('resolution',''),
            'counts':{'za':za,'przeciw':pr,'wstrzymal_sie':wz},'named_votes':named})
    sessions_data=[]
    for d in sorted(sessions_by_date.keys()):
        sidx=list(sorted(sessions_by_date.keys())).index(d)
        s=sessions_by_date[d]
        sessions_data.append({'date':d,'number':str(sidx+1),'vote_count':s['vote_count'],
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
            'votes_przeciw':c['votes_przeciw'],'votes_wstrzymal':c['votes_wstrzymal'],'votes_brak':c['votes_brak'],
            'votes_nieobecny':c['votes_nieobecny'],'votes_total':total_votes,'rebellion_count':0,
            'rebellions':[],'has_activity_data':False,'activity':None})
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
        for name,v in rec['rows']:
            named[VOTE_MAP.get(v,'brak')].append(name.strip())
        for k in named: named[k]=list(dict.fromkeys(named[k]))
        for cat,ns in named.items():
            for n in ns: cv[n][cat]+=1; cv[n]['votes'].append({'session':d,'vote':cat})
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
    print(f"[wladyslawowo] sesje: {k['total_sessions']}, glosowania: {k['total_votes']}, radni: {k['total_councilors']}")
    print(f"[wladyslawowo] walidacja agregatow: ok={validated['ok']} mismatch={validated['mismatch']} noagg={validated['noagg']} emptytable={validated['emptytable']}")
    profiles=build_profiles(records)
    save_split(output, Path(args.output), profiles)
    print("[wladyslawowo] OK")

if __name__=='__main__':
    main()
