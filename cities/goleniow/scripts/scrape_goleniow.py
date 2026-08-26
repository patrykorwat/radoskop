#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Radoskop Goleniów — imienne głosowania Rady Miejskiej w Goleniowie (IX kadencja).

Źródło: BIP bip.goleniow.pl (CMS idcom/eBOI), kategoria
"Imienne wykazy głosowań radnych" (/artykul/imienne-wykazy-glosowan-radnych).
Dla KAŻDEJ sesji IX kadencji (I … XXXIII) publikowany jest artykuł z załącznikiem PDF
(ścieżka /pliki/goleniow/zalaczniki/{id}/..._imienne_wykazy_glosowan_radnych_na_{NN}_sesji_....pdf)
zawierającym imienne głosowania per radny — format wydruku eSesja, dwukolumnowa tabela
"Lp | Nazwisko i imię | Głos" (ZA / PRZECIW / WSTRZYMUJĘ SIĘ / NIEOBECNY / OBECNY).

Struktura każdego PDF: jeden głos na stronę; sekcja "Liczba uprawnionych/obecnych/nieobecnych",
"Głosy za/przeciw/wstrzymujące się", "Obecni niegłosujący", a następnie tabela 21 radnych
(skład Rady Miejskiej). Walidacja per głos: zsumowane głosy imienne == agregaty z nagłówka.

Parser oparty o współrzędne PDF (pdfplumber.extract_words): dynamiczna granica kolumn
wg położenia kolumny Lp prawej połowy; nazwiska łączone aż do tokenu głosu (exit dla nazw
zawiniętych w dwa wiersze). Głosy NIEOBECNY/OBECNY traktowane odpowiednio.

Użycie:
    python scrape_goleniow.py --city-dir <cities/goleniow> [--cache-dir dir]
Zapisuje: docs/data.json, docs/kadencja-2024-2029.json, docs/profiles.json
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
from pathlib import Path

import pdfplumber
import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BIP = "https://bip.goleniow.pl"
CATEGORY = "/artykul/imienne-wykazy-glosowan-radnych"
KAD_START = "2024-05-07"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"

# Kuratorowany skład Rady Miejskiej w Goleniowie (21 radnych) — z BIP "Skład osobowy Rady Miejskiej".
ROSTER = [
    "Adamkiewicz Piotr","Banach Tomasz","Czerwiński Krzysztof","Geblewicz Agnieszka",
    "Guziak Arkadiusz","Henkelman Irena","Jastrzębski Michał","Jaworska Krystyna",
    "Jurewicz Anita","Kinik Sylwester","Kuszyński Robert","Latka Małgorzata",
    "Łebiński Wojciech","Mac Michał","Muszyńska-Popielarczyk Aleksandra","Panek Artur",
    "Różański Andrzej","Skakuj Mariusz","Szurgot Zbigniew","Wojciechowska Agnieszka",
    "Zajko Małgorzata",
]

_MONTHS = {
    "stycznia":1,"lutego":2,"marca":3,"kwietnia":4,"maja":5,"czerwca":6,"lipca":7,
    "sierpnia":8,"września":9,"pazdziernika":10,"października":10,"listopada":11,"grudnia":12,
}
_ROM = {"I":1,"II":2,"III":3,"IV":4,"V":5,"VI":6,"VII":7,"VIII":8,"IX":9,"X":10,
        "XI":11,"XII":12,"XIII":13,"XIV":14,"XV":15,"XVI":16,"XVII":17,"XVIII":18,"XIX":19,
        "XX":20,"XXI":21,"XXII":22,"XXIII":23,"XXIV":24,"XXV":25,"XXVI":26,"XXVII":27,
        "XXVIII":28,"XXIX":29,"XXX":30,"XXXI":31,"XXXII":32,"XXXIII":33,"XXXIV":34,"XXXV":35,"XXXVI":36}

def _nk(s):
    s=s.lower().replace("ł","l")
    s=unicodedata.normalize("NFKD",s)
    return re.sub(r"[^a-z0-9]","","".join(c for c in s if not unicodedata.combining(c)))

def _norm_vote(w):
    k=_nk(w)
    if k in ("za","z","ża","ze"): return "za"
    if k.startswith("przeciw"): return "przeciw"
    if k.startswith("wstrzym"): return "wstrzymal_sie"
    if k.startswith("nieobecn"): return "nieobecni"
    if k.startswith("obecn"): return "obecny"
    return None

