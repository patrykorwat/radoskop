#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Radoskop Miechów — imienne głosowania Rady Miejskiej w Miechowie (IX kadencja 2024-2029).

Źródło: BIP Urzędu Gminy i Miasta w Miechowie na platformie bip.malopolska.pl (Madkom SPA,
encja `ugimmiechow`), kategoria menu "Wyniki głosowań" (menuId 314396). Każdy artykuł =
jedna sesja; załącznik "Raport z głosowań" (PDF generowany przez app.esesja.pl) zawiera
per-głosowanie imienne w formacie:

    {N}. Głosowanie w sprawie {temat} - czas głosowania: {data}, godz. {hh:mm}, wyniki:
         ZA: a, PRZECIW: b, WSTRZYMUJĘ SIĘ: c, BRAK GŁOSU: d, NIEOBECNI: e
    Wyniki imienne: Imię Nazwisko (ZA), Imię Nazwisko (PRZECIW), ... (BRAK GŁOSU/NIEOBECNI)

Większość raportów ma warstwę tekstową. CZĘŚĆ SESJI 2024 (III, V, VI, VII) to ZESKANOWANE
PDF-y (bez warstwy tekstowej) — scraper ocruje je (tesseract -l pol --psm 6, render 200dpi),
a następnie ten sam parser przetwarza OCR. Nazwiska z OCR bywają wariantowe (Fłorek/Fiorek,
Wąwożny/Wąwoźny, Śliwań/Śliwoń) — każdy nazwisko kanonizujemy do ustalonego rostera 15
radnych (fuzzy-matching + agregat do walidacji). Sesja XXV (2026-05-28) ma USZKODZONY plik
raportu (nie-PDF) -> pomijana.

API Madkom (bez auth): /api/contexts/ugimmiechow, /api/menu/314396/articles?limit=N,
/api/articles/{id}, /api/files/{attachmentId}.

Sesje IX kad. z raportem: II..XXIX (brak raportu dla I inauguracyjnej, X, XV, XVII, XXI,
XXIV, XXVIII). Rooster 15 radnych kanoniczny z raportów tekstowych. Kluby: BIP nie publikuje
"Klubów Radnych" dla IX kad. (kategoria 311648 ma tylko 2018-2023) -> club_assignments
PENDING, wszyscy radni Niezrzeszeni (NZ).

Użycie:
    python scrape_miechow.py --output docs/data.json --profiles docs/profiles.json
        [--cache-dir .cache] [--max-sessions N]
