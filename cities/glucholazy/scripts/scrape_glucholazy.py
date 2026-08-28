#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Radoskop Głuchołazy — imienne głosowania Rady Miejskiej w Głuchołazach (IX kadencja 2024-2029).

Źródło: BIP bip.glucholazy.pl (custom CMS), "Rada Miejska -> Protokoły z sesji Rady
Miejskiej 2024-29" -> per-year kategorie "Protokoły z sesji {rok}":
  - 2024: /7415/protokoly-z-sesji-2024.html
  - 2025: /8424/protokoly-z-sesji-2025.html
  - 2026: /9808/protokoly-z-sesji-2026.html
Każdy "Protokół nr {ROMAN}/{YY} z Sesji Rady Miejskiej w Głuchołazach" jest artykułem HTML
(serwer-renderowany, bez PDF) zawierającym komplet głosowań IMiennych w formacie:

    głosowanie uchwały (HH:MM)
    Wyniki imienne:
    ZA(16):
    <imię nazwisko, ...>
    PRZECIW(n): ...
    WSTRZYMUJĘ SIĘ(n): ...
    NIE GŁOSOWALI(n): ...
    NIEOBECNI(n): ...
    Z powodu problemu technicznego...

Data sesji z linii "Obrady rozpoczęto DD-MM-YYYY o godz. HH:MM". Numer sesji (rzymski) z
"Protokół nr {ROMAN}/{YY}". Skład: pełny zbiór unikalnych radnych z głosowań (+ oficjalny
roster "Skład Rady Miejskiej" /27/) — kotwica nazwisk. Kluby kuratorowane z BIP
"Kluby Radnych Rady Miejskiej w Głuchołazach" (jeśli dostępne; w razie braku PENDING/NZ).
Walidacja per głos: zsumowane głosy imienne == liczniki z nagłówka "ZA(n):…".

Użycie:
    python scrape_glucholazy.py --city-dir <cities/glucholazy> [--work-dir dir] [--cache-dir dir]