REQ_DELAY=0.5
_LAST=0.0
def _rate():
    global _LAST
    d=time.time()-_LAST
    if d<REQ_DELAY: time.sleep(REQ_DELAY-d)
    _LAST=time.time()

def _get(url, cache_dir):
    key=hashlib.md5(url.encode()).hexdigest()
    if cache_dir:
        cache_dir=Path(cache_dir); cache_dir.mkdir(parents=True,exist_ok=True)
        cf=cache_dir/(key+".dat")
        if cf.is_file():
            return cf.read_bytes()
    _rate()
    r=requests.get(url,headers={"User-Agent":"Mozilla/5.0 (Radoskop)"},timeout=60,verify=False)
    r.raise_for_status()
    data=r.content
    if cache_dir:
        (cache_dir/(key+".dat")).write_bytes(data)
    return data

# ---------------- discovery ----------------
def discover_sessions():
    t=_get(BIP+CATEGORY, None).decode("utf-8","ignore")
    from html import unescape
    sessions=[]
    for m in re.finditer(r'<a href="(/artykul/imienne-wykazy-glosowan-radnych[^"]+)"[^>]*>(.*?)</td>',t,re.S):
        title=unescape(re.sub(r'<[^>]+>','',m.group(2))).strip()
        if 'sesji' not in title and 'nadzwyczajnej' not in title:
            continue
        dm=re.search(r'w dniu (\d{1,2}) (stycznia|lutego|marca|kwietnia|maja|czerwca|lipca|sierpnia|września|pazdziernika|października|listopada|grudnia)(?:\.|\s|r)', title)
        if not dm:
            dm2=re.search(r'w dniu (\d{1,2})\.(\d{1,2})\.(\d{4})', title)
        else:
            dm2=None
        if not dm and not dm2:
            continue
        if dm:
            day=int(dm.group(1))
            monn=_MONTHS.get(dm.group(2).replace('października','pazdziernika'))
            mon=monn
            year=re.search(r'(\d{4})\s*r\.',title)
            year=int(year.group(1)) if year else 2024
        else:
            day=int(dm2.group(1)); mon=int(dm2.group(2)); year=int(dm2.group(3))
        date=f"{year}-{mon:02d}-{day:02d}"
        if date < KAD_START:
            continue
        rm=re.search(r'na (XXX[IV]*|XX[IVX]*|X[IVX]*|IX|VIII|VII|VI|V|IV|III|II|I)(?: |nadzwyczaj)',title)
        num= _ROM.get(rm.group(1),'') if rm else ''
        sessions.append({"url":BIP+m.group(1),"title":title,"date":date,"num":num})
    sessions.sort(key=lambda s:s["date"])
    return sessions

# ---------------- PDF parsing ----------------
def _table_region(words):
    up=[w for w in words if _nk(w['text'])=="uprawnieni"]
    if not up: return words,0
    thr=max(w['top'] for w in up)
    return [w for w in words if w['top']>thr+4], thr

def _col_boundary(words):
    right_lps=[w['x0'] for w in words if re.match(r'^\d{1,2}\.$',w['text']) and w['x0']>150]
    if right_lps: return min(right_lps)-3
    lps=[w['x0'] for w in words if re.match(r'^\d{1,2}\.$',w['text'])]
    return (min(lps)+max(lps))/2.0 if lps else 297.0

def _parse_column(col):
    col.sort(key=lambda t:(t[0],t[1]))
    rows=[]; cr=None
    for top,x0,t in col:
        if cr is None or top-cr[0]>6:
            cr=[top,[]]; rows.append(cr)
        cr[1].append((x0,t))
    out=[]; cur=None
    def emit():
        nonlocal cur
        if cur is not None and cur.get('vote'):
            out.append((cur['name'].strip(), cur['vote']))
        cur=None
    for top,toks in rows:
        toks.sort(key=lambda z:z[0])
        if re.match(r'^\d+\.$', toks[0][1]):
            emit(); cur={'name':'','vote':None}; toks=toks[1:]
        elif cur is None:
            cur={'name':'','vote':None}
        for _x,t in toks:
            nv=_norm_vote(t)
            if nv in ("za","przeciw","wstrzymal_sie","nieobecni","obecny"):
                cur['vote']=nv
            elif _nk(t) in ("sie","sier"):
                pass
            elif re.match(r'(?i)(wydrukowano|\d{1,2}\.\d{1,2}\.\d{4})$',t):
                pass
            else:
                cur['name']=(cur['name']+' '+t).strip()
    emit()
    return out

