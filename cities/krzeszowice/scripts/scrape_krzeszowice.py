#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Radoskop Krzeszowice — imienne głosowania Rady Miejskiej w Krzeszowicach (IX kadencja 2024-2029).

Źródło: BIP Urzędu Miejskiego w Krzeszowicach na platformie bip.malopolska.pl (Madkom SPA,
encja `umkrzeszowice`). Sekcja Rada Miejska -> Sesje -> "Imienne wykazy głosowań" (menu 310174)
jest rozbita na podmenu per rok: 2024 (430283), 2025 (457930), 2026 (471942). Każdy artykuł =
jedna sesja ("Protokoły głosowań z N sesji Rady Miejskiej w Krzeszowicach z dnia ..."); załączniki
PDF "Wyniki głosowań - ... .pdf" w formacie systemu ELECTOR (PROTOKÓŁ GŁOSOWANIA):

    RADA MIASTA KRZESZOWICE
    {data}, {czas} PROTOKÓŁ GŁOSOWANIA
    {N}. {temat}
    Głosowanie jawne
    ({data}, {czas})
    (Przyjęto jednomyślnie | wynik)
    Uprawnieni:21 Obecni:19
    ZA 19
    Oddano głosów:19
    PRZECIW 0
    WSTRZYMAŁO SIĘ 0
    Brak głosu 0
    Brak obecności 2
    Szczegóły
    ZA: 19 głosów
    <nazwiska...>
    [WSTRZYMAŁO SIĘ: n głosów / PRZECIW: n / Brak głosu: n / Brak obecności: n + nazwiska]

Każde głosowanie ma agregat (ZA/PRZECIW/WSTRZYMAŁO SIĘ/Brak głosu/Brak obecności) do walidacji
liczności nazwisk. Sesje I-X 2024 (od 2024-05-07), XI-XXIII 2025, XXIV-XXX 2026.

API Madkom (bez auth): /api/menu/{id}/submenu, /api/menu/{id}/articles?limit=200,
/api/articles/{id} (attachments z link e,pobierz,get.html?id=), pobierz PDF bezpośrednio.

Rooster radnych = nazwiska z raportów głosowań (kanoniczne, walidowane agregatem).
Kluby: BIP nie publikuje "Klubów Radnych" dla IX kad. -> club_assignments PENDING.

Użycie:
    python scrape_krzeszowice.py --output docs/data.json --profiles docs/profiles.json [--cache-dir .cache] [--max-sessions N]