Zapisuje: docs/data.json, docs/kadencja-2024-2029.json, docs/profiles.json
"""
import argparse
import hashlib
import io
import json
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from itertools import combinations
from pathlib import Path
from html import unescape

import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BIP = "https://bip.glucholazy.pl"
YEAR_CATS = {
    "2024": "/7415/protokoly-z-sesji-2024.html",
    "2025": "/8424/protokoly-z-sesji-2025.html",
    "2026": "/9808/protokoly-z-sesji-2026.html",
}
KAD_START = "2024-05-07"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"

# Oficjalny skład Rady Miejskiej w Głuchołazach (IX kadencja, "Skład Rady Miejskiej" /27/),
# forma "Nazwisko Imię" -> "Imię Nazwisko". Kotwica do kanonizacji nazwisk z głosowań.
_ROSTER_SURNAME_FIRST = [
    "Biliński Szymon", "Bortniczuk Marek", "Ćwiek Jan", "Grocholski Stanisław",
    "Drożdżyński Jarosław", "Dunaj Jerzy", "Gargol Mateusz", "Gąsior Grzegorz",
    "Pach-Gomulnicka Klaudia", "Maciński Kamil", "Migała Mariusz",
    "Mikłasz Radosław", "Ptak Grzegorz", "Szeloch Leszek", "Stokłosa Roman",
    "Studzienna Jadwiga", "Szczegielniak Anna", "Szupryczyński Edward", "Udziela Ryszard",
    "Wilk Teresa", "Zyśk Szymon", "Szpak Paweł",
]
ROSTER = set()
for _sf in _ROSTER_SURNAME_FIRST:
    parts = _sf.split()
    if len(parts) == 2:
        ROSTER.add(f"{parts[1]} {parts[0]}")
# curated first+last canonical map for name variants seen in votes
_VARIANTS = {
    "klaudia pach-gomulnicka": "Klaudia Pach-Gomulnicka",
    "klaudia pach gomulnicka": "Klaudia Pach-Gomulnicka",
    "klaudia gomulnicka": "Klaudia Pach-Gomulnicka",
    "klaudia pachgomulnicka": "Klaudia Pach-Gomulnicka",
}
def _norm(s):
    mp = {'ą':'a','ć':'c','ę':'e','ł':'l','ń':'n','ó':'o','ś':'s','ź':'z','ż':'z'}
    return ''.join(mp.get(ch, ch) for ch in s.lower())

def canonical(name):
    n = _norm(name)
    if n in _VARIANTS: return _VARIANTS[n]
    if name in ROSTER: return name
    # roster "Imię Nazwisko"
    parts = name.split()
    if len(parts) == 2:
        first, last = parts
        inv = f"{last} {first}"
        if inv in ROSTER: return inv
    return name

# alternation over canonical roster names (longest-first) for name extraction
_roster_names = sorted(ROSTER, key=len, reverse=True)
_roster_re = re.compile(r'(?:' + '|'.join(re.escape(n) for n in _roster_names) + r')')

REQ_DELAY = 0.6
_LAST = 0.0
_ROMAN = {'I':1,'V':5,'X':10,'L':50,'C':100,'D':500,'M':1000}
def roman_to_int(s):
    tot=0; prev=0
    for ch in reversed(s.upper().strip()):
        v=_ROMAN.get(ch,0)
        if v<prev: tot-=v
        else: tot+=v; prev=v
    return tot

def _rate():
    global _LAST
    d=time.time()-_LAST
    if d<REQ_DELAY: time.sleep(REQ_DELAY-d)
    _LAST=time.time()

def _get(url, cache_dir):
    key=hashlib.md5(url.encode()).hexdigest()
    if cache_dir:
        cache_dir=Path(cache_dir); cache_dir.mkdir(parents=True, exist_ok=True)
        cf=cache_dir/(key+".dat")
        if cf.is_file(): return cf.read_bytes().decode("utf-8","ignore")
    from requests.exceptions import ConnectionError, Timeout
    for attempt in range(6):
        _rate()
        try:
            r=requests.get(url,headers={"User-Agent":"Mozilla/5.0 (Radoskop)"},timeout=60,verify=False)
            r.raise_for_status()
            data=r.text
            if cache_dir: (cache_dir/(key+".dat")).write_bytes(data.encode("utf-8","ignore"))
            return data
        except (ConnectionError,Timeout,OSError) as e:
            if attempt==5: raise
            time.sleep(3+attempt*4)
    raise RuntimeError(f"GET failed: {url}")

def discover_sessions(cache_dir):
    sessions=[]; seen=set()
    for year, path in YEAR_CATS.items():
        t=_get(BIP+path, cache_dir)
        for m in re.finditer(r'<a[^>]+href="([^"]+)"[^>]*>\s*(Protokół\s+nr[^<]*?)\s*</a>', t, re.S):
            href, label = m.group(1), unescape(re.sub(r'<[^>]+>','',m.group(2))).strip()
            if href in seen: continue
            seen.add(href)
            # roman + year from label "Protokół nr XXX 26 / XXX/26"
            rm = re.search(r'nr\s+([IVXLCDM]+)', label)
            rom = rm.group(1) if rm else None
            url = href if href.startswith('http') else BIP+href
            sessions.append({"rom": rom, "num": roman_to_int(rom) if rom else None,
                             "year": year, "url": url, "label": label})
    sessions.sort(key=lambda s: (s["num"] or 0))
    return sessions

_LABELS = ["ZA","PRZECIW","WSTRZYMUJĘ SIĘ","STRZYMUJĘ SIĘ","NIE GŁOSOWALI", "NIEOBECNI",
           "NIE GŁOSOWALI/NIEOBECNI", "NIE GŁOSOWALI / NIEOBECNI"]
_CAT = {"ZA":"za","PRZECIW":"przeciw","WSTRZYMUJĘ SIĘ":"wstrzymal_sie",
        "STRZYMUJĘ SIĘ":"wstrzymal_sie","NIE GŁOSOWALI":"brak","NIEOBECNI":"nieobecni",
        "NIE GŁOSOWALI/NIEOBECNI":"nieobecni","NIE GŁOSOWALI / NIEOBECNI":"nieobecni"}
_LABEL_RE = re.compile(
    r'^\s*(ZA|PRZECIW|WSTRZYMUJĘ SIĘ|STRZYMUJĘ SIĘ|NIE GŁOSOWALI ?[/ ]?NIEOBECNI|NIE GŁOSOWALI|NIEOBECNI)\s*\((\d+)\)\s*:?',
    re.M)
_NAME_RE = re.compile(
    r'\b[A-ZĄĆĘŁŃÓŚŹŻ][A-Za-ząćęłńóśźżĄĆĘŁŃÓŚŹŻ-]+ [A-ZĄĆĘŁŃÓŚŹŻ][A-Za-ząćęłńóśźżĄĆĘŁŃÓŚŹŻ-]+')
_NARR_CUT = re.compile(r'Z powodu problemu|Z powodu|radni:|Przebieg Sesji|W dyskusji', re.I)

def _name_filter(s):
    s = re.sub(r'\s+', ' ', s).strip().strip(' .')
    if not _NAME_RE.fullmatch(s):
        return None
    return canonical(s)

def _text_of(html):
    h=re.sub(r'<script.*?</script>','',html,flags=re.S)
    i=h.find('printArea')
    seg=h[i:] if i!=-1 else h
    j=seg.find('Data dodania')
    if j!=-1: seg=seg[:j]
    seg=re.sub(r'<[^>]+>','\n',seg)
    seg=unescape(seg)
    return seg

def parse_session(html):
    txt=_text_of(html)
    # session date
    date=None
    m=re.search(r'Obrady rozpoczęto\s+(\d{1,2})-(\d{1,2})-(\d{4})', txt)
    if m:
        d,mo,y=int(m.group(1)),int(m.group(2)),int(m.group(3))
        date=f"{y}-{mo:02d}-{d:02d}"
    # session roman (from "Protokół nr X/25" body)
    rom=None
    m=re.search(r'Protokół\s+nr\s+([IVXLCDM]+)\s*[/\s]\s*(\d{2})', txt)
    if m: rom=m.group(1)
    # split on 'Wyniki imienne:'
    marks=[mm.start() for mm in re.finditer(r'Wyniki imienne', txt)]
    records=[]; n_fail=0
    for i,wi in enumerate(marks):
        end=marks[i+1] if i+1<len(marks) else len(txt)
        seg=txt[wi:end]
        # parse label groups (tolerant of combined/variant category labels)
        named={}
        label_ps=list(_LABEL_RE.finditer(seg))
        if not label_ps:
            n_fail+=1; continue
        for li,lp in enumerate(label_ps):
            cat=_CAT[lp.group(1)]; expected=int(lp.group(2))
            seg_end=label_ps[li+1].start() if li+1<len(label_ps) else len(seg)
            raw=seg[lp.end():seg_end]
            # cut narrative that can follow the last list
            ncut=_NARR_CUT.search(raw)
            if ncut: raw=raw[:ncut.start()]
            for nm in re.split(r',', raw):
                nm=_name_filter(nm)
                if nm:
                    named.setdefault(cat,[])
                    if nm not in named[cat]: named[cat].append(nm)
        named.setdefault('za',[]); named.setdefault('przeciw',[])
        named.setdefault('wstrzymal_sie',[]); named.setdefault('brak',[])
        named.setdefault('nieobecni',[])
        # validate the 3 core Radoskop categories (za/przeciw/wstrzymuja)
        wanted={}
        for lp in label_ps:
            wanted[_CAT[lp.group(1)]]=int(lp.group(2))
        agg_ok=True
        for cat,exp in wanted.items():
            if cat in ("za","przeciw","wstrzymal_sie") and len(named[cat])!=exp:
                agg_ok=False
        if not agg_ok: n_fail+=1
        # topic: the last "w sprawie …" clause before this vote's "Wyniki imienne"
        pre=txt[max(0,wi-900):wi]
        ws=[m.start() for m in re.finditer(r'w sprawie', pre, re.I)]
        topic=None
        if ws:
            tail=re.sub(r'\s+',' ',pre[ws[-1]:])
            tail=re.split(r'\.\s', tail)[0]
            tail=re.split(r'\bgłosowanie uchwały|\(?\d{1,2}:\d{2}\b', tail)[0]
            tail=tail.strip().strip(' .')
            tail=re.sub(r'^(w sprawie)\s+', '', tail, flags=re.I)
            if tail:
                topic="Projekt uchwały w sprawie " + tail
        if not topic:
            tm=re.search(r'(?:^|\n\s*)(\d+\.\s*Rozpatrzenie i głosowanie[^\n]*)', pre)
            topic=tm.group(1).strip() if tm else "(uchwała)"
        if len(topic)>230:
            topic=topic[:230].rsplit(' ',1)[0]+' …'
        records.append({"topic":topic,"date":date,"num":roman_to_int(rom) if rom else None,
                        "named":named,"valid":agg_ok})
    return records,n_fail,rom,date

def make_slug(name):
    repl={'ą':'a','ć':'c','ę':'e','ł':'l','ń':'n','ó':'o','ś':'s','ź':'z','ż':'z',
          'Ą':'A','Ć':'C','Ę':'E','Ł':'L','Ń':'N','Ó':'O','Ś':'S','Ź':'Z','Ż':'Z'}
    slug=name.lower()
    for p,a in repl.items(): slug=slug.replace(p,a)
    return re.sub(r"[^a-z0-9]+","",slug)

def build_output(records, club_assign=None):
    club_assign=club_assign or {}
    all_votes=[]; vid=0; sessions_by_date={}
    for rec in records:
        d=rec["date"]
        if not d or d<KAD_START: continue
        if d not in sessions_by_date:
            sessions_by_date[d]={"date":d,"number":rec.get("num") or "","vote_count":0,"attendees":set(),"speakers":[]}
        sessions_by_date[d]["vote_count"]+=1
        named={k:list(v) for k,v in rec["named"].items()}
        for cat in ("za","przeciw","wstrzymal_sie","brak"):
            sessions_by_date[d]["attendees"].update(rec["named"].get(cat,[]))
        vid+=1
        all_votes.append({"id":str(vid),"session_date":d,"session_number":rec.get("num") or "",
                          "topic":rec.get("topic",""),"named_votes":named,
                          "counts":{k:len(named.get(k,[])) for k in ("za","przeciw","wstrzymal_sie")}})
    sessions_data=[]
    for d in sorted(sessions_by_date.keys()):
        s=sessions_by_date[d]
        sessions_data.append({"date":d,"number":s["number"],"vote_count":s["vote_count"],
                              "attendee_count":len(s["attendees"]),"attendees":sorted(s["attendees"]),"speakers":[]})
    all_names=set(ROSTER) | set(club_assign.keys())
    # diagnostic: names seen in votes but NOT in the canonical roster (should be empty)
    seen_vote_names=set()
    for v in all_votes:
        for names in v["named_votes"].values(): seen_vote_names.update(names)
    extra = seen_vote_names - all_names
    if extra:
        print(f"[glucholazy] WARN: vote names outside canonical roster: {sorted(extra)}")
    cd={}
    for name in sorted(all_names):
        cd[name]={"name":name,"club":club_assign.get(name,"NZ"),"district":None,
            "votes_za":0,"votes_przeciw":0,"votes_wstrzymal":0,"votes_brak":0,"votes_nieobecny":0,"rebellions":[]}
    for v in all_votes:
        for cat,names in v["named_votes"].items():
            for nm in names:
                if nm not in cd: continue
                if cat=="nieobecni": cd[nm]["votes_nieobecny"]+=1
                elif cat=="brak": cd[nm]["votes_brak"]+=1
                elif cat=="za": cd[nm]["votes_za"]+=1
                elif cat=="przeciw": cd[nm]["votes_przeciw"]+=1
                elif cat=="wstrzymal_sie": cd[nm]["votes_wstrzymal"]+=1
    total_votes=len(all_votes); total_sessions=len(sessions_data)
    csess=defaultdict(set)
    for v in all_votes:
        for cat,names in v["named_votes"].items():
            for nm in names: csess[nm].add(v["session_date"])
    cl=[]
    for c in sorted(cd.values(), key=lambda x:x["name"]):
        present=c["votes_za"]+c["votes_przeciw"]+c["votes_wstrzymal"]+c["votes_brak"]
        aktywn=(present/total_votes*100) if total_votes else 0
        frekw=(len(csess.get(c["name"],set()))/total_sessions*100) if total_sessions else 0
        cl.append({"name":c["name"],"club":c["club"],"district":None,
            "frekwencja":round(frekw,1),"aktywnosc":round(aktywn,1),"zgodnosc_z_klubem":0.0,
            "votes_za":c["votes_za"],"votes_przeciw":c["votes_przeciw"],
            "votes_wstrzymal":c["votes_wstrzymal"],"votes_brak":c["votes_brak"],
            "votes_nieobecny":c["votes_nieobecny"],"votes_total":total_votes,
            "rebellion_count":0,"rebellions":[],"has_activity_data":False,"activity":None})
    vectors=defaultdict(dict)
    for v in all_votes:
        for cat in ("za","przeciw","wstrzymal_sie"):
            for nm in v["named_votes"].get(cat,[]): vectors[nm][v["id"]]=cat
    pairs=[]; ns=sorted(vectors.keys())
    for a,b in combinations(ns,2):
        common=set(vectors[a].keys()) & set(vectors[b].keys())
        if len(common)<10: continue
        same=sum(1 for vid in common if vectors[a][vid]==vectors[b][vid])
        pairs.append({"a":a,"b":b,"club_a":"","club_b":"","score":round(same/len(common)*100,1),"common_votes":len(common)})
    pairs.sort(key=lambda x:x["score"],reverse=True)
    kad={"id":KADENCJA_ID,"label":KADENCJA_LABEL,
         "clubs":dict(Counter(club_assign.get(c["name"],"NZ") for c in cl)),
         "sessions":sessions_data,"total_sessions":total_sessions,
         "total_votes":total_votes,"total_councilors":len(cl),
         "councilors":cl,"votes":all_votes,
         "similarity_top":pairs[:20],"similarity_bottom":pairs[-20:][::-1]}
    return {"generated":datetime.now().isoformat(),"default_kadencja":KADENCJA_ID,"kadencje":[kad]}, total_votes,total_sessions

def build_profiles(records, club_assign=None):
    club_assign=club_assign or {}
    cv=defaultdict(lambda:{"za":0,"przeciw":0,"wstrzymal_sie":0,"brak":0,"nieobecni":0,"votes":[]})
    for rec in records:
        d=rec["date"]
        if not d or d<KAD_START: continue
        for cat,names in rec["named"].items():
            for nm in names:
                if cat in cv[nm]: cv[nm][cat]+=1
                cv[nm]["votes"].append({"session":d,"vote":cat})
    profiles=[]
    sess_set={r["date"] for r in records if r["date"]>=KAD_START}
    n_sessions=len(sess_set) or 1
    for nm in sorted(set(ROSTER)):
        vd=cv[nm]; total=sum(vd[k] for k in ("za","przeciw","wstrzymal_sie","brak")) or 1
        sess=len({v["session"] for v in vd["votes"]})
        aktywn=(vd["za"]+vd["przeciw"]+vd["wstrzymal_sie"])/n_sessions*100
        profiles.append({"name":nm,"slug":make_slug(nm),
            "kadencje":{KADENCJA_ID:{"club":club_assign.get(nm,"NZ"),"has_voting_data":True,
                "has_activity_data":False,"frekwencja":round(sess/n_sessions*100,1),
                "aktywnosc":round(aktywn,1),"zgodnosc_z_klubem":0.0,
                "votes_za":vd["za"],"votes_przeciw":vd["przeciw"],
                "votes_wstrzymal":vd["wstrzymal_sie"],"votes_brak":vd["brak"],
                "votes_nieobecny":0,"votes_total":total,
                "rebellion_count":0,"rebellions":[],"roles":[],"notes":"",
                "former":False,"mid_term":False}}})
    return {"profiles":profiles,"total":len(profiles)}

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--city-dir",required=True)
    ap.add_argument("--work-dir",default=None)
    ap.add_argument("--cache-dir",default=None)
    args=ap.parse_args()
    city_dir=Path(args.city_dir)
    work=Path(args.work_dir) if args.work_dir else city_dir/"work"; work.mkdir(parents=True,exist_ok=True)
    cache=Path(args.cache_dir) if args.cache_dir else None
    cfg={}
    if (city_dir/"config.json").is_file():
        cfg=json.loads((city_dir/"config.json").read_text(encoding="utf-8"))
    club_assign=cfg.get("club_assignments",{}) or {}
    sessions=discover_sessions(cache)
    print(f"[glucholazy] {len(sessions)} protokolow IX kad.")
    records=[]; n_fail=0; n_ok=0
    for se in sessions:
        try: html=_get(se["url"],cache)
        except Exception as e:
            print(f"  [skip] {se['rom']} pobieranie: {str(e)[:50]}"); continue
        recs,nf,rom,date=parse_session(html)
        if not recs:
            print(f"  [skip] {se['rom']} {date} brak glosowan (nf={nf})"); continue
        valid=sum(1 for r in recs if r["valid"])
        n_fail+=nf; n_ok+=1
        print(f"  [ok] {se['rom']:6s} {date} {len(recs):3d} glosowan ({valid}/{len(recs)} valid)")
        records.extend(recs)
    data,total_votes,total_sessions=build_output(records,club_assign)
    profiles=build_profiles(records,club_assign)
    docs=city_dir/"docs"; docs.mkdir(parents=True,exist_ok=True)
    (docs/"data.json").write_text(json.dumps(data,ensure_ascii=False),encoding="utf-8")
    (docs/"kadencja-2024-2029.json").write_text(json.dumps(data["kadencje"][0],ensure_ascii=False),encoding="utf-8")
    (docs/"profiles.json").write_text(json.dumps(profiles,ensure_ascii=False),encoding="utf-8")
    print(f"[glucholazy] DONE: {n_ok} protokolow, {total_votes} glosowan, {len(profiles['profiles'])} radnych, fail={n_fail}")

if __name__=="__main__":
    main()