def _table_cells(words):
    b=_col_boundary(words)
    L=[(w['top'],w['x0'],w['text']) for w in words if w['x0']<b]
    R=[(w['top'],w['x0'],w['text']) for w in words if w['x0']>=b]
    cells=[]
    for col in (L,R):
        cells+=_parse_column(col)
    return cells

def _extract_aggs(text):
    agg={}
    for key,pat in [("uprawnionych",r'Liczba uprawnionych\s+(\d+)'),("obecnych",r'Liczba obecnych\s+(\d+)'),
                    ("nieobecnych",r'Liczba nieobecnych\s+(\d+)'),("obecni_nieglosujacy",r'Obecni niegłosujący\s+(\d+)'),
                    ("za",r'Głosy za\s+(\d+)'),("przeciw",r'Głosy przeciw\s+(\d+)'),("wstrzym",r'Głosy wstrzymujące się\s+(\d+)')]:
        m=re.search(pat,text)
        if m: agg[key]=int(m.group(1))
    return agg

def _extract_topic(text):
    m=re.search(r'Typ głosowania',text)
    pre=text[:m.start()] if m else text
    pre=re.sub(r'(?is)^.*?Sesja Rady Miejskiej w Goleniowie.*?(w dniu[^\n]*)?\n','',pre)
    pre=pre.replace('Głosowanie',' ')
    lines=[l.strip() for l in pre.split('\n')]
    out=[]
    for l in lines:
        if not l: continue
        if re.match(r'^(Nr\s*)?\d+(\.|,)?$',l): continue
        out.append(l)
    topic=' '.join(out)
    topic=re.sub(r'\s+',' ',topic).strip(' .:,;-')
    return topic or '(glosowanie)'

# roster matching
def _match_roster(cells):
    bysur={}
    for full in ROSTER:
        parts=full.split(); bysur.setdefault(_nk(parts[0]),[]).append(full)
    out=[]
    for nm,vote in cells:
        words=nm.split()
        if not words: out.append(('?',vote)); continue
        cands=bysur.get(_nk(words[0]))
        if not cands:
            out.append((nm,vote)); continue
        if len(cands)==1:
            out.append((cands[0],vote)); continue
        given=''.join(w[0] for w in words[1:] if w and w[0].isalpha())
        best=None
        for c in cands:
            cg=''.join(pp[0] for pp in c.split()[1:])
            if cg and cg==given: best=c; break
        out.append((best or cands[0],vote))
    return out

def parse_pdf_payload(data):
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        votes=[]
        cur=None
        for page in pdf.pages:
            words=page.extract_words()
            text=page.extract_text() or ""
            has_agg="Liczba uprawnionych" in text
            tw,_=_table_region(words)
            cells=_table_cells(tw)
            matched=_match_roster(cells)
            if has_agg:
                agg=_extract_aggs(text)
                topic=_extract_topic(text)
                cur={"topic":topic,"agg":agg,"matched":matched}
                votes.append(cur)
            else:
                if cur is not None:
                    cur["matched"]=cur["matched"]+matched
    # validate + convert to records
    records=[]
    for v in votes:
        counter=Counter(vote for _n,vote in v["matched"])
        ok = (
            counter.get('za',0)==v["agg"].get('za',-1) and
            counter.get('przeciw',0)==v["agg"].get('przeciw',-1) and
            counter.get('wstrzymal_sie',0)==v["agg"].get('wstrzym',-1) and
            counter.get('nieobecni',0)==v["agg"].get('nieobecnych',-1) and
            counter.get('obecny',0)==v["agg"].get('obecni_nieglosujacy',-1)
        )
        if not ok:
            # mark vote as unvalidated -> skip (should not happen)
            continue
        named={"za":[],"przeciw":[],"wstrzymal_sie":[],"nieobecni":[]}
        for name,vote in v["matched"]:
            if vote in named:
                named[vote].append(name)
        records.append({"topic":v["topic"],"named":named})
    return records

# ---------------- output (wzorowane na sroda-wielkopolska) ----------------
def make_slug(name):
    repl={'ą':'a','ć':'c','ę':'e','ł':'l','ń':'n','ó':'o','ś':'s','ź':'z','ż':'z',
          'Ą':'A','Ć':'C','Ę':'E','Ł':'L','Ń':'N','Ó':'O','Ś':'S','Ź':'Z','Ż':'Z'}
    slug=name.lower()
    for pl,a in repl.items(): slug=slug.replace(pl,a)
    return re.sub(r"[^a-z0-9]+","",slug)