"""

import argparse
import json
import re
import subprocess
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from io import BytesIO
from itertools import combinations
from pathlib import Path

import pdfplumber
import requests

BASE = "https://bip.malopolska.pl"
ENTITY = "ugimmiechow"
GLOS_MENU = 314396
KAD_START = "2024-05-07"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36"}

ROSTER = ["Arkadiusz Kluska","Jacek Uchto","Jarosław Florek","Krzysztof Wołkowski",
          "Lidia Baranowska","Marcin Florek","Mateusz Sobecki","Piotr Micuła",
          "Rafał Michalski","Roman Piwowarski","Roman Wąwoźny","Stanisław Pietras",
          "Wojciech Tambor","Wojciech Śliwoń","Łukasz Janas"]

_MON = {'stycznia':1,'lutego':2,'marca':3,'kwietnia':4,'maja':5,'czerwca':6,
        'lipca':7,'sierpnia':8,'września':9,'października':10,'listopada':11,'grudnia':12}
_MON.update({'styczen':1,'luty':2,'marzec':3,'kwiecien':4,'maj':5,'czerwiec':6,
             'lipiec':7,'sierpien':8,'wrzesien':9,'pazdziernik':10,'listopad':11,'grudzien':12})

_VOTE_NORM = {
    'ZA':'za','PRZECIW':'przeciw','WSTRZYMUJĘ SIĘ':'wstrzymal_sie',
    'WSTRZYMUJE SIĘ':'wstrzymal_sie','BRAK GŁOSU':'brak','BRAK GLOSU':'brak',
    'NIEOBECNI':'nieobecny','NIEOBECNY':'nieobecny','NIEOBECNA':'nieobecny',
}
_ACC = str.maketrans({'ą':'a','ć':'c','ę':'e','ł':'l','ń':'n','ó':'o','ś':'s','ź':'z','ż':'z'})
def _norm(s):
    return re.sub(r"[^a-z0-9]", "", s.lower().translate(_ACC))
def make_slug(name):
    repl={'ą':'a','ć':'c','ę':'e','ł':'l','ń':'n','ó':'o','ś':'s','ź':'z','ż':'z'}
    sn=str(name or "").lower()
    for pl,a in repl.items(): sn=sn.replace(pl,a)
    sn=re.sub(r"[^a-z0-9]+","-",sn); return sn.strip("-")

# canonical roster normalized
_ROSTER_NORM = [_norm(n) for n in ROSTER]

def canon_name(raw, row_votes):
    """Map an (OCR-corrupted) name to the canonical roster; fallback to best surname match."""
    raw=re.sub(r'^\d+\.\s*','', (raw or '').strip())
    if not raw: return raw
    rn=_norm(raw)
    for c,cn in zip(ROSTER,_ROSTER_NORM):
        if rn==cn: return c
    # surname match: last token of raw vs last token of canonical
    raw_last=_norm(raw.split()[-1])
    for c,cn in zip(ROSTER,_ROSTER_NORM):
        cn_last=cn.split()[-1]
        if raw_last==cn_last:
            return c
    # fuzzy whole-name via difflib
    from difflib import SequenceMatcher
    best=None;best_s=0
    for c,cn in zip(ROSTER,_ROSTER_NORM):
        s=SequenceMatcher(None,rn,cn).ratio()
        if s>best_s: best_s=s;best=c
    return best if best_s>=0.6 else (raw.strip() or best)

def _date_from_title(title):
    m=re.search(r'w\s+dniu\s+(\d{1,2})\s+(\w+)\s+(\d{4})', title, re.I)
    if not m: return None
    mo=_MON.get(m.group(2).lower())
    if not mo: return None
    return f"{m.group(3)}-{mo:02d}-{int(m.group(1)):02d}"
def _roman_from_title(title):
    m=re.search(r'(?:z\s+)?([IVXLCDM]+)\s+[Ss]esj', title)
    return m.group(1) if m else ""

def get_json(url, retries=3):
    for i in range(retries):
        try:
            r=requests.get(url, headers=UA, timeout=40, verify=False)
            if r.status_code==200: return r.json()
        except Exception:
            time.sleep(1.5*(i+1))
    raise RuntimeError(f"GET failed: {url}")
def get_file(url, retries=3):
    for i in range(retries):
        try:
            r=requests.get(url, headers=UA, timeout=60, verify=False)
            if r.status_code==200: return r.content
        except Exception:
            time.sleep(1.5*(i+1))
    raise RuntimeError(f"GET failed: {url}")

def collect_articles():
    d=get_json(f"{BASE}/api/menu/{GLOS_MENU}/articles?limit=200")
    out=[]
    for a in d.get("articles") or []:
        aid=a["id"]
        art=get_json(f"{BASE}/api/articles/{aid}")
        title=art.get("title") or ""
        sdate=_date_from_title(title)
        if not sdate or sdate < KAD_START: continue
        att=None
        for x in art.get("attachments") or []:
            nm=" ".join(str(x.get(k) or "") for k in ("name","fileName","title"))
            if "raport" in nm.lower() or "glosow" in nm.lower(): att=x; break
        if att is None and (art.get("attachments") or []): att=art["attachments"][0]
        if att is None: continue
        out.append({"id":aid,"title":title,"date":sdate,"num":_roman_from_title(title),
                    "att":att.get("id")})
    by_date={}
    for r in out: by_date.setdefault(r["date"], r)
    return list(by_date.values())

_HEADER_RE = re.compile(
    r'Głosow[ao]nie\s+w\s+sprawie\s*(.*?)\s*-\s*czas\s+głosowania:\s*([^,]+?),?\s*godz\.\s*([\d:.]+),?\s*wyniki:\s*'
    r'ZA:\s*(\d+),\s*PRZECIW:\s*(\d+),\s*WSTRZYMUJ[ĘE]\s*SI[ĘE]:\s*(\d+),\s*BRAK\s*G[ŁL]OSU:\s*(\d+),\s*NIEOBECNI:\s*(\d+)',
    re.I|re.S)
_NAME_RE = re.compile(r'([^(),]+?)\s*\(([^()]+)\)')

def extract_pdf_text(content, cache_dir=None, pdf_key=None):
    """Try text layer; if scanned (chars<200), OCR via render+tesseract (cached)."""
    try:
        with pdfplumber.open(BytesIO(content)) as p:
            text="\n".join((pg.extract_text() or '') for pg in p.pages)
        if len(text.strip())>=200:
            return text
    except Exception:
        text=""
    # scanned -> OCR
    ocr_dir = None
    if cache_dir:
        ocr_dir = Path(cache_dir)/"ocr"; ocr_dir.mkdir(parents=True, exist_ok=True)
    out=[]
    try:
        with pdfplumber.open(BytesIO(content)) as p:
            for i,pg in enumerate(p.pages):
                cachef = (ocr_dir/f"{pdf_key}_p{i}.txt") if (ocr_dir and pdf_key) else None
                if cachef and cachef.exists():
                    out.append(cachef.read_text(encoding='utf-8', errors='ignore'))
                    continue
                im=pg.to_image(resolution=200)
                png=BytesIO(); im.save(png, format="PNG")
                png.seek(0)
                res=subprocess.run(["tesseract","-","-","-l","pol","--psm","6"],
                                   input=png.getvalue(), capture_output=True, timeout=120)
                txt=(res.stdout or b"").decode("utf-8", errors="ignore")
                out.append(txt)
                if cachef:
                    cachef.write_text(txt, encoding="utf-8")
    except Exception:
        return ""
    return "\n".join(out)

def parse_pdf(text):
    text = re.sub(r'Wyniki\s+imienne\s*:', 'Wyniki imienne:', text, flags=re.I)
    heads=[]
    for m in _HEADER_RE.finditer(text):
        heads.append({"topic":re.sub(r'\s+',' ',m.group(1)).strip(),
                      "agg":{"za":int(m.group(4)),"przeciw":int(m.group(5)),
                             "wstrzymal_sie":int(m.group(6)),"brak":int(m.group(7)),
                             "nieobecny":int(m.group(8))},
                      "start":m.start(),"end":m.end()})
    if not heads:
        return []
    votes=[]
    for i,h in enumerate(heads):
        seg_end = heads[i+1]["start"] if i+1<len(heads) else len(text)
        seg = text[h["end"]:seg_end]
        inm = re.search(r'Wyniki\s*imienne:\s*(.*?)(?=\n\s*(?:\d+\.)?\s*Głosow[ao]nie\s+w\s+sprawie|\Z)', seg, re.I|re.S)
        raw=[]
        if inm:
            for nm in _NAME_RE.finditer(inm.group(1)):
                name=re.sub(r'^\d+\.\s*','',nm.group(1)).strip()
                v=_VOTE_NORM.get(nm.group(2).strip().upper(),'brak')
                raw.append((name,v))
        votes.append({"topic":h["topic"],"agg":h["agg"],"raw_named":raw})
    return votes

def build_named(raw):
    named={'za':[],'przeciw':[],'wstrzymal_sie':[],'brak':[],'nieobecny':[]}
    for name,vote in raw:
        named[vote].append(name)
    for k in named: named[k]=list(dict.fromkeys(named[k]))
    return named

def validate_and_repair(votes):
    """Validate each vote's named counts vs aggregate; repair unknown-vote names by deficit."""
    fixed=0; mism=0
    for v in votes:
        if not v["raw_named"]: continue
        named=build_named(v["raw_named"])
        ag=v["agg"]
        counts={k:len(vv) for k,vv in named.items()}
        # unknown-token names landed in 'brak' bucket; check totals
        want={"za":ag["za"],"przeciw":ag["przeciw"],"wstrzymal_sie":ag["wstrzymal_sie"],
              "brak":ag["brak"],"nieobecny":ag["nieobecny"]}
        if all(counts[k]==want[k] for k in want):
            continue
        # try deficit-based repair: names in 'brak' may actually belong to a short category
        short={k:want[k]-counts[k] for k in want if want[k]-counts[k]>0}
        # names currently unassigned to a definitive category (in 'brak' because token glitch)
        # We cannot reliably reassign; just count mismatch.
        mism+=1
    return mism

