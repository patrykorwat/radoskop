#!/usr/bin/env python3
"""Radoskop Olesno — custom scraper (BIP olesno, text imienne in article HTML).

Źródło: https://bip.olesno.pl (custom gov.pl-style BIP). Rada Miejska w Oleśnie
publikuje per-sesja "Wykaz głosowań sesji - <roman> sesja Rady Miejskiej w Oleśnie"
as HTML articles under category "Imienny wykaz głosowań" (id=8319). Each article
contains full per-councilor imienne votes in TEXT:

  Głosowanie nad uchwałą w sprawie ... - 14:42:00
  Wyniki imienne:
    ZA (13): <names>, PRZECIW (0): , WSTRZYMUJE SIĘ (1): ..., NIE GŁOSOWALI (0): , NIEOBECNI (3): ...

Enumerator: XML feed /xml/8319/imienny-wykaz-glosowan.html lists all session-vote
articles' URLs. IX kadencja = sessions with date >= 2024-05-07.

Output: docs/kadencja-2024-2029.json + docs/data.json + docs/profiles.json
(format identical to parczew/blonie/police). Text-based, no OCR.
"""
import argparse, json, re, hashlib
from collections import Counter, defaultdict
from datetime import datetime
from itertools import combinations
from pathlib import Path
import requests

BASE = "https://bip.olesno.pl"
FEED = f"{BASE}/xml/8319/imienny-wykaz-glosowan.html"
PROTO_FEED = f"{BASE}/xml/11634/protokoly-z-sesji-rady-miejskiej-w-olesnie-kadencja-2024-2029.html"
KAD_START = "2024-05-07"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"
HDRS = {"User-Agent": "Mozilla/5.0 (Radoskop cron)", "Accept-Language": "pl,en"}

_MONTHS = {"stycznia":1,"lutego":2,"marca":3,"kwietnia":4,"maja":5,"czerwca":6,
           "lipca":7,"sierpnia":8,"września":9,"października":10,"pazdziernika":10,
           "listopada":11,"grudnia":12}

# Session roman numerals -> int (for mapping session number to date).
_ROMAN = {"I":1,"II":2,"III":3,"IV":4,"V":5,"VI":6,"VII":7,"VIII":8,"IX":9,"X":10,
          "XI":11,"XII":12,"XIII":13,"XIV":14,"XV":15,"XVI":16,"XVII":17,"XVIII":18,"XIX":19,
          "XX":20,"XXI":21,"XXII":22,"XXIII":23,"XXIV":24,"XXV":25,"XXVI":26,"XXVII":27,
          "XXVIII":28,"XXIX":29,"XXX":30,"XXXI":31,"XXXII":32,"XXXIII":33,"XXXIV":34,
          "XXXV":35,"XXXVI":36,"XXXVII":37,"XXXVIII":38,"XXXIX":39,"XL":40}

# Vote category labels found in Olesno articles (counts in parens before names).
_VOTE_HEAD = {
    "ZA": "za", "PRZECIW": "przeciw", "WSTRZYMUJE SIĘ": "wstrzymal_sie",
    "WSTRZYMUJĄ SIĘ": "wstrzymal_sie", "NIE GŁOSOWALI": "nie_glosowal",
    "NIEOBECNI": "nieobecny", "BRAK GŁOSU": "brak", "WSTRZYMAŁ SIĘ": "wstrzymal_sie",
}


def _get(url, cache_dir=None, binary=False):
    if cache_dir:
        h = hashlib.md5(url.encode()).hexdigest()[:16]
        p = Path(cache_dir) / (h + ".bin")
        if p.is_file():
            return p.read_bytes() if binary else p.read_text(encoding="utf-8", errors="ignore")
    r = requests.get(url, headers=HDRS, timeout=60)
    r.raise_for_status()
    data = r.content if binary else r.text
    if cache_dir:
        Path(cache_dir).mkdir(parents=True, exist_ok=True)
        p = Path(cache_dir) / (hashlib.md5(url.encode()).hexdigest()[:16] + ".bin")
        p.write_bytes(r.content if binary else data.encode("utf-8"))
    return data