def build_output(records, club_assign=None):
    club_assign=club_assign or {}
    all_votes=[]; vid=0; sessions_by_date={}
    for rec in records:
        d=rec["date"]
        if not d or d<KAD_START: continue
        if d not in sessions_by_date:
            sessions_by_date[d]={"date":d,"number":rec.get("num",""),"vote_count":0,"attendees":set(),"speakers":[]}
        sessions_by_date[d]["vote_count"]+=1
        named={k:list(v) for k,v in rec["named"].items()}
        for cat in ("za","przeciw","wstrzymal_sie"):
            sessions_by_date[d]["attendees"].update(rec["named"].get(cat,[]))
        vid+=1
        all_votes.append({"id":str(vid),"session_date":d,"session_number":rec.get("num",""),
                          "topic":rec.get("topic",""),"named_votes":named,
                          "counts":{k:len(named.get(k,[])) for k in ("za","przeciw","wstrzymal_sie")}})
    sessions_data=[]
    for d in sorted(sessions_by_date.keys()):
        s=sessions_by_date[d]
        sessions_data.append({"date":d,"number":s["number"],"vote_count":s["vote_count"],
                              "attendee_count":len(s["attendees"]),"attendees":sorted(s["attendees"]),"speakers":[]})
    all_names=set()
    for v in all_votes:
        for names in v["named_votes"].values(): all_names.update(names)
    councilors_data={}
    for name in all_names:
        councilors_data[name]={"name":name,"club":club_assign.get(name,"NZ"),"district":None,
            "votes_za":0,"votes_przeciw":0,"votes_wstrzymal":0,"votes_brak":0,"votes_nieobecny":0,"rebellions":[]}
    for v in all_votes:
        for cat,names in v["named_votes"].items():
            if cat=="nieobecni":
                for nm in names:
                    if nm in councilors_data: councilors_data[nm]["votes_nieobecny"]+=1
                continue
            for nm in names:
                if nm not in councilors_data: continue
                if cat=="za": councilors_data[nm]["votes_za"]+=1
                elif cat=="przeciw": councilors_data[nm]["votes_przeciw"]+=1
                else: councilors_data[nm]["votes_wstrzymal"]+=1
    total_votes=len(all_votes); total_sessions=len(sessions_data)
    councillor_sess=defaultdict(set)
    for v in all_votes:
        for cat,names in v["named_votes"].items():
            for nm in names: councillor_sess[nm].add(v["session_date"])
    councilors_list=[]
    for c in sorted(councilors_data.values(),key=lambda x:x["name"]):
        present=c["votes_za"]+c["votes_przeciw"]+c["votes_wstrzymal"]
        aktywn=(present/total_votes*100) if total_votes else 0
        frekw=(len(councillor_sess.get(c["name"],set()))/total_sessions*100) if total_sessions else 0
        councilors_list.append({"name":c["name"],"club":c["club"],"district":None,
            "frekwencja":round(frekw,1),"aktywnosc":round(aktywn,1),"zgodnosc_z_klubem":0.0,
            "votes_za":c["votes_za"],"votes_przeciw":c["votes_przeciw"],"votes_wstrzymal":c["votes_wstrzymal"],
            "votes_brak":c["votes_brak"],"votes_nieobecny":c["votes_nieobecny"],"votes_total":total_votes,
            "rebellion_count":0,"rebellions":[],"has_activity_data":False,"activity":None})
    vectors=defaultdict(dict)
    for v in all_votes:
        for cat in ("za","przeciw","wstrzymal_sie"):
            for nm in v["named_votes"].get(cat,[]): vectors[nm][v["id"]]=cat
    from itertools import combinations
    pairs=[]; names_sorted=sorted(vectors.keys())
    for a,b in combinations(names_sorted,2):
        common=set(vectors[a].keys())&set(vectors[b].keys())
        if len(common)<10: continue
        same=sum(1 for vid in common if vectors[a][vid]==vectors[b][vid])
        pairs.append({"a":a,"b":b,"club_a":"","club_b":"","score":round(same/len(common)*100,1),"common_votes":len(common)})
    pairs.sort(key=lambda x:x["score"],reverse=True)
    kad={"id":KADENCJA_ID,"label":KADENCJA_LABEL,
         "clubs":dict(Counter(club_assign.get(c["name"],"NZ") for c in councilors_list)),
         "sessions":sessions_data,"total_sessions":total_sessions,
         "total_votes":total_votes,"total_councilors":len(councilors_list),
         "councilors":councilors_list,"votes":all_votes,
         "similarity_top":pairs[:20],"similarity_bottom":pairs[-20:][::-1]}
    return {"generated":datetime.now().isoformat(),"default_kadencja":KADENCJA_ID,"kadencje":[kad]}, total_votes, total_sessions

