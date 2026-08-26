#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Radoskop Skierniewice — imienne głosowania Rady Miasta.

Źródło: BIP Urzędu Miasta Skierniewice (bip.um.skierniewice.pl), kategoria
"Imienne wykazy głosowań radnych Rady Miasta Skierniewice". Rada Miasta
(IX kadencja 2024-2029, 21 radnych) publikuje jako "Imienny wykaz głosowania
radnych ... na {ROMAN} sesji ... w dniu {DD miesiąc RRRR}" per-sesyjne PDF-y
z wynikami głosowań imiennych (za / przeciw / wstrzymuje per radny, temat).

DWA formaty PDF (wydruki z systemu eSesja):
  * Format A (dominujący): jedna strona = jedno głosowanie; nagłówek
    "{ROMAN} sesja Rady Miasta Skierniewice / Urząd Miasta Skierniewice",
    temat, "Czas głosowania", podsumowanie "WYNIKI GŁOSOWANIA Za: N | ..."
    i tabela "Lp. | Imię i nazwisko | głos" (głos = za|przeciw|wstrzymuje).
  * Format B (sesja V, 31.10.2024): głosowania grupują nazwiska pod
    nagłówkami "ZA:", "PRZECIW:", "WSTRZYMALI SIĘ:" (lista przecinkowa,
    przełamuje linię w połowie nazwiska; docinanie do liczby z podsumowania).

Użycie:
    python scrape_skierniewice.py --output docs/data.json --profiles docs/profiles.json [--cache-dir .cache]