def discover_session_urls(cache_dir=None):
    """All session-vote article URLs from the XML feed."""
    xml = _get(FEED, cache_dir)
    urls = sorted(set(re.findall(r'https?://[^"<\s]*wykaz-glosowan-sesji-[^"<\s]+\.html', xml)))
    return urls


def build_session_date_map(cache_dir=None):
    """Map session roman numeral -> ISO date.

    The imienny-wykaz articles mostly carry no date in the body. The authorative
    session dates come from the 'Protokoły z sesji ... kadencja 2024-2029'
    category (11634), whose articles open 'Protokół z <ROMAN> sesji Rady Miejskiej
    w Oleśnie' + 'Obrady rozpoczęto YYYY-MM-DD'.
    """
    rom_date = {}
    xml = _get(PROTO_FEED, cache_dir)
    for u in re.findall(r'https?://[^"<\s]*protokol-sesji-[^"<\s]+\.html', xml):
        try:
            html = _get(u, cache_dir)
        except Exception:
            continue
        txt = re.sub(r"<[^>]+>", " ", html)
        txt = re.sub(r"\s+", " ", txt)
        # "Protokół z <ROMAN> sesji Rady Miejskiej ..." then "Obrady rozpoczęto YYYY-MM-DD"
        rom = re.search(r"Protokół\s+z\s+([IVXLCDM]+)\s+sesji\s+Rady\s+Miejskiej", txt)
        iso = re.search(r"Obrady\s+rozpoczęto\s+(\d{4}-\d{2}-\d{2})", txt)
        rk = rom.group(1).upper() if rom else None
        if rk and iso and rk in _ROMAN:
            rom_date[rk] = iso.group(1)
    return rom_date


def session_date_from_article(html, url, rom_date=None):
    """Session date: ONLY via the authoritative session-roman map (built from
    protokoły). The body contains uchwała dates (e.g. 'z dnia 31 grudnia 2024')
    that are NOT the session date, so we deliberately do NOT fall back to the
    body — a session whose roman isn't in the IX-kadence map is treated as
    unresolved (old-kadence or undated) and skipped."""
    rom_date = rom_date or {}
    txt0 = re.sub(r"<[^>]+>", " ", html)
    txt0 = re.sub(r"\s+", " ", txt0)
    hm = re.search(r"Wykaz głosowań sesji\s*[-–—]?\s*(\w+)\s+sesj\w*\s+Rady\s+Miejskiej", txt0, re.I)
    if hm:
        cand = hm.group(1).upper()
        if cand in rom_date:
            return rom_date[cand]
    # explicit URL date ('...-w-dniu-12-...-2026-...') — trustworthy, add it
    um = re.search(r'w-dniu-(\d{1,2})-([a-ząćęłńóśźż]+)-(\d{4})', url, re.I)
    if um:
        mom = _MONTHS.get(um.group(2).lower())
        if mom:
            d = f"{um.group(3)}-{mom:02d}-{int(um.group(1)):02d}"
            if d >= KAD_START:
                return d
    return None