def main():
    ap=argparse.ArgumentParser(prog="Radoskop Miechów")
    ap.add_argument("--output", required=True)
    ap.add_argument("--profiles", required=True)
    ap.add_argument("--cache-dir", default=".cache")
    ap.add_argument("--max-sessions", type=int, default=0)
    args=ap.parse_args()

    arts=collect_articles()
    if args.max_sessions>0: arts=arts[:args.max_sessions]
    arts.sort(key=lambda r:r["date"])

    occount=0; skipped=[]
    all_records=[]  # (date, num, votes)
    for r in arts:
        key=f"{r['date']}_{r['att']}"
        try:
            content=get_file(f"{BASE}/api/files/{r['att']}")
        except Exception:
            skipped.append((r['date'],'dl-file')); continue
        text=extract_pdf_text(content, cache_dir=args.cache_dir, pdf_key=key)
        if not text.strip():
            skipped.append((r['date'],'empty')); continue
        if len(text)<200:  # corrupt/short
            skipped.append((r['date'],'shorttext')); continue
        votes=parse_pdf(text)
        if votes:
            all_records.append((r['date'],r['num'],votes))
        else:
            skipped.append((r['date'],'novotes'))
    # skipped
    print("SKIPPED:", skipped, flush=True)

    # canonicalize names (map OCR variants to roster) and DROP non-roster names
    # (footer "Przygotował(a): ...", OCR garbage — real voters are always in ROSTER)
    for d,num,votes in all_records:
        for v in votes:
            fixed=[]
            for (n,vv) in v["raw_named"]:
                cn=canon_name(n,None)
                if cn in ROSTER:
                    fixed.append((cn,vv))
            v["raw_named"]=fixed

    # build all_votes + sessions
    validated={'ok':0,'mismatch':0,'noim':0}
    all_votes=[]; vid=0; by_date=defaultdict(list)
    for d,num,votes in all_records:
        for v in votes:
            named=build_named(v["raw_named"])
            if not named["za"] and not named["przeciw"] and not named["wstrzymal_sie"]:
                validated['noim']+=1
                continue
            ag=v["agg"]
            if (len(named["za"])==ag["za"] and len(named["przeciw"])==ag["przeciw"]
                and len(named["wstrzymal_sie"])==ag["wstrzymal_sie"]
                and len(named["brak"])==ag["brak"]):
                validated["ok"]+=1
            else:
                validated["mismatch"]+=1
            vid+=1
            rec={'id':str(vid),'source_url':f"{BASE}/api/menu/{GLOS_MENU}/articles?limit=200",
                 'session_date':d,'session_number':str(num),'topic':v["topic"],'druk':'','resolution':'',
                 'counts':{'za':len(named["za"]),'przeciw':len(named["przeciw"]),
                           'wstrzymal_sie':len(named["wstrzymal_sie"])},'named_votes':named}
            all_votes.append(rec); by_date[d].append(rec)

    sessions_data=[]
    for d in sorted(by_date.keys()):
        sv=by_date[d]; att=set()
        for v in sv:
            for cat,ns in v["named_votes"].items(): att.update(ns)
        num=next((n for (dd,n,vv) in all_records if dd==d),'')
        sessions_data.append({'date':d,'number':str(num),'vote_count':len(sv),
                              'attendee_count':len(att),'attendees':sorted(att),'speakers':[]})
    print("Walldacja:", validated, flush=True)

    total_votes=len(all_votes); total_sessions=len(sessions_data)
    all_names=set()
    for v in all_votes:
        for ns in v["named_votes"].values(): all_names.update(ns)
    cdata={n:{'name':n,'club':'','votes_za':0,'votes_przeciw':0,'votes_wstrzymal':0,'votes_brak':0,'votes_nieobecny':0} for n in all_names}
    for v in all_votes:
        for cat,ns in v["named_votes"].items():
            for n in ns:
                if n not in cdata: continue
                c=cdata[n]
                if cat=='za': c['votes_za']+=1
                elif cat=='przeciw': c['votes_przeciw']+=1
                elif cat=='wstrzymal_sie': c['votes_wstrzymal']+=1
                elif cat=='brak': c['votes_brak']+=1
                else: c['votes_nieobecny']+=1
    counc_sess=defaultdict(set)
    for v in all_votes:
        for cat,ns in v["named_votes"].items():
            for n in ns: counc_sess[n].add(v['session_date'])
    councilors_list=[]
    for c in sorted(cdata.values(), key=lambda x:x['name']):
        present=c['votes_za']+c['votes_przeciw']+c['votes_wstrzymal']+c['votes_brak']
        aktywnosc=present/total_votes*100 if total_votes else 0
        frekwencja=len(counc_sess[c['name']])/total_sessions*100 if total_sessions else 0
        councilors_list.append({'name':c['name'],'club':'','frekwencja':round(frekwencja,1),
            'aktywnosc':round(aktywnosc,1),'zgodnosc_z_klubem':0.0,'votes_za':c['votes_za'],
            'votes_przeciw':c['votes_przeciw'],'votes_wstrzymal':c['votes_wstrzymal'],
            'votes_brak':c['votes_brak'],'votes_nieobecny':c['votes_nieobecny'],'votes_total':total_votes,
            'rebellion_count':0,'rebellions':[],'has_activity_data':False,'activity':None})
    vectors=defaultdict(dict)
    for v in all_votes:
        for cat in ('za','przeciw','wstrzymal_sie'):
            for n in v['named_votes'].get(cat,[]): vectors[n][v['id']]=cat
    pairs=[]; ns=sorted(vectors.keys())
    for a,b in combinations(ns,2):
        common=set(vectors[a].keys())&set(vectors[b].keys())
        if len(common)<10: continue
        same=sum(1 for v in common if vectors[a][v]==vectors[b][v])
        pairs.append({'a':a,'b':b,'club_a':'','club_b':'','score':round(same/len(common)*100,1),'common_votes':len(common)})
    pairs.sort(key=lambda x:x['score'],reverse=True)
    kad={'id':KADENCJA_ID,'label':KADENCJA_LABEL,'clubs':{},'sessions':sessions_data,
         'total_sessions':total_sessions,'total_votes':total_votes,'total_councilors':len(councilors_list),
         'councilors':councilors_list,'votes':all_votes,'similarity_top':pairs[:20],'similarity_bottom':pairs[-20:][::-1]}
    output={'generated':datetime.now().isoformat(),'default_kadencja':KADENCJA_ID,'kadencje':[kad]}

    cv=defaultdict(lambda:{'za':0,'przeciw':0,'wstrzymal_sie':0,'nieobecny':0,'brak':0,'votes':[]})
    for v in all_votes:
        d=v['session_date']
        for cat,ns in v['named_votes'].items():
            for n in ns:
                cv[n][cat]+=1; cv[n]['votes'].append({'session':d,'vote':cat})
    profiles=[]
    for name in sorted(cv.keys()):
        vd=cv[name]
        total=sum(vd[k] for k in ('za','przeciw','wstrzymal_sie','nieobecny','brak')) or 1
        present_sess=len({x['session'] for x in vd['votes'] if x['vote']!='nieobecny'})
        all_sess=len({x['session'] for x in vd['votes']})
        frekw=100.0*present_sess/all_sess if all_sess else 0.0
        profiles.append({'name':name,'slug':make_slug(name),'kadencje':{KADENCJA_ID:{
            'club':'','has_voting_data':True,'has_activity_data':False,'frekwencja':round(frekw,1),
            'aktywnosc':0.0,'zgodnosc_z_klubem':0.0,'votes_za':vd['za'],'votes_przeciw':vd['przeciw'],
            'votes_wstrzymal':vd['wstrzymal_sie'],'votes_brak':vd['brak'],'votes_nieobecny':vd['nieobecny'],
            'votes_total':total,'rebellion_count':0,'rebellions':[],'roles':[],'notes':''}}})
    prof={'profiles':profiles,'total':len(profiles)}

    out_path=Path(args.output); out_path.parent.mkdir(parents=True,exist_ok=True)
    index={'generated':output.get('generated',''),'default_kadencja':output.get('default_kadencja',''),'kadencje':[]}
    for kad in output['kadencje']:
        kid=kad['id']
        (out_path.parent/f'kadencja-{kid}.json').write_text(json.dumps(kad,ensure_ascii=False,separators=(",",":")),encoding='utf-8')
        index['kadencje'].append({'id':kid,'label':kad.get('label','')})
    out_path.write_text(json.dumps(index,ensure_ascii=False,separators=(",",":")),encoding='utf-8')
    Path(args.profiles).parent.mkdir(parents=True,exist_ok=True)
    Path(args.profiles).write_text(json.dumps(prof,ensure_ascii=False,separators=(",",":")),encoding='utf-8')
    print(f"\nZapisano: {total_sessions} sesji, {total_votes} głosowań, {len(councilors_list)} radnych, skipped={skipped}")
    print("=>", out_path, "|", args.profiles)

if __name__=="__main__":
    main()