def build_profiles(records, club_assign=None):
    club_assign=club_assign or {}
    cv=defaultdict(lambda:{"za":0,"przeciw":0,"wstrzymal_sie":0,"nieobecni":0,"votes":[]})
    for rec in records:
        d=rec["date"]
        if not d or d<KAD_START: continue
        for cat,names in rec["named"].items():
            for nm in names:
                cv[nm][cat]+=1; cv[nm]["votes"].append({"session":d,"vote":cat})
    profiles=[]
    sess_set={r["date"] for r in records if r["date"]>=KAD_START}
    n_sessions=len(sess_set) or 1
    for nm in sorted(cv.keys()):
        vd=cv[nm]
        total=sum(vd[k] for k in ("za","przeciw","wstrzymal_sie")) or 1
        sess=len({v["session"] for v in vd["votes"]})
        aktywn=(vd["za"]+vd["przeciw"]+vd["wstrzymal_sie"])/n_sessions*100
        profiles.append({"name":nm,"slug":make_slug(nm),
            "kadencje":{KADENCJA_ID:{"club":club_assign.get(nm,"NZ"),"has_voting_data":True,
                "has_activity_data":False,"frekwencja":round(sess/n_sessions*100,1),"aktywnosc":round(aktywn,1),
                "zgodnosc_z_klubem":0.0,"votes_za":vd["za"],"votes_przeciw":vd["przeciw"],
                "votes_wstrzymal":vd["wstrzymal_sie"],"votes_brak":0,"votes_nieobecny":0,"votes_total":total,
                "rebellion_count":0,"rebellions":[],"roles":[],"notes":"","former":False,"mid_term":False}}})
    return {"profiles":profiles,"total":len(profiles)}

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--city-dir",required=True)
    ap.add_argument("--cache-dir",default=None)
    args=ap.parse_args()
    city_dir=Path(args.city_dir)
    cache=Path(args.cache_dir) if args.cache_dir else None
    cfg={}
    if (city_dir/"config.json").is_file():
        cfg=json.loads((city_dir/"config.json").read_text(encoding="utf-8"))
    club_assign=cfg.get("club_assignments",{}) or {}
    sessions=discover_sessions()
    print(f"[goleniow] {len(sessions)} sesji IX kad.")
    records=[]
    for se in sessions:
        try:
            pdf=_get(se["url"], cache)  # we cache by full article URL -> pdf bytes below
            # need real pdf: find attachment link inside article
            art=pdf.decode("utf-8","ignore")
            hrefs=re.findall(r'href="(/pliki/[^"]+\.pdf)"',art)
            if not hrefs:
                print(f"  [skip {se['date']} no pdf]"); continue
            pdf_bytes=_get(BIP+hrefs[0], cache)
            recs=parse_pdf_payload(pdf_bytes)
            for r in recs:
                r["date"]=se["date"]; r["num"]=se["num"]
            records+=recs
            print(f"  [ok] {se['date']} {se['num'] or '?':>3} votes={len(recs)}")
        except Exception as e:
            print(f"  [ERR {se['date']}] {type(e).__name__}: {e}")
    output,total_votes,total_sessions=build_output(records,club_assign)
    profiles=build_profiles(records,club_assign)
    docs=city_dir/"docs"; docs.mkdir(parents=True,exist_ok=True)
    (docs/"kadencja-2024-2029.json").write_text(json.dumps(output["kadencje"][0],ensure_ascii=False,indent=1),encoding="utf-8")
    data={"generated":output["generated"],"default_kadencja":KADENCJA_ID,"kadencje":[{"id":KADENCJA_ID,"label":KADENCJA_LABEL}]}
    (docs/"data.json").write_text(json.dumps(data,ensure_ascii=False,indent=1),encoding="utf-8")
    (docs/"profiles.json").write_text(json.dumps(profiles,ensure_ascii=False,indent=1),encoding="utf-8")
    print(f"[goleniow] DONE votes={total_votes} sessions={total_sessions} councilors={len(profiles['profiles'])}")

if __name__=="__main__":
    main()