def parse_article_votes(html):
    """Parse per-vote imienne blocks from article HTML. Returns list of dicts."""
    txt = re.sub(r"<[^>]+>", " ", html)
    txt = re.sub(r"\s+", " ", txt)
    votes = []
    # Text structure (per vote):
    #   "Głosowanie nad <topic>. - <time> Wyniki imienne: <results> Głosowanie nad <next>..."
    # Split on 'Wyniki imienne:' — parts[0]=preamble, parts[i]=i-th vote results
    # followed by the start of the next vote's "Głosowanie nad".
    parts = re.split(r"Wyniki imienne\s*:", txt)
    for i in range(1, len(parts)):
        prev_seg = parts[i-1]
        topic = ""
        gm = list(re.finditer(r"Głosowanie nad\s+(.*?)(?:\s+-?\s*\d{1,2}:\d{2}:\d{2}|\s*$)", prev_seg))
        if gm:
            topic = gm[-1].group(1).strip()
        # isolate this vote's results: cut at the next vote/topic starter marker
        parts_i = parts[i]
        # cut at the next vote/topic starter marker (any case: 'Głosowanie nad',
        # 'głosowanie i', 'Podjęcie uchwały', 'Wniosek', 'wykreślenie', 'Reasumpcja',
        # 'Imienny wykaz głosowań' search chrome)
        mseg = re.split(
            r"\s+\b(?:[Gg][łl]osowanie|[Gg][łl]osownie|Podjęcie|Wniosek|wykreślenie|Reasumpcja|Imienny wykaz głosowań)\b",
            parts_i)
        seg = mseg[0] if len(mseg) > 1 else parts_i
        # drop trailing search/footer chrome that follows the last real name
        cut = re.search(r"\s(?:Imienny wykaz głosowań|Liczba wyników|Obrady|Metryczka|XML)\s", seg)
        if cut:
            seg = seg[:cut.start()]
        # category headers in fixed order; capture names between consecutive headers
        heads = list(re.finditer(r"\b(ZA|PRZECIW|WSTRZYMUJ[EĄ] SIĘ|NIE GŁOSOWALI|NIEOBECNI|BRAK GŁOSU)\s*\((\d+)\)\s*:", seg))
        named = {"za": [], "przeciw": [], "wstrzymal_sie": [], "nie_glosowal": [], "nieobecny": [], "brak": []}
        counts = {}
        for hi, m in enumerate(heads):
            head, cnt = m.group(1), int(m.group(2))
            key = _VOTE_HEAD[head]
            start = m.end()
            # bound to next header, or to a footer/article marker for the last one
            end = heads[hi+1].start() if hi+1 < len(heads) else len(seg)
            raw = seg[start:end]
            # stop if leftover footer/article chrome leaks in
            cut = re.search(r"\s(XML|Drukuj stron|Metryczka|Załączniki|Powrót do poprzedniej|Informacje dodatkowe)\s", raw)
            if cut:
                raw = raw[:cut.start()]
            names = [n.strip().rstrip(".,:") for n in raw.split(",") if n.strip().rstrip(".,:")]
            # aggregate is authoritative; drop any footer-ish trailing tokens that
            # leak in after the real names (esp. for the last vote of an article)
            names = names[:cnt]
            counts[key] = cnt
            named[key] = names
        votes.append({"topic": topic, "counts": counts, "named": named})
    return votes


def validate_vote(v):
    """Check parsed named count vs aggregate for za/przeciw/wstrzymal.

    PRZECIW/WSTRZYMUJE headers are OMITTED entirely when their count is 0 —
    default missing aggregate to 0. Also guard against the header regex
    over-capturing when a following header is absent.
    """
    for key in ("za", "przeciw", "wstrzymal_sie"):
        c = v["counts"].get(key)
        if c is None:
            c = 0
            v["counts"][key] = 0
        n = len(v["named"].get(key, []))
        if n != c:
            return False, f"{key}: agg={c} parsed={n}"
    return True, "ok"


def make_slug(name):
    repl = {'ą':'a','ć':'c','ę':'e','ł':'l','ń':'n','ó':'o','ś':'s','ź':'z','ż':'z',
            'Ą':'A','Ć':'C','Ę':'E','Ł':'L','Ń':'N','Ó':'O','Ś':'S','Ź':'Z','Ż':'Z'}
    s = name.lower()
    for pl, a in repl.items():
        s = s.replace(pl, a)
    return re.sub(r"[^a-z0-9]+", "", s)


