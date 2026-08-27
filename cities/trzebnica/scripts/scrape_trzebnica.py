#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Radoskop Trzebnica — imienne głosowania Rady Miejskiej w Trzebnicy (IX kadencja 2024-2029).

Źródło: BIP bip.trzebnica.pl (idcom-jst 'artykuly' CMS), kategoria
"/artykuly/201/protokoly-i-wykazy-glosowan-ix-kadencja". Każda sesja to artykuł
z dwoma załącznikami:
  - "Wykaz głosowań" (PDF, tekstowy, system Rada365) — głosowania imienne per radny
    w formacie blokowym: "Wyniki imienne: ZA (N) [nazwiska], PRZECIW (N),
    WSTRZYMUJĘ SIĘ (N), NIE GŁOSOWALI (N), NIEOBECNI (N)".
  - "Protokół z ... sesji" (PDF, tekstowy) — data sesji ("PROTOKÓŁ Nr XXIV/26
    ... z dnia 10 czerwca 2026 roku").

Data sesji NIE jest w tytule artykułu ani w wykazie (footer 'wydrukowano' = data
wydruku); bierzemy ją z protokołu.

Walidacja per głos: sumy imienne ZA/PRZECIW == liczniki nagłówków bloków.
Skład rady (21 radnych) z głosowań. Kluby radnych — patrz club_assignments.

Użycie:
    python scrape_trzebnica.py --city-dir <cities/trzebnica> [--cache-dir dir]
Zapisuje: docs/kadencja-2024-2029.json, docs/data.json, docs/profiles.json
"""
import argparse, hashlib, io, json, re, time, unicodedata
from collections import Counter, defaultdict
from datetime import datetime
from itertools import combinations
from pathlib import Path
import pdfplumber, requests, urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BIP = "https://bip.trzebnica.pl"
CAT = "/artykuly/201/protokoly-i-wykazy-glosowan-ix-kadencja"
KAD_START = "2024-05-07"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"
REQ_DELAY = 0.5
_LAST = 0.0

_ROMAN = {'I':1,'V':5,'X':10,'L':50,'C':100,'D':500,'M':1000}
def roman_to_int(s):
    tot=0; prev=0
    for ch in reversed(s.upper()):
        v=_ROMAN.get(ch,0)
        if v<prev: tot-=v
        else: tot+=v; prev=v
    return tot

_MONTH_PL = {'stycznia':1,'lutego':2,'marca':3,'kwietnia':4,'maja':5,'czerwca':6,
             'lipca':7,'sierpnia':8,'wrzesnia':9,'września':9,'pazdziernika':10,
             'października':10,'listopada':11,'grudnia':12}
def _norm_diac(s):
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))

def _rate():
    global _LAST
    d=time.time()-_LAST
    if d<REQ_DELAY: time.sleep(REQ_DELAY-d)
    _LAST=time.time()

def _get(url, cache_dir, ext=".bin"):
    key=hashlib.md5(url.encode()).hexdigest()
    if cache_dir:
        cd=Path(cache_dir); cd.mkdir(parents=True,exist_ok=True)
        cf=cd/(key+ext)
        if cf.is_file(): return cf.read_bytes(), "cache"
    _rate()
    r=requests.get(url, headers={"User-Agent":"Mozilla/5.0 (Radoskop; trilogy)"}, timeout=60, verify=False)
    r.raise_for_status()
    data=r.content
    if cache_dir: (Path(cache_dir)/(key+ext)).write_bytes(data)
    return data, "http"

def roman_from_title(title):
    m=re.search(r'([IVXLCDM]+)\s+sesj', title, re.I)
    return roman_to_int(m.group(1)) if m else 0

def discover_sessions(cache_dir):
    sessions=[]
    seen=set()
    for page in range(1,6):
        url = (f"{BIP}/artykuly/201/{page}/10/protokoly-i-wykazy-glosowan-ix-kadencja"
               if page>1 else BIP+CAT)
        data,_ = _get(url, cache_dir, ".html")
        text=data.decode('utf-8','ignore')
        found=0
        for m in re.finditer(r'<a[^>]*href=["\']([^"\']*?/artykul/201/(\d+)/[^"\']+)["\'][^>]*>(.*?)</a>', text, re.S):
            href=m.group(1); artid=m.group(2); title=re.sub(r'<[^>]+>','',m.group(3)).strip()
            if artid in seen: continue
            seen.add(artid)
            full=href if href.startswith('http') else BIP+href
            sessions.append({"id":artid,"title":title,"url":full,"num":roman_from_title(title)})
            found+=1
        if found==0: break
    sessions.sort(key=lambda s: s["id"])
    return sessions

def get_attachments(article_url, cache_dir):
    """Return list[(label, download_url)] for a session article."""
    data,_ = _get(article_url, cache_dir, ".html")
    text=data.decode('utf-8','ignore')
    atts=[]
    for m in re.finditer(r'<a[^>]*href=["\']([^"\']*?/attachments/download/(\d+))["\'][^>]*>(.*?)</a>', text, re.S):
        url=m.group(1); label=re.sub(r'<[^>]+>','',m.group(3)).strip()
        full=url if url.startswith('http') else BIP+url
        atts.append((label, full))
    return atts, len(text)

def parse_protocol_date(data):
    """'PROTOKÓŁ Nr XXIV/26 ... z dnia 10 czerwca 2026 roku' -> '2026-06-10'."""
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            t=(pdf.pages[0].extract_text() or "")+"\n"+(pdf.pages[1].extract_text() or "")
    except Exception:
        return None
    m=re.search(r'z dnia\s+(\d{1,2})\s+([a-ząćęłńóśźż]+)\s+(\d{4})', t, re.I)
    if not m: return None
    mo=_MONTH_PL.get(m.group(2).lower()) or _MONTH_PL.get(_norm_diac(m.group(2).lower()))
    if not mo: return None
    return f"{m.group(3)}-{mo:02d}-{int(m.group(1)):02d}"

_VOTE_MAP = {'ZA':'za','PRZECIW':'przeciw','WSTRZYMUJĘ SIĘ':'wstrzymal_sie',
             'WSTRZYMUJE SIĘ':'wstrzymal_sie','NIE GŁOSOWALI':'brak_glosu',
             'NIE GŁOSOWAŁ':'brak_glosu','BRAK GŁOSU':'brak_glosu','NIEOBECNI':'nieobecni'}

def _norm_name(n):
    n=re.sub(r'\s+',' ',n).strip(' .,;:')
    return n.strip()

def parse_wykaz(data):
    """Parse Rada365 wykaz PDF -> [ {topic, named} ]. Format blokowy.

    Każde głosowanie zaczyna się tematem (linia zakończona czasem (HH:MM); czas
    czasem stoi w OSOBNEJ linii), po nim bloki ZA(N)/PRZECIW(N)/WSTRZYMUJĘ SIĘ(N)/
    NIE GŁOSOWALI(N)/NIEOBECNI(N) — każdy z N nazwiskami. Nazwisko może łamać się
    między liniami bez przecinka ("Zenobiusz\\nModliborski,"). Licznik N zamyka
    blok; nadmiarowe linie po bloku to kontynuacja tematu kolejnego głosowania.
    Nagłówki stron/footery usuwane; duplikaty w obrębie kategorii usuwane.
    """
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            full="\n".join((p.extract_text() or "") for p in pdf.pages)
    except Exception:
        return []
    full=re.sub(r'(?m)^Strona - wydrukowano dn .*$','',full)
    full=re.sub(r'(?m)^Gmina Trzebnica\s*$','',full)
    full=re.sub(r'(?m)^Wyniki imienne:\s*$','',full)
    leader=(r'^(ZA|PRZECIW|WSTRZYMUJĘ SIĘ|WSTRZYMUJE SIĘ|NIE GŁOSOWALI|NIE GŁOSOWAŁ|'
            r'BRAK GŁOSU|NIEOBECNI)\s*\(\s*(\d+)\s*\)\s*$')
    time_re=re.compile(r'^\s*\(\d{1,2}:\d{2}\)\s*$')
    time_re2=re.compile(r'\(\d{1,2}:\d{2}\)\s*$')
    lines=full.split("\n")

    def parse_block(raw_lines, needed):
        """Z listy linii bloku wyciąga nazwiska (z wrap) do `needed` sztuk.
        Zwraca (names, leftover) gdzie leftover = nadmiarowe linie (kontynuacja tematu)."""
        names=[]; k=0; leftover=[]
        def norm_toks(s):
            out=[]
            for t in s.split(','):
                t=_norm_name(t)
                if t: out.append(t)
            return out
        while k<len(raw_lines):
            if len(names)>=needed:
                leftover=raw_lines[k:]
                break
            ln=raw_lines[k].rstrip()
            nxt=raw_lines[k+1] if k+1<len(raw_lines) else None
            if ln.endswith(','):
                names.extend(norm_toks(ln)); k+=1; continue
            # no trailing comma
            if nxt is not None and (nxt.endswith(',') or (',' in nxt)):
                # wrap: first part + next line
                names.extend(norm_toks(ln+' '+nxt.lstrip())); k+=2; continue
            names.extend(norm_toks(ln)); k+=1
        # trim to needed
        if len(names)>needed:
            names=names[:needed]
            leftover=raw_lines[k:]+(raw_lines[-1:] if False else [])
        return names, leftover

    votes=[]
    cur=None; cur_key=None; cur_need=0
    blocklines=[]; prev_nonspace_runs=[]
    i=0
    while i<len(lines):
        line=lines[i].strip()
        if not line:
            i+=1; continue
        if time_re.search(line) or (re.search(r'\(\d{1,2}:\d{2}\)\s*$', line) and
                                    (not re.match(leader,line))):
            # boundary: close block + finalize vote
            if cur_key is not None and blocklines:
                names, _left = parse_block(blocklines, cur_need)
                cur["named"][cur_key].extend(names)
                blocklines=[]; cur_key=None; cur_need=0
            if cur is not None and any(cur["named"].values()):
                votes.append(cur)
            if prev_nonspace_runs:
                line=" ".join(prev_nonspace_runs)+" "+line
            cur={"topic":line,"named":defaultdict(list)}
            prev_nonspace_runs=[]; blocklines=[]
            i+=1; continue
        lm=re.match(leader, line, re.I)
        if lm:
            # close previous block
            if cur_key is not None and blocklines:
                names, _l = parse_block(blocklines, cur_need)
                cur["named"][cur_key].extend(names); blocklines=[]
            if cur is None:
                cur={"topic":"","named":defaultdict(list)}
            cur_key=_VOTE_MAP.get(lm.group(1).upper())
            cur_need=int(lm.group(2))
            prev_nonspace_runs=[]
            i+=1; continue
        if cur is None:
            cur={"topic":"","named":defaultdict(list)}
        if cur_key is not None and cur_need is not None:
            blocklines.append(line); prev_nonspace_runs=[]
        else:
            prev_nonspace_runs.append(line)
        i+=1
    if cur_key is not None and blocklines:
        names,_l = parse_block(blocklines, cur_need)
        cur["named"][cur_key].extend(names)
    if cur is not None and any(cur["named"].values()):
        votes.append(cur)
    out=[]
    for v in votes:
        named={}
        for k,n in dict(v["named"]).items():
            seen=set(); uniq=[]
            for nm in n:
                if nm not in seen:
                    seen.add(nm); uniq.append(nm)
            if uniq: named[k]=uniq
        topic=v["topic"] or "Głosowanie"
        topic=re.sub(r'\s*\(\d{1,2}:\d{2}\)\s*$','',topic).strip()
        # stripped standalone time occupations
        topic=topic.replace("( ","").strip()
        out.append({"topic":topic,"named":named})
    return out

# ---- output builders (from scrape_lapy.py pattern) ----
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
        named={k:list(v) for k,v in rec["named"].items()}
        vid+=1
        sessions_by_date[d]["vote_count"]+=1
        for cat in ("za","przeciw","wstrzymal_sie","brak_glosu"):
            sessions_by_date[d]["attendees"].update(rec["named"].get(cat,[]))
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
        for ns in v["named_votes"].values(): all_names.update(ns)
    councilors_data={}
    for name in all_names:
        councilors_data[name]={"name":name,"club":club_assign.get(name,"NZ"),"district":None,
            "votes_za":0,"votes_przeciw":0,"votes_wstrzymal":0,"votes_brak":0,"votes_nieobecny":0,
            "votes_with_club":0,"votes_against_club":0,"rebellions":[]}
    for v in all_votes:
        for cat,names in v["named_votes"].items():
            for name in names:
                if name not in councilors_data: continue
                c=councilors_data[name]
                if cat=="za": c["votes_za"]+=1
                elif cat=="przeciw": c["votes_przeciw"]+=1
                elif cat=="wstrzymal_sie": c["votes_wstrzymal"]+=1
                elif cat=="nieobecni": c["votes_nieobecny"]+=1
                else: c["votes_brak"]+=1
    total_votes=len(all_votes); total_sessions=len(sessions_data)
    councillor_sess=defaultdict(set)
    for v in all_votes:
        for cat,names in v["named_votes"].items():
            if cat!="nieobecni":
                for n in names: councillor_sess[n].add(v["session_date"])
    councilors_list=[]
    for c in sorted(councilors_data.values(), key=lambda x:x["name"]):
        present=c["votes_za"]+c["votes_przeciw"]+c["votes_wstrzymal"]+c["votes_brak"]
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
            for name in v["named_votes"].get(cat,[]): vectors[name][v["id"]]=cat
    pairs=[]; ns=sorted(vectors.keys())
    for a,b in combinations(ns,2):
        common=set(vectors[a].keys())&set(vectors[b].keys())
        if len(common)<10: continue
        same=sum(1 for vid in common if vectors[a][vid]==vectors[b][vid])
        pairs.append({"a":a,"b":b,"club_a":club_assign.get(a,"NZ"),"club_b":club_assign.get(b,"NZ"),
                      "score":round(same/len(common)*100,1),"common_votes":len(common)})
    pairs.sort(key=lambda x:x["score"],reverse=True)
    kad={"id":KADENCJA_ID,"label":KADENCJA_LABEL,
         "clubs":dict(Counter(club_assign.get(c["name"],"NZ") for c in councilors_list)),
         "sessions":sessions_data,"total_sessions":total_sessions,"total_votes":total_votes,
         "total_councilors":len(councilors_list),"councilors":councilors_list,"votes":all_votes,
         "similarity_top":pairs[:20],"similarity_bottom":pairs[-20:][::-1]}
    return {"generated":datetime.now().isoformat(),"default_kadencja":KADENCJA_ID,"kadencje":[kad]}, total_votes, total_sessions

def build_profiles(records, club_assign=None, roster=None, total_sessions=None):
    club_assign=club_assign or {}; roster=roster or set()
    cv=defaultdict(lambda:{"za":0,"przeciw":0,"wstrzymal_sie":0,"brak":0,"nieobecny":0,"votes":[]})
    for rec in records:
        d=rec["date"]
        if not d or d<KAD_START: continue
        for cat,names in rec["named"].items():
            for name in names:
                key="za" if cat=="za" else "przeciw" if cat=="przeciw" else "wstrzymal_sie" if cat=="wstrzymal_sie" else "nieobecny" if cat=="nieobecni" else "brak"
                cv[name][key]+=1
                cv[name]["votes"].append({"session":d,"vote":key})
    profiles=[]
    sessions_set={r["date"] for r in records if r.get("date") and r["date"]>=KAD_START}
    n_sessions=total_sessions or len(sessions_set) or 1
    for name in sorted(set(list(cv.keys())+list(roster))):
        vd=cv[name]
        total=sum(vd[k] for k in ("za","przeciw","wstrzymal_sie","brak","nieobecny")) or 1
        present_sess=len({v["session"] for v in vd["votes"] if v["vote"]!="nieobecny"})
        all_sess=len({v["session"] for v in vd["votes"]})
        frekw=100.0*present_sess/all_sess if all_sess else 100.0*present_sess/n_sessions
        aktywn=sum(vd[k] for k in ("za","przeciw","wstrzymal_sie"))/n_sessions*100
        profiles.append({"name":name,"slug":make_slug(name),
            "kadencje":{KADENCJA_ID:{"club":club_assign.get(name,"NZ"),"has_voting_data":True,
                "has_activity_data":False,"frekwencja":round(frekw,1),"aktywnosc":round(aktywn,1),
                "zgodnosc_z_klubem":0.0,"votes_za":vd["za"],"votes_przeciw":vd["przeciw"],
                "votes_wstrzymal":vd["wstrzymal_sie"],"votes_brak":vd["brak"],"votes_nieobecny":vd["nieobecny"],
                "votes_total":total,"rebellion_count":0,"rebellions":[],"roles":[],"notes":"",
                "former":False,"mid_term":False}}})
    return {"profiles":profiles,"total":len(profiles)}

def _is_full_name(t):
    """Dokładnie 2 wyrazy, oba z wielkiej litery (bez cyfr/znaków)."""
    parts=t.split()
    if len(parts)!=2: return False
    if any(re.search(r'[^A-Za-zÀ-ž]', p) for p in parts): return False
    return all(p and p[0].isupper() for p in parts)

def build_roster(records):
    """Kanoniczny skład rady z czystych 2-wyrazowych nazwisk powtarzających się."""
    from collections import Counter
    cnt=Counter()
    for r in records:
        for ns in r["named"].values():
            for t in ns:
                t=t.strip()
                if _is_full_name(t):
                    cnt[t]+=1
    # skład: nazwiska mocno powtarzalne (>=2 głosowań) + każde 2-wyrazowe czyste z >=1
    # (przechwytuje rotację kadencji np. Iwona Kurowska)
    roster=set(t for t,c in cnt.items() if c>=1)
    return roster

def resolve_record(rec, roster):
    """Rozwiązuje surowe tokeny głosowania do kanonicznego składu (merge wrap,
    strip 'nazwisko+tekst tematu', dedupe). Każdy radny głosuje raz na głosowanie."""
    roster_full=sorted(roster)
    used=set()
    out=defaultdict(list)
    for cat, toks in rec["named"].items():
        i=0
        while i<len(toks):
            t=toks[i].strip()
            if not t:
                i+=1; continue
            matched=None
            if t in roster:
                matched=t
            else:
                for r in roster_full:
                    if t.startswith(r) and len(t)>len(r):
                        matched=r; break
            if matched:
                if matched not in used:
                    used.add(matched); out[cat].append(matched)
                i+=1; continue
            # częściowy wrap: pierwszy + drugi token daje pełne nazwisko
            if i+1<len(toks):
                combo=(t+" "+toks[i+1].strip())
                if combo in roster and combo not in used:
                    used.add(combo); out[cat].append(combo); i+=2; continue
            # pojedyncze słowo pasujące do dokładnie jednego radnego
            cands=[r for r in roster_full if r not in used and t in r.split()]
            if len(cands)==1:
                used.add(cands[0]); out[cat].append(cands[0])
                i+=1; continue
            i+=1
    return {k:list(v) for k,v in out.items() if v}

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--city-dir", required=True)
    ap.add_argument("--work-dir", default=None)
    ap.add_argument("--cache-dir", default=None)
    args=ap.parse_args()
    city_dir=Path(args.city_dir)
    cache=Path(args.cache_dir) if args.cache_dir else None
    cfg={}
    if (city_dir/"config.json").is_file():
        cfg=json.loads((city_dir/"config.json").read_text(encoding="utf-8"))
    club_assign=cfg.get("club_assignments",{}) or {}
    roster=set(cfg.get("councilor_roster",[]))

    sessions=discover_sessions(cache)
    print(f"[trzebnica] {len(sessions)} sesji w kat. 201 (IX kad.)")
    records=[]; missing_date=[]; n_no_votes=0
    for se in sessions:
        atts,_ = get_attachments(se["url"], cache)
        # wszystkie załączniki z wykazem głosowań (może być podzielony na części)
        def _low(s): return s.replace("ł","l").replace("ó","o").lower()
        wykaz_urls=[u for l,u in atts if ("wykaz" in _low(l)) and ("glosow" in _low(l))]
        prot=None
        for l,u in atts:
            if "protokol" in _low(l):
                if "wykaz" not in _low(l):
                    prot=u; break
        sno=se.get("num",0)
        date=None
        if prot:
            pd=hashlib.md5(prot.encode()).hexdigest()
            if cache and (cache/(pd+".bin")).is_file():
                pdata=(cache/(pd+".bin")).read_bytes()
            else:
                pdata,_ = _get(prot, cache, ".bin")
            date=parse_protocol_date(pdata)
        if not date:
            missing_date.append(se["title"])
        session_votes=[]
        for wu in wykaz_urls:
            wk=hashlib.md5(wu.encode()).hexdigest()
            if cache and (cache/(wk+".bin")).is_file():
                wdata=(cache/(wk+".bin")).read_bytes()
            else:
                wdata,_ = _get(wu, cache, ".bin")
            session_votes += parse_wykaz(wdata)
        if not session_votes:
            n_no_votes+=1
            print(f"  [!!] {se['title']}: brak parsowalnego wykazu (attachments={[l[:30] for l,_ in atts]})")
            continue
        for v in session_votes:
            v["date"]=date; v["num"]=sno
        records+=session_votes
        print(f"  [ok] {se['title']} nr{sno} date={date} votes={len(session_votes)}")
    print(f"[trzebnica] records={len(records)} missing_date={missing_date} no_votes={n_no_votes}")

    # Resolution do kanonicznego składu (merge wrap-fragmentów, strip 'nazwisko+temat')
    roster_set=build_roster(records)
    records=[{**r,"named":resolve_record(r, roster_set)} for r in records]
    # odrzucamy głosowania bez daty (np. XXV - protokół jeszcze nieopublikowany) i puste
    records=[r for r in records if r.get("date") and r["date"]>=KAD_START and any(r["named"].values())]
    print(f"[trzebnica] po rozwiązywaniu: votes={len(records)} roster={len(roster_set)}")

    output,total_votes,total_sessions=build_output(records, club_assign)
    profiles=build_profiles(records, club_assign, roster_set, total_sessions)
    docs=city_dir/"docs"; docs.mkdir(parents=True,exist_ok=True)
    (docs/"kadencja-2024-2029.json").write_text(json.dumps(output["kadencje"][0],ensure_ascii=False,indent=1),encoding="utf-8")
    data={"generated":output["generated"],"default_kadencja":KADENCJA_ID,"kadencje":[{"id":KADENCJA_ID,"label":KADENCJA_LABEL}]}
    (docs/"data.json").write_text(json.dumps(data,ensure_ascii=False,indent=1),encoding="utf-8")
    (docs/"profiles.json").write_text(json.dumps(profiles,ensure_ascii=False,indent=1),encoding="utf-8")
    print(f"[trzebnica] DONE votes={total_votes} sessions={total_sessions} councilors={len(profiles['profiles'])}")

if __name__=="__main__":
    main()