"""

import argparse
import json
import re
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime
from io import BytesIO
from itertools import combinations
from pathlib import Path

import pdfplumber
import requests

BASE = "https://bip.malopolska.pl"
ENTITY = "umkrzeszowice"
GLOS_MENU = 310174  # "Imienne wykazy głosowań"
YEARS = {2024: 430283, 2025: 457930, 2026: 471942}
KAD_START = "2024-05-07"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36"}

_MON = {'stycznia':1,'lutego':2,'marca':3,'kwietnia':4,'maja':5,'czerwca':6,
        'lipca':7,'sierpnia':8,'września':9,'października':10,'listopada':11,'grudnia':12}

def _norm_date(ttl):
    # "z dnia 7 maja 2024" or "z dnia 27.06.2024"
    m = re.search(r'z dnia (\d{1,2})\.(\d{1,2})\.(\d{4})', ttl)
    if m:
        return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    m = re.search(r'z dnia (\d{1,2}) (\w+) (\d{4})', ttl)
    if m:
        mo=_MON.get(m.group(2).lower())
        if mo: return f"{m.group(3)}-{mo:02d}-{int(m.group(1)):02d}"
    return None

def _roman(ttl):
    m=re.search(r'(?:z |zNadzwycz|nadzwycz|z nadzwycz)?\s*(?:z\s+)?([IVXLCDM]{1,6})\s+[Ss]esj', ttl)
    return m.group(1) if m else ""

def get_json(url, retries=4):
    for i in range(retries):
        try:
            r=requests.get(url, headers=UA, timeout=45, verify=False)
            if r.status_code==200: return r.json()
        except Exception:
            time.sleep(2.0*(i+1))
    raise RuntimeError(f"GET failed: {url}")
def get_file(url, retries=4):
    for i in range(retries):
        try:
            r=requests.get(url, headers=UA, timeout=90, verify=False)
            if r.status_code==200: return r.content
        except Exception:
            time.sleep(2.0*(i+1))
    raise RuntimeError(f"GET failed: {url}")

def collect_articles():
    out=[]
    for yr,mid in YEARS.items():
        d=get_json(f"{BASE}/api/menu/{mid}/articles?limit=200")
        for a in (d.get("articles") or []):
            ar=get_json(f"{BASE}/api/articles/{a['id']}")
            ttl=ar.get("title") or ""
            date=_norm_date(ttl)
            if not date or date < KAD_START: continue
            att=None
            for x in (ar.get("attachments") or []):
                if x.get("extension")=="pdf" and ("głosowań" in (x.get("name") or "").lower() or "Wyniki" in (x.get("name") or "")):
                    att=x; break
            if att is None:
                for x in (ar.get("attachments") or []):
                    if x.get("extension")=="pdf": att=x; break
            if att is None: continue
            out.append({"title":ttl,"date":date,"num":_roman(ttl),"att":att.get("id"),"year":yr})
    # dedup by date
    by={}
    for r in out: by.setdefault(r["date"],r)
    return list(by.values())

_NAME_TOKEN_STR = "A-ZĄĆĘŁŃÓŚŹŻa-ząćęłńóśźż\\-"
def _tokens(blk):
    return re.findall(r"[A-ZĄĆĘŁŃÓŚŹŻ][" + _NAME_TOKEN_STR + r"]*", blk)

# Vote block: topic before "Głosowanie jawne"
def parse_pdf(text):
    """Return list of vote dicts: {topic, agg, named:{za,przeciw,wstrzymal_sie,brak,nieobecny}}."""
    votes=[]
    page_brk = r'\d+\s*/\s*\d+|RADA MIASTA KRZESZOWICE|PROTOKÓŁ GŁOSOWANIA'
    matches=list(re.finditer(r'Głosowanie\s+jawne', text))
    for i,m in enumerate(matches):
        start=m.start()
        end = matches[i+1].start() if i+1<len(matches) else len(text)
        chunk=text[start:end]
        agg_m=re.search(r'Uprawnieni:(\d+)\s+Obecni:(\d+).*?ZA\s+(\d+).*?Oddano głosów:\s*(\d+).*?PRZECIW\s+(\d+).*?WSTRZYMAŁO SIĘ\s+(\d+).*?Brak głosu\s+(\d+).*?Brak obecności\s+(\d+)', chunk, re.S)
        if not agg_m: continue
        agg={"za":int(agg_m.group(3)),"przeciw":int(agg_m.group(5)),
             "wstrzymal_sie":int(agg_m.group(6)),"brak":int(agg_m.group(7)),
             "nieobecny":int(agg_m.group(8))}
        named={"za":[],"przeciw":[],"wstrzymal_sie":[],"brak":[],"nieobecny":[]}
        si=chunk.find("Szczegóły")
        if si>=0:
            seg=chunk[si+len("Szczegóły"):]
            cat_re=re.compile(r'(?m)^\s*(ZA|PRZECIW|WSTRZYMAŁO SIĘ|Brak głosu|Brak obecności)\s*:\s*(\d+)\s*(?:głosy?|głosów)\s*[:]?\s*')
            heads=list(cat_re.finditer(seg))
            for hi,h in enumerate(heads):
                label=h.group(1).strip()
                key={'ZA':'za','PRZECIW':'przeciw','WSTRZYMAŁO SIĘ':'wstrzymal_sie',
                     'Brak głosu':'brak','Brak obecności':'nieobecny'}[label]
                hstart=h.end()
                hend = heads[hi+1].start() if hi+1<len(heads) else len(seg)
                blk=seg[hstart:hend]
                pb=[mm.start() for mm in re.finditer(page_brk, blk)]
                if pb: blk=blk[:min(pb)]
                # store raw token list (multiple names per line, no separator)
                named[key+"_tok"]=_tokens(blk)
        # topic: scan preceding context for the numbered topic line(s)
        topic=""
        pre=text[max(0,start-700):start]
        tm=list(re.finditer(r'\n(\d{1,2})\.\s+([^\n]+)', pre))
        if tm:
            last=tm[-1]; topic=last.group(2).strip()
        votes.append({"topic":topic,"agg":agg,"named_tokens":named,"raw_named":{k:v for k,v in named.items() if not k.endswith("_tok")}})
    return votes

def _reconstruct_names(tokens, count, roster_pairs):
    """Chunk a flat token list into names. Names are mostly 2 tokens (First Last).
    Use roster of known (first,last) pairs; fall back to 2-token chunks matching count."""
    if count<=0: return []
    # build surname->list of firsts from roster pairs
    from collections import defaultdict
    by_surn=defaultdict(set)
    for f,l in roster_pairs:
        by_surn[l].add(f)
    names=[]
    i=0
    while i < len(tokens) and len(names)<count:
        t=tokens
        if i+2<=len(t):
            first,last=t[i],t[i+1]
            if last in by_surn and first in by_surn[last]:
                names.append(f"{first} {last}"); i+=2; continue
            if first in by_surn and last in by_surn[first]:  # reversed? First Last order fixed
                pass
        if i+2<=len(t):
            names.append(f"{t[i]} {t[i+1]}"); i+=2
        else:
            i+=1
    return [_title_case(n) for n in names[:count]]

def _title_case(name):
    """ANGELIKA BALAWEJDER -> Angelika Balawejder; Krystyna Galos stays."""
    parts=name.split()
    out=[]
    for p in parts:
        if p==p.upper() and len(p)>1:  # all caps already
            out.append(p.title())
        else:
            out.append(p)
    return " ".join(out)

def _build_roster(records):
    """From all parsed votes' za token lists, derive canonical (first,last) names.
    Assumes constant roster (~21 councilors) and za lists are 2-token names."""
    from collections import Counter
    pairs=Counter()
    cats=["za_tok","przeciw_tok","wstrzymal_sie_tok","brak_tok","nieobecny_tok"]
    for d,num,votes in records:
        for v in votes:
            nt=v.get("named_tokens",{})
            for ck in cats:
                toks=nt.get(ck,[])
                if len(toks)>=2:
                    for k in range(0, len(toks)-1, 2):
                        pairs[(toks[k],toks[k+1])]+=1
    roster=[]
    for (f,l),c in pairs.most_common(300):
        if c>=5 and len(f)>=2 and len(l)>=2:
            roster.append((f,l))
    return roster

def main():
    ap=argparse.ArgumentParser(prog="Radoskop Krzeszowice")
    ap.add_argument("--output", required=True)
    ap.add_argument("--profiles", required=True)
    ap.add_argument("--cache-dir", default=".cache")
    ap.add_argument("--max-sessions", type=int, default=0)
    args=ap.parse_args()

    arts=collect_articles()
    if args.max_sessions>0: arts=arts[:args.max_sessions]
    arts.sort(key=lambda r:r["date"])
    print("sesje IX kad.:",len(arts), flush=True)

    cache=Path(args.cache_dir)/"krzeszowice"; cache.mkdir(parents=True,exist_ok=True)
    skipped=[]
    all_records=[]  # (date,num,votes)
    for r in arts:
        key=f"{r['date']}_{r['att']}"
        cpath=cache/f"{key}.txt"
        try:
            if cpath.exists():
                text=cpath.read_text(encoding='utf-8')
            else:
                content=get_file(f"{BASE}/e,pobierz,get.html?id={r['att']}")
                text="\n".join((pg.extract_text() or '') for pg in pdfplumber.open(BytesIO(content)).pages)
                cpath.write_text(text,encoding='utf-8')
        except Exception as e:
            skipped.append((r['date'],f'dl:{e}')); continue
        if not text or len(text)<200:
            skipped.append((r['date'],'empty')); continue
        votes=parse_pdf(text)
        if votes:
            all_records.append((r['date'],r['num'],votes))
        else:
            skipped.append((r['date'],'novotes'))
    print("SKIPPED:",skipped,flush=True)

    # build roster from za token pairs
    roster=_build_roster(all_records)
    print("roster:",roster,flush=True)
    roster_pairs=roster

    # build vote records
    roster_set={_title_case(f"{f} {l}") for f,l in roster_pairs}
    validated={'ok':0,'mismatch':0,'noim':0}
    all_votes=[]; vid=0; by_date=defaultdict(list)
    all_names=set()
    for d,num,votes in all_records:
        for v in votes:
            ag=v["agg"]
            nt=v.get("named_tokens",{})
            named={}
            for key,cat in [("za_tok","za"),("przeciw_tok","przeciw"),
                            ("wstrzymal_sie_tok","wstrzymal_sie"),
                            ("brak_tok","brak"),("nieobecny_tok","nieobecny")]:
                toks=nt.get(key,[])
                count=ag[cat] if cat!='nieobecny' else ag.get('nieobecny',0)
                nms=_reconstruct_names(toks,count,roster_pairs)
                # only keep names present in the roster (drop OCR/pair garbage)
                named[cat]=[n for n in nms if n in roster_set]
            if not named['za'] and not named['przeciw'] and not named['wstrzymal_sie']:
                validated['noim']+=1; continue
            if (len(named['za'])==ag['za'] and len(named['przeciw'])==ag['przeciw']
                and len(named['wstrzymal_sie'])==ag['wstrzymal_sie']
                and len(named['brak'])==ag['brak'] and len(named['nieobecny'])==ag['nieobecny']):
                validated['ok']+=1
            else:
                validated['mismatch']+=1
            vid+=1
            rec={'id':str(vid),'source_url':f"{BASE}/umkrzeszowice,m,310174,imienne-wykazy-glosowan.html",
                 'session_date':d,'session_number':str(num),'topic':v['topic'],'druk':'','resolution':'',
                 'counts':{'za':len(named['za']),'przeciw':len(named['przeciw']),'wstrzymal_sie':len(named['wstrzymal_sie'])},
                 'named_votes':named}
            all_votes.append(rec); by_date[d].append(rec)
            for k,ns in named.items(): all_names.update(ns)

    sessions_data=[]
    for d in sorted(by_date.keys()):
        sv=by_date[d]; att=set()
        for v in sv:
            for cat,ns in v['named_votes'].items(): att.update(ns)
        num=next((n for (dd,n,vv) in all_records if dd==d),'')
        sessions_data.append({'date':d,'number':str(num),'vote_count':len(sv),
                              'attendee_count':len(att),'attendees':sorted(att),'speakers':[]})
    print("Walldacja:",validated,flush=True)

    total_votes=len(all_votes); total_sessions=len(sessions_data)
    cv=defaultdict(lambda:{'za':0,'przeciw':0,'wstrzymal_sie':0,'nieobecny':0,'brak':0,'votes':[]})
    for v in all_votes:
        for cat,ns in v['named_votes'].items():
            for n in ns:
                cv[n][cat]+=1; cv[n]['votes'].append({'session':v['session_date'],'vote':cat})
    profiles=[]
    for name in sorted(cv.keys()):
        vd=cv[name]
        total=sum(vd[k] for k in ('za','przeciw','wstrzymal_sie','nieobecny','brak')) or 1
        sess_set=set(x['session'] for x in vd['votes'])
        frekw=100.0*len(sess_set)/total_sessions if total_sessions else 0.0
        profiles.append({'name':name,'slug':_slugify(name),'kadencje':{KADENCJA_ID:{
            'club':'','has_voting_data':True,'has_activity_data':False,'frekwencja':round(frekw,1),
            'aktywnosc':0.0,'zgodnosc_z_klubem':0.0,'votes_za':vd['za'],'votes_przeciw':vd['przeciw'],
            'votes_wstrzymal':vd['wstrzymal_sie'],'votes_brak':vd['brak'],'votes_nieobecny':vd['nieobecny'],
            'votes_total':total,'rebellion_count':0,'rebellions':[],'roles':[],'notes':''}}})
    prof={'profiles':profiles,'total':len(profiles)}

    # councilors list in kadencja
    counc=[]; scores=defaultdict(dict)
    for v in all_votes:
        for cat in ('za','przeciw','wstrzymal_sie'):
            for n in v['named_votes'].get(cat,[]): scores[n][v['id']]=cat
    counc_sess=defaultdict(set)
    for v in all_votes:
        for cat,ns in v['named_votes'].items():
            for n in ns: counc_sess[n].add(v['session_date'])
    for n in sorted(cv.keys()):
        vd=cv[n]
        present=vd['za']+vd['przeciw']+vd['wstrzymal_sie']+vd['brak']
        aktywnosc=present/total_votes*100 if total_votes else 0
        frekw=len(counc_sess[n])/total_sessions*100 if total_sessions else 0
        counc.append({'name':n,'club':'','frekwencja':round(frekw,1),'aktywnosc':round(aktywnosc,1),
            'zgodnosc_z_klubem':0.0,'votes_za':vd['za'],'votes_przeciw':vd['przeciw'],
            'votes_wstrzymal':vd['wstrzymal_sie'],'votes_brak':vd['brak'],'votes_nieobecny':vd['nieobecny'],
            'votes_total':total_votes,'rebellion_count':0,'rebellions':[],'has_activity_data':False,'activity':None})

    pairs=[]; ns_names=sorted(cv.keys())
    for a,b in combinations(ns_names,2):
        common=set(scores[a].keys())&set(scores[b].keys())
        if len(common)<10: continue
        same=sum(1 for v in common if scores[a][v]==scores[b][v])
        pairs.append({'a':a,'b':b,'club_a':'','club_b':'','score':round(same/len(common)*100,1),'common_votes':len(common)})
    pairs.sort(key=lambda x:x['score'],reverse=True)

    kad={'id':KADENCJA_ID,'label':KADENCJA_LABEL,'clubs':{},'sessions':sessions_data,
         'total_sessions':total_sessions,'total_votes':total_votes,'total_councilors':len(counc),
         'councilors':counc,'votes':all_votes,'similarity_top':pairs[:20],'similarity_bottom':pairs[-20:][::-1]}
    output={'generated':datetime.now().isoformat(),'default_kadencja':KADENCJA_ID,'kadencje':[kad]}

    out_path=Path(args.output); out_path.parent.mkdir(parents=True,exist_ok=True)
    index={'generated':output.get('generated',''),'default_kadencja':KADENCJA_ID,'kadencje':[]}
    for k in output['kadencje']:
        (out_path.parent/f'kadencja-{k["id"]}.json').write_text(json.dumps(k,ensure_ascii=False,separators=(",",":")),encoding='utf-8')
        index['kadencje'].append({'id':k['id'],'label':k.get('label','')})
    out_path.write_text(json.dumps(index,ensure_ascii=False,separators=(",",":")),encoding='utf-8')
    Path(args.profiles).parent.mkdir(parents=True,exist_ok=True)
    Path(args.profiles).write_text(json.dumps(prof,ensure_ascii=False,separators=(",",":")),encoding='utf-8')
    print(f"\nZapisano: {total_sessions} sesji, {total_votes} głosowań, {len(profiles)} radnych, skipped={skipped}")
    print("=>",out_path,"|",args.profiles)

def _slugify(name):
    repl={'ą':'a','ć':'c','ę':'e','ł':'l','ń':'n','ó':'o','ś':'s','ź':'z','ż':'z'}
    sn=str(name or "").lower()
    for pl,a in repl.items(): sn=sn.replace(pl,a)
    sn=re.sub(r'[^a-z0-9]+','-',sn); return sn.strip('-')

if __name__=='__main__':
    main()