def build_output(records, club_assign=None):
    club_assign = club_assign or {}
    from collections import defaultdict
    all_votes = []; vid = 0; sessions_by_date = {}
    for rec in records:
        d = rec["date"]
        if not d or d < KAD_START:
            continue
        if d not in sessions_by_date:
            sessions_by_date[d] = {"date": d, "number": rec.get("session_num", ""),
                                   "vote_count": 0, "attendees": set(), "speakers": []}
        sessions_by_date[d]["vote_count"] += 1
        named = {k: list(v) for k, v in rec["named"].items()}
        for cat in ("za", "przeciw", "wstrzymal_sie", "nie_glosowal"):
            sessions_by_date[d]["attendees"].update(rec["named"].get(cat, []))
        vid += 1
        all_votes.append({"id": str(vid), "session_date": d, "session_number": rec.get("session_num", ""),
                          "topic": rec.get("topic", ""), "named_votes": named,
                          "counts": {k: len(named.get(k, [])) for k in ("za", "przeciw", "wstrzymal_sie")}})
    sessions_data = []
    for d in sorted(sessions_by_date.keys()):
        s = sessions_by_date[d]
        sessions_data.append({"date": d, "number": s["number"], "vote_count": s["vote_count"],
                              "attendee_count": len(s["attendees"]), "attendees": sorted(s["attendees"]), "speakers": []})
    all_names = set()
    for v in all_votes:
        for names in v["named_votes"].values():
            all_names.update(names)
    councilors_data = {}
    for name in all_names:
        councilors_data[name] = {"name": name, "club": club_assign.get(name, "NZ"), "district": None,
            "votes_za":0,"votes_przeciw":0,"votes_wstrzymal":0,"votes_brak":0,"votes_nieobecny":0,"rebellions":[]}
    for v in all_votes:
        for cat, names in v["named_votes"].items():
            for nm in names:
                if nm not in councilors_data: continue
                if cat=="nieobecny": councilors_data[nm]["votes_nieobecny"]+=1
                elif cat=="brak": councilors_data[nm]["votes_brak"]+=1
                elif cat=="za": councilors_data[nm]["votes_za"]+=1
                elif cat=="przeciw": councilors_data[nm]["votes_przeciw"]+=1
                elif cat=="wstrzymal_sie": councilors_data[nm]["votes_wstrzymal"]+=1
    total_votes = len(all_votes); total_sessions = len(sessions_data)
    councillor_sess = defaultdict(set)
    for v in all_votes:
        for cat, names in v["named_votes"].items():
            for nm in names:
                councillor_sess[nm].add(v["session_date"])
    councilors_list = []
    for c in sorted(councilors_data.values(), key=lambda x: x["name"]):
        present = c["votes_za"]+c["votes_przeciw"]+c["votes_wstrzymal"]+c["votes_brak"]
        aktywn = (present/total_votes*100) if total_votes else 0
        frekw = (len(councillor_sess.get(c["name"], set()))/total_sessions*100) if total_sessions else 0
        councilors_list.append({"name": c["name"], "club": c["club"], "district": None,
            "frekwencja": round(frekw,1), "aktywnosc": round(aktywn,1), "zgodnosc_z_klubem": 0.0,
            "votes_za": c["votes_za"], "votes_przeciw": c["votes_przeciw"], "votes_wstrzymal": c["votes_wstrzymal"],
            "votes_brak": c["votes_brak"], "votes_nieobecny": c["votes_nieobecny"], "votes_total": total_votes,
            "rebellion_count": 0, "rebellions": [], "has_activity_data": False, "activity": None})
    vectors = defaultdict(dict)
    for v in all_votes:
        for cat in ("za","przeciw","wstrzymal_sie"):
            for nm in v["named_votes"].get(cat, []):
                vectors[nm][v["id"]] = cat
    pairs=[]; names_sorted=sorted(vectors.keys())
    for a,b in combinations(names_sorted,2):
        common=set(vectors[a].keys())&set(vectors[b].keys())
        if len(common)<10: continue
        same=sum(1 for vid in common if vectors[a][vid]==vectors[b][vid])
        pairs.append({"a":a,"b":b,"club_a":"","club_b":"","score":round(same/len(common)*100,1),"common_votes":len(common)})
    pairs.sort(key=lambda x:x["score"], reverse=True)
    kad={"id":KADENCJA_ID,"label":KADENCJA_LABEL,
         "clubs":dict(Counter(club_assign.get(c["name"],"NZ") for c in councilors_list)),
         "sessions":sessions_data,"total_sessions":total_sessions,"total_votes":total_votes,
         "total_councilors":len(councilors_list),"councilors":councilors_list,"votes":all_votes,
         "similarity_top":pairs[:20],"similarity_bottom":pairs[-20:][::-1]}
    return {"generated":datetime.now().isoformat(),"default_kadencja":KADENCJA_ID,"kadencje":[kad]}, total_votes, total_sessions