"""

import argparse, hashlib, io, json, re, sys, time, unicodedata
from collections import Counter, defaultdict
from datetime import datetime
from itertools import combinations
from pathlib import Path

import pdfplumber
import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BIP = "https://www.bip.um.skierniewice.pl"
CATEGORY = "/kategorie/imienne_wykazy_glosowan_radnych_rady_miasta"
KAD_START = "2024-05-07"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"
CAT_PAGES = 8
REQ_DELAY = 0.6
_LAST_REQ = 0.0

# Kanoniczna lista radnych (z danych głosowań IX kadencji — realne źródło).
COUNCILORS = [
    'Agata Paprocka','Agnieszka Kuchta','Artur Sułek','Artur Zakrzewski',
    'Dariusz Chęcielewski','Dorota Rutkowska','Eliza Polakowska-Binder','Jacek Gędek',
    'Janusz Marek Pastusiak','Jarosław Borowski','Jarosław Chęcielewski','Jerzy Gołębiewski',
    'Krystyna Cieślak','Liwia Małczak','Maciej Wieprzkowicz','Małgorzata Serkowska',
    'Małgorzata Stefanowska','Piotr Paradowski','Piotr Łyżeń','Rafał Koczywąs',
    'Zbigniew Wyszogrodzki',
]
_COUNC = set(COUNCILORS)

_MONTHS_PL = {"stycznia":1,"lutego":2,"marca":3,"kwietnia":4,"maja":5,"czerwca":6,
              "lipca":7,"sierpnia":8,"września":9,"października":10,"listopada":11,"grudnia":12}


def _rate():
    global _LAST_REQ
    now = time.time()
    d = now - _LAST_REQ
    if d < REQ_DELAY:
        time.sleep(REQ_DELAY - d)
    _LAST_REQ = time.time()


def fetch(url, cache_dir=None, binary=False):
    if cache_dir is not None:
        key = hashlib.md5(url.encode()).hexdigest()
        ext = ".bin" if binary else ".html"
        cf = cache_dir / (key + ext)
        if cf.is_file():
            return cf.read_bytes() if binary else cf.read_text(encoding="utf-8", errors="ignore")
    _rate()
    resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Radoskop/1.0"},
                        timeout=90, verify=False)
    resp.raise_for_status()
    data = resp.content if binary else resp.text
    if cache_dir is not None:
        cf = cache_dir / (key + ext)
        cf.parent.mkdir(parents=True, exist_ok=True)
        if binary:
            cf.write_bytes(data)
        else:
            cf.write_text(data, encoding="utf-8", errors="ignore")
    return data


def make_slug(name):
    repl = {'ą':'a','ć':'c','ę':'e','ł':'l','ń':'n','ó':'o','ś':'s','ź':'z','ż':'z',
            'Ą':'A','Ć':'C','Ę':'E','Ł':'L','Ń':'N','Ó':'O','Ś':'S','Ź':'Z','Ż':'Z'}
    slug = name.lower()
    for pl, a in repl.items():
        slug = slug.replace(pl, a)
    return re.sub(r"[^a-z0-9]+", "", slug)


_ROMAN = {'I':1,'V':5,'X':10,'L':50,'C':100,'D':500,'M':1000}
def _roman_to_int(roman):
    n = 0; prev = 0
    for ch in reversed(roman.upper()):
        v = _ROMAN.get(ch, 0)
        n += -v if v < prev else v
        prev = v
    return n


_SESSION_RE = re.compile(
    r'na\s+([IVXLCDM]{1,7})\s+sesj\w*\s*(?:sesji\s+)?w\s+dniu\s+(\d{1,2})\s+([a-ząćęłńóśźż]+)\s+(\d{4})',
    re.IGNORECASE)


def _parse_session_title(title):
    m = _SESSION_RE.search(title)
    if not m:
        return None
    roman, dd, mon, yyyy = m.group(1), int(m.group(2)), m.group(3).lower(), m.group(4)
    mm = _MONTHS_PL.get(mon)
    if mm:
        return {"roman": roman, "date": f"{yyyy}-{mm:02d}-{dd:02d}"}
    return None


# ---------------------------------------------------------------------------
# 1. Kolekcja sesji IX kadencji z kategorii
# ---------------------------------------------------------------------------
def collect_sessions(cache_dir=None):
    out, seen = [], set()
    for page in range(CAT_PAGES):
        url = BIP + CATEGORY + (f"/{page}" if page else "")
        try:
            html = fetch(url, cache_dir)
        except Exception as e:
            print(f"    [warn] kategoria page {page}: {e}")
            break
        for m in re.finditer(r'href="(/artykuly/(\d+))"[^>]*>(.*?)</a>', html, re.S):
            artid = m.group(2)
            if artid in seen:
                continue
            anchor = re.sub(r"\s+", " ", re.sub("<[^>]+>", " ", m.group(3))).strip()
            info = _parse_session_title(anchor)
            if not info or info["date"] < KAD_START:
                continue
            seen.add(artid)
            out.append({"artid": artid, "roman": info["roman"], "date": info["date"],
                        "num": _roman_to_int(info["roman"]), "url": BIP + m.group(1)})
        print(f"    page {page}: sessions IX = {len(out)}")
    out.sort(key=lambda s: (s["date"], s["num"]))
    return out


def _pdf_link(art_url, cache_dir=None):
    html = fetch(art_url, cache_dir)
    m = re.search(r'href="([^"]*wykaz[^"]*\.pdf)"', html, re.I)
    if m:
        return BIP + m.group(1) if m.group(1).startswith("/") else m.group(1)
    return None


# ---------------------------------------------------------------------------
# 2. Parsowanie imiennych głosowań
# ---------------------------------------------------------------------------
_AGGR = re.compile(r'Za:\s*(\d+)\s*\|\s*Przeciw:\s*(\d+)\s*\|\s*Wstrzymali (?:się|sie):\s*(\d+)\s*\|\s*Uprawnieni:\s*(\d+)')
_CZAS = re.compile(r'Czas głosowania:\s*([\d\- :]+)')
_ROW_A = re.compile(r'^(\d+)\s+(.+?)\s+(za|przeciw|wstrzymuje|wstrzymał\s+się|wstrzymal\s+sie|wstrzymała\s+się|nieobecny|nieobecna|brak)$')


def _norm_name(s):
    s = s.strip().strip(',').strip()
    s = re.sub(r'\s+', ' ', s)
    s = re.sub(r'-\s+', '-', s)   # "Polakowska- Binder" -> "Polakowska-Binder"
    return s


def _page_votes_format_a(pages_text, session_date, session_num):
    """Każda strona = jeden głos. Zwraca listę rekordów z named_votes."""
    out = []
    for page in pages_text:
        t = page
        if 'WYNIKI' not in t:
            continue
        lines = [l.strip() for l in t.split('\n')]
        aggr = _AGGR.search(t)
        if not aggr:
            continue
        za, pc, ws, up = map(int, aggr.groups())
        tm = None
        cz_i = None
        for i, l in enumerate(lines):
            m = _CZAS.search(l)
            if m:
                tm = m.group(1).strip()
                cz_i = i
                break
        # temat = linie od nagłówka do linii "Czas głosowania" (która w formacie A
        # występuje PRZED "WYNIKI GŁOSOWANIA"), minus nagłówek sesji/miasta.
        topic_lines = lines[:cz_i] if cz_i is not None else []
        while topic_lines and ('sesja' in topic_lines[0].lower() and 'Rady Miasta' in topic_lines[0]):
            topic_lines.pop(0)
        while topic_lines and topic_lines[0].startswith('Urząd Miasta'):
            topic_lines.pop(0)
        topic = ' '.join(x for x in topic_lines if x).strip()
        # tabela głosów
        named = {"za": [], "przeciw": [], "wstrzymal_sie": []}
        in_rows = False
        for l in lines:
            if re.match(r'^Lp\.', l):
                in_rows = True
                continue
            if not in_rows:
                continue
            m = _ROW_A.match(l)
            if m:
                nm = _norm_name(m.group(2))
                v = _clean_vote(m.group(3))
                if v and nm in _COUNC:
                    named[v].append(nm)
        # walidacja
        if len(named['za']) == za and len(named['przeciw']) == pc and len(named['wstrzymal_sie']) == ws:
            out.append({"session_date": session_date, "session_num": session_num,
                        "topic": topic, "time": tm, "named": named})
        else:
            print(f"    [warn]{session_num} {session_date} formatA page: sums "
                  f"za{len(named['za'])}/{za} pc{len(named['przeciw'])}/{pc} ws{len(named['wstrzymal_sie'])}/{ws} — pominięto")
    return out


def _clean_vote(v):
    v = v.strip().lower().replace('ł', 'l').replace('ę', 'e')
    if v == 'za':
        return 'za'
    if 'przeciw' in v:
        return 'przeciw'
    if 'wstrzym' in v:
        return 'wstrzymal_sie'
    return None


def _section_names(lines, start_idx, target):
    """Łączy linie sekcji ze złamaniami (nazwisko może się złamać na końcu linii
    BEZ przecinka) i wycina do `target` unikalnych nazwisk radnych z listy.
    Zwraca (names, first_idx_po_nazwiskach)."""
    names, seen = [], set()
    running = ""
    i = start_idx
    nl = len(lines)
    while i < nl and len(names) < target:
        ls = lines[i].strip()
        if ls:
            running = (running + " " + ls) if running else ls
            parts = running.split(',')
            if len(parts) > 1:
                for p in parts[:-1]:
                    nm = _norm_name(p)
                    if nm in _COUNC and nm not in seen:
                        names.append(nm); seen.add(nm)
                running = parts[-1]
        i += 1
    if running:
        nm = _norm_name(running)
        if nm in _COUNC and nm not in seen:
            names.append(nm)
    return names, i


def _b_chunk(chunk):
    """Parsuje jeden blok głosowania (format B). Zwraca (named, trailing, aggr, czas)."""
    lines = chunk.split('\n')
    aggr = _AGGR.search(lines[0])
    if not aggr:
        return None, '', None, None
    za, pc, ws, up = map(int, aggr.groups())
    tm = None
    for l in lines:
        m = _CZAS.search(l)
        if m:
            tm = m.group(1).strip()
            break
    za_i = pc_i = ws_i = None
    for idx, l in enumerate(lines):
        if re.match(r'^ZA:$', l.strip()): za_i = idx
        elif re.match(r'^PRZECIW:$', l.strip()): pc_i = idx
        elif re.match(r'^WSTRZYMALI\s*(SIĘ|SIE):$', l.strip()): ws_i = idx
    za_names, _ = _section_names(lines, (za_i + 1) if za_i is not None else 0, za)
    pc_names, _ = _section_names(lines, (pc_i + 1) if pc_i is not None else 0, pc)
    if ws_i is not None and ws > 0:
        ws_names, trail = _section_names(lines, ws_i + 1, ws)
    elif ws_i is not None:
        ws_names, trail = [], ws_i + 1
    else:
        ws_names, trail = [], 0
    trailing = '\n'.join(lines[trail:]).strip() if trail < len(lines) else ''
    named = {"za": za_names, "przeciw": pc_names, "wstrzymal_sie": ws_names}
    return named, trailing, (za, pc, ws, up), tm


def _parse_grouped_doc(full, session_date, session_num):
    """Format B: dokumentowo, grupy ZA:/PRZECIW:/WSTRZYMALI SIĘ:."""
    parts = re.split(r'(?m)^WYNIKI\s*GŁOSOWANIA', full)
    votes = []
    prev_topic = ""
    for pi in range(1, len(parts)):
        named, trailing, aggr, tm = _b_chunk(parts[pi])
        if not aggr:
            continue
        za, pc, ws, up = aggr
        if len(named['za']) == za and len(named['przeciw']) == pc and len(named['wstrzymal_sie']) == ws:
            votes.append({"session_date": session_date, "session_num": session_num,
                          "topic": prev_topic, "time": tm, "named": named})
        else:
            print(f"    [warn]{session_num} {session_date} grouped: sums "
                  f"za{len(named['za'])}/{za} pc{len(named['przeciw'])}/{pc} "
                  f"ws{len(named['wstrzymal_sie'])}/{ws} — pominięto")
        prev_topic = trailing
    # pierwsze głosowanie: temat z preambuły (część parts[0] po nagłówku sesji/miasta)
    if votes:
        pre = '\n'.join(parts[0].split('\n')[2:]).strip()
        votes[0]["topic"] = pre
    return votes


def parse_pdf(data, session_date, session_num):
    pages = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        pages = [(p.extract_text() or "") for p in pdf.pages]
    joined = '\n'.join(pages)
    if re.search(r'(?m)^ZA:\s*$', joined) or '\nZA:\n' in joined:
        return _parse_grouped_doc(joined, session_date, session_num)
    return _page_votes_format_a(pages, session_date, session_num)


# ---------------------------------------------------------------------------
# 3. Budowa danych Radoskop (wzorzec jak siemianowice/konin)
# ---------------------------------------------------------------------------
CLUB = {n: "" for n in COUNCILORS}


def _club_of(name):
    return CLUB.get(name, "")


def _compute_consensus(all_votes):
    stats = defaultdict(lambda: {"za":0,"przeciw":0,"wstrzymal":0,"brak":0,"nieobecny":0,
                                 "with":0,"against":0,"sess":set()})
    for v in all_votes:
        for cat, names in v["named_votes"].items():
            for name in names:
                key = "za" if cat=="za" else "przeciw" if cat=="przeciw" else "wstrzymal"
                stats[name][key] += 1
                stats[name]["sess"].add(v["session_date"])
    return stats


def build_output(records):
    all_votes = []
    vid = 0
    sessions_by_date = {}
    for rec in records:
        d = rec.get("session_date")
        if not d:
            continue
        if d not in sessions_by_date:
            sessions_by_date[d] = {"date": d, "number": rec.get("session_num",""),
                                   "vote_count": 0, "attendees": set()}
        named = {k: list(v) for k, v in rec["named"].items()}
        vid += 1
        sessions_by_date[d]["vote_count"] += 1
        for cat in ("za","przeciw","wstrzymal_sie"):
            sessions_by_date[d]["attendees"].update(named.get(cat, []))
        all_votes.append({"id": str(vid), "session_date": d,
                          "session_number": rec.get("session_num",""),
                          "topic": rec.get("topic","") or "",
                          "named_votes": named,
                          "counts": {k: len(named.get(k,[])) for k in ("za","przeciw","wstrzymal_sie")}})
    sessions_data = []
    for d in sorted(sessions_by_date.keys()):
        s = sessions_by_date[d]
        sessions_data.append({"date": d, "number": s["number"], "vote_count": s["vote_count"],
                              "attendee_count": len(s["attendees"]), "attendees": sorted(s["attendees"]),
                              "speakers": []})
    all_names = set()
    for v in all_votes:
        for names in v["named_votes"].values():
            all_names.update(names)
    councilors_data = {}
    for name in sorted(all_names):
        councilors_data[name] = {"name": name, "club": _club_of(name), "district": None,
            "votes_za":0,"votes_przeciw":0,"votes_wstrzymal":0,"votes_brak":0,"votes_nieobecny":0,
            "votes_with_club":0,"votes_against_club":0,"rebellions":[]}
    for v in all_votes:
        for cat, names in v["named_votes"].items():
            for name in names:
                c = councilors_data.get(name)
                if not c: continue
                if cat=="za": c["votes_za"]+=1
                elif cat=="przeciw": c["votes_przeciw"]+=1
                else: c["votes_wstrzymal"]+=1
    total_votes = len(all_votes)
    total_sessions = len(sessions_data)
    stats = _compute_consensus(all_votes)
    councilors_list = []
    for name in sorted(councilors_data.keys()):
        c = councilors_data[name]
        st = stats[name]
        present = c["votes_za"]+c["votes_przeciw"]+c["votes_wstrzymal"]+c["votes_brak"]
        aktywnosc = (present/total_votes*100) if total_votes else 0
        frekwencja = (len(st["sess"])/total_sessions*100) if total_sessions else 0
        dec = st["with"]+st["against"]
        zgodnosc = (st["with"]/dec*100) if dec else 0.0
        councilors_list.append({"name": name, "club": c["club"], "district": None,
            "frekwencja": round(frekwencja,1), "aktywnosc": round(aktywnosc,1),
            "zgodnosc_z_klubem": round(zgodnosc,1),
            "votes_za": c["votes_za"], "votes_przeciw": c["votes_przeciw"],
            "votes_wstrzymal": c["votes_wstrzymal"], "votes_brak": c["votes_brak"],
            "votes_nieobecny": c["votes_nieobecny"], "votes_total": total_votes,
            "rebellion_count": 0, "rebellions": [], "has_activity_data": False, "activity": None})
    global NAME_AGG
    global _all_session_dates
    NAME_AGG = {name: dict(stats[name], sess=len(stats[name]["sess"])) for name in stats}
    _all_session_dates = [s["date"] for s in sessions_data]
    # similarity
    vectors = defaultdict(dict)
    for v in all_votes:
        for cat in ("za","przeciw","wstrzymal_sie"):
            for name in v["named_votes"].get(cat, []):
                vectors[name][v["id"]] = cat
    pairs = []
    names_sorted = sorted(vectors.keys())
    for a, b in combinations(names_sorted, 2):
        common = set(vectors[a].keys()) & set(vectors[b].keys())
        if len(common) < 10:
            continue
        same = sum(1 for vid in common if vectors[a][vid] == vectors[b][vid])
        pairs.append({"a": a, "b": b, "club_a": _club_of(a), "club_b": _club_of(b),
                      "score": round(same/len(common)*100,1), "common_votes": len(common)})
    pairs.sort(key=lambda x: x["score"], reverse=True)
    kad = {"id": KADENCJA_ID, "label": KADENCJA_LABEL, "clubs": {},
           "sessions": sessions_data, "total_sessions": total_sessions,
           "total_votes": total_votes, "total_councilors": len(councilors_list),
           "councilors": councilors_list, "votes": all_votes,
           "similarity_top": pairs[:20], "similarity_bottom": pairs[-20:][::-1]}
    return {"generated": datetime.now().isoformat(), "default_kadencja": KADENCJA_ID,
            "kadencje": [kad]}


NAME_AGG = {}
_all_session_dates = []


def build_profiles(records):
    cv = defaultdict(lambda: {"za":0,"przeciw":0,"wstrzymal_sie":0,"nieobecny":0,"brak":0,"sess":set()})
    for rec in records:
        d = rec.get("session_date")
        if not d: continue
        for cat, names in rec["named"].items():
            for name in names:
                key = "za" if cat=="za" else "przeciw" if cat=="przeciw" else "wstrzymal_sie"
                cv[name][key] += 1
                cv[name]["sess"].add(d)
    profiles = []
    for name in sorted(cv.keys()):
        vd = cv[name]
        total = sum(vd[k] for k in ("za","przeciw","wstrzymal_sie","nieobecny","brak")) or 1
        agg = NAME_AGG.get(name, {})
        frekw = 100.0*len(vd["sess"])/len(_all_session_dates) if _all_session_dates else 0.0
        dec = agg.get("with",0)+agg.get("against",0)
        zgod = 100.0*agg.get("with",0)/dec if dec else 0.0
        profiles.append({"name": name, "slug": make_slug(name),
            "kadencje": {KADENCJA_ID: {
                "club": _club_of(name), "has_voting_data": True, "has_activity_data": False,
                "frekwencja": round(frekw,1),
                "aktywnosc": round(float(vd["za"]+vd["przeciw"]+vd["wstrzymal_sie"])/total*100,1),
                "zgodnosc_z_klubem": round(zgod,1),
                "votes_za": vd["za"], "votes_przeciw": vd["przeciw"],
                "votes_wstrzymal": vd["wstrzymal_sie"], "votes_brak": vd["brak"],
                "votes_nieobecny": vd["nieobecny"], "votes_total": total,
                "rebellion_count": 0, "rebellions": [], "roles": [], "notes": "",
                "former": False, "mid_term": False}}}),
    return {"profiles": profiles}


def save_split(output, out_path, profiles):
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    stubs = []
    for kad in output.get("kadencje", []):
        kid = kad["id"]
        stubs.append({"id": kid, "label": kad.get("label", f"Kadencja {kid}")})
        with open(out_path.parent / f"kadencja-{kid}.json", "w", encoding="utf-8") as f:
            json.dump(kad, f, ensure_ascii=False, separators=(",", ":"))
    index = {"generated": output.get("generated",""), "default_kadencja": output.get("default_kadencja",""),
             "kadencje": stubs}
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, separators=(",", ":"))
    with open(out_path.parent / "profiles.json", "w", encoding="utf-8") as f:
        json.dump(profiles, f, ensure_ascii=False, separators=(",", ":"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", required=True)
    ap.add_argument("--profiles", required=True)
    ap.add_argument("--cache-dir", default=None)
    args = ap.parse_args()
    cache_dir = Path(args.cache_dir) if args.cache_dir else None
    print("=== Scraper Rada Miasta Skierniewice (bip.um.skierniewice.pl) ===")
    sessions = collect_sessions(cache_dir)
    print(f"  Sesji IX kadencji: {len(sessions)}")
    if not sessions:
        print("  BRAK SESJI — koniec."); sys.exit(1)
    records = []
    for s in sessions:
        link = _pdf_link(s["url"], cache_dir)
        if not link:
            print(f"  [warn] {s['roman']} {s['date']}: brak linku PDF")
            continue
        try:
            data = fetch(link, cache_dir, binary=True)
        except Exception as e:
            print(f"  [warn] {s['roman']}: pdf fetch {e}")
            continue
        vs = parse_pdf(data, s["date"], s["roman"])
        if not vs:
            print(f"  [warn] {s['roman']:7s} {s['date']}: 0 głosowań")
        else:
            print(f"  {s['roman']:7s} {s['date']}: {len(vs)} głosowań")
        records.extend(vs)
    print(f"  Razem głosowań (zwalidowanych): {len(records)}")
    if not records:
        print("  BRAK DANYCH"); sys.exit(1)
    output = build_output(records)
    profiles = build_profiles(records)
    save_split(output, args.output, profiles)
    t = output["kadencje"][0]
    print(f"  Zapisano: {args.output}, kadencja-{KADENCJA_ID}.json, profiles.json")
    print(f"  Sesji: {t['total_sessions']}, głosowań: {t['total_votes']}, radnych: {t['total_councilors']}")


if __name__ == "__main__":
    main()