def build_profiles(records, club_assign=None):
    club_assign = club_assign or {}
    cv = defaultdict(lambda: {"za":0,"przeciw":0,"wstrzymal_sie":0,"brak":0,"nieobecni":0,"votes":[]})
    for rec in records:
        d = rec["date"]
        if not d or d < KAD_START: continue
        for cat,names in rec["named"].items():
            for nm in names:
                if cat in cv[nm]: cv[nm][cat]+=1
                cv[nm]["votes"].append({"session":d,"vote":cat})
    profiles=[]
    sess_set={r["date"] for r in records if r["date"]>=KAD_START}
    n_sessions=len(sess_set) or 1
    for nm in sorted(cv.keys()):
        vd=cv[nm]
        total=sum(vd[k] for k in ("za","przeciw","wstrzymal_sie","brak")) or 1
        sess=len({v["session"] for v in vd["votes"]})
        aktywn=(vd["za"]+vd["przeciw"]+vd["wstrzymal_sie"])/n_sessions*100
        profiles.append({"name":nm,"slug":make_slug(nm),
            "kadencje":{KADENCJA_ID:{"club":club_assign.get(nm,"NZ"),"has_voting_data":True,"has_activity_data":False,
                "frekwencja":round(sess/n_sessions*100,1),"aktywnosc":round(aktywn,1),"zgodnosc_z_klubem":0.0,
                "votes_za":vd["za"],"votes_przeciw":vd["przeciw"],"votes_wstrzymal":vd["wstrzymal_sie"],
                "votes_brak":vd["brak"],"votes_nieobecny":0,"votes_total":total,"rebellion_count":0,
                "rebellions":[],"roles":[],"notes":"","former":False,"mid_term":False}}})
    return {"profiles":profiles,"total":len(profiles)}


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--city-dir", required=True)
    ap.add_argument("--work-dir", default=None)
    ap.add_argument("--cache-dir", default=None)
    args=ap.parse_args()
    city_dir=Path(args.city_dir)
    cache=Path(args.cache_dir) if args.cache_dir else (Path(args.work_dir) if args.work_dir else city_dir/"work")
    cache.mkdir(parents=True, exist_ok=True)
    cfg={}
    if (city_dir/"config.json").is_file():
        cfg=json.loads((city_dir/"config.json").read_text(encoding="utf-8"))
    club_assign=dict(cfg.get("club_assignments",{}) or {})

    urls=discover_session_urls(cache)
    print(f"[olesno] {len(urls)} session-vote articles in feed")
    rom_date=build_session_date_map(cache)
    print(f"[olesno] session-date map: {len(rom_date)} sessions")
    records=[]
    ok_sessions=0; ok_votes=0
    for url in urls:
        html=_get(url.replace("http://","https://"), cache)
        sdate=session_date_from_article(html, url, rom_date)
        if not sdate or sdate < KAD_START:
            continue
        votes=parse_article_votes(html)
        valid=[]
        for v in votes:
            ok,msg=validate_vote(v)
            if ok:
                v["date"]=sdate; v["session_num"]=""; valid.append(v)
            else:
                print(f"    [VAL-FAIL {sdate}] {msg}")
        if valid:
            records+=valid; ok_sessions+=1; ok_votes+=len(valid)
            print(f"  [ok] {sdate} votes={len(valid)}")
    output,total_votes,total_sessions=build_output(records, club_assign)
    profiles=build_profiles(records, club_assign)
    docs_dir=city_dir/"docs"; docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir/"kadencja-2024-2029.json").write_text(json.dumps(output["kadencje"][0],ensure_ascii=False,indent=1),encoding="utf-8")
    data={"generated":output["generated"],"default_kadencja":KADENCJA_ID,"kadencje":[{"id":KADENCJA_ID,"label":KADENCJA_LABEL}]}
    (docs_dir/"data.json").write_text(json.dumps(data,ensure_ascii=False,indent=1),encoding="utf-8")
    (docs_dir/"profiles.json").write_text(json.dumps(profiles,ensure_ascii=False,indent=1),encoding="utf-8")
    print(f"[olesno] DONE sessions={total_sessions} votes={total_votes} councilors={len(profiles['profiles'])} (from {ok_sessions} report articles)")


if __name__=="__main__":
    main()
