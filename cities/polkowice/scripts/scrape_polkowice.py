#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Radoskop Polkowice — imienne głosowania Rady Miejskiej w Polkowicach (IX kadencja 2024-2029).

Źródło: BIP bip.polkowice.eu — React SPA "layout-default" z API pod /api/.
  * menu drzewa:  /api/menu/{menuId}            (Rada Miejska -> Posiedzenia -> Protokoły z sesji = 1688)
  * lista artykułów: /api/menu/1688/articles?offset=&limit=   (30 protokołów sesji IX kad., I..XXX)
  * szczegóły + załączniki: /api/articles/{articleId}   -> attachments[] "Głosowania z <N> sesji..." (PDF)
  * pobranie PDF: /e,pobierz,get.html?id=<attachmentId>
  Jeden PDF = JEDNA sesja, jeden głos na stronę, wydruk eSesja:
     liczba uprawnionych/za/przeciw/wstrzymujące się + tabela dwukolumnowa
     "Lp | Nazwisko i imię | Głos (ZA/PRZECIW/WSTRZYMUJE SIĘ/OBECNY/NIEOBECNY)".

Walidacja: KAŻDE głosowanie reconcilowane vs agregaty (Głosy za/przeciw/wstrzymujące ==
suma list imiennych; nieobecni == Liczba nieobecnych).

Użycie:
    python scrape_polkowice.py --city-dir cities/polkowice
"""
import argparse
import hashlib
import io
import json
import re
import time
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import pdfplumber
import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE = "https://bip.polkowice.eu"
PROTOKOLY_MENU = 1688
KAD_START = "2024-05-07"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/131", "Accept": "application/json"}

REQ_DELAY = 0.4
_LAST = 0.0

_ROM = {"I":1,"II":2,"III":3,"IV":4,"V":5,"VI":6,"VII":7,"VIII":8,"IX":9,"X":10,
        "XI":11,"XII":12,"XIII":13,"XIV":14,"XV":15,"XVI":16,"XVII":17,"XVIII":18,"XIX":19,
        "XX":20,"XXI":21,"XXII":22,"XXIII":23,"XXIV":24,"XXV":25,"XXVI":26,"XXVII":27,
        "XXVIII":28,"XXIX":29,"XXX":30,"XXXI":31}

_VOTE_TOKENS = {"ZA", "PRZECIW", "PRZECIWNA", "WSTRZYMUJE", "OBECNY", "OBECNA", "OBECNI",
                "NIEOBECNY", "NIEOBECNA", "NIEOBECNI"}


def _nk(s):
    s = s.lower().replace("ł", "l")
    s = unicodedata.normalize("NFKD", s)
    return re.sub(r"[^a-z0-9]", "", "".join(c for c in s if not unicodedata.combining(c)))


def _is_vote_token(txt):
    if txt != txt.upper():
        return None
    k = _nk(txt)
    if k in ("za", "z", "ża", "ze"):
        return "za"
    if k.startswith("przeciw"):
        return "przeciw"
    if k.startswith("wstrzym"):
        return "wstrzymal_sie"
    if k.startswith("nieobecn"):
        return "nieobecni"
    if k.startswith("obecn"):
        return "obecny"
    return None


def _rate():
    global _LAST
    d = time.time() - _LAST
    if d < REQ_DELAY:
        time.sleep(REQ_DELAY - d)
    _LAST = time.time()


def _get(url, cache_dir=None, binary=False):
    key = hashlib.md5(url.encode()).hexdigest()
    if cache_dir:
        cache_dir = Path(cache_dir); cache_dir.mkdir(parents=True, exist_ok=True)
        cf = cache_dir / (key + (".bin" if binary else ".json"))
        if cf.is_file() and cf.stat().st_size > 0:
            return cf.read_bytes() if binary else json.loads(cf.read_text(encoding="utf-8"))
    _rate()
    r = requests.get(url, headers=HEADERS, timeout=90, verify=False)
    r.raise_for_status()
    if cache_dir:
        (cache_dir / (key + (".bin" if binary else ".json"))).write_bytes(r.content if binary else r.content)
    return r.content if binary else r.json()


# ---------------- discovery (custom BIP API) ----------------
def discover_sessions(cache=None):
    """30 protokołów sesji IX kad. -> [{num,date,title,voting_pdf_url,aid}]."""
    arts = []
    for off in range(0, 60, 10):
        d = _get(f"{BASE}/api/menu/{PROTOKOLY_MENU}/articles?offset={off}&limit=10", cache)
        batch = d.get("articles", [])
        arts.extend(batch)
        if len(batch) < 10:
            break
    sessions = []
    for a in arts:
        title = ""
        for cf in a.get("columnFields", []):
            if cf.get("fieldId") == 17:
                title = cf.get("value", ""); break
        if not title:
            for af in a.get("aliasFields", []):
                if af.get("alias") == "title":
                    title = af.get("value", ""); break
        rm = re.search(r"Nr\s+([IVXLCDM]+)/", title)
        num = rm.group(1) if rm else ""
        date = _title_date(title)
        if not date:
            try:
                ad = _get(f"{BASE}/api/articles/{a['id']}", cache)
                cd = ad.get("basicData", {}).get("createDate", "")
                if cd:
                    date = cd[:10]
            except Exception:
                pass
        if not date or date < KAD_START:
            continue
        # voting PDF attachment
        try:
            ad = _get(f"{BASE}/api/articles/{a['id']}", cache)
        except Exception:
            continue
        best = None
        for at in ad.get("attachments", []):
            nmn = _nk(at.get("name", "")); ext = (at.get("extension") or "").lower()
            if "glosowan" in nmn:
                lnk = at.get("link", "")
                if not lnk.startswith("http"):
                    lnk = BASE + "/" + lnk.lstrip("/")
                best = lnk
                if ext == "pdf":
                    break
        sessions.append({"num": num, "date": date, "title": title, "aid": a["id"], "pdf": best})
    sessions.sort(key=lambda s: s["date"])
    return sessions


def _title_date(title):
    m = re.search(r"(?:z dnia|w dniu)\s+(\d{1,2})\s+(\w+)\s+(\d{4})\s*r?\.?", title)
    if m:
        mon = {"stycznia":1,"lutego":2,"marca":3,"kwietnia":4,"maja":5,"czerwca":6,"lipca":7,
               "sierpnia":8,"wrzesnia":9,"pazdziernika":10,"listopada":11,"grudnia":12}.get(_nk(m.group(2)))
        if mon:
            return f"{m.group(3)}-{mon:02d}-{int(m.group(1)):02d}"
    m = re.search(r"(?:z dnia|w dniu)\s+(\d{1,2})[.](\d{1,2})[.](\d{4})", title)
    if m:
        return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    return None


# ---------------- PDF parsing (eSesja print, jak Międzyrzecz) ----------------
def _col_tokens(words, table_min):
    rows = []
    for w in words:
        if w["top"] < table_min:
            continue
        txt = w["text"]
        if txt in ("SIĘ", "się", "Się"):
            continue
        if _is_vote_token(txt):
            rows.append({"vote": txt, "x0": w["x0"], "top": w["top"]})
    return rows


def _header_polkowice(words, text):
    """Temat: linie między nagłówkiem sesji a agregatami (Typ głosowania / Liczba
    uprawnionych), z pominięciem licznika głosu i wiodącego numeru porządkowego 'N. '."""
    lines = [l.strip() for l in text.split("\n")]
    # 1. index nagłówka sesji: "30 XXX Sesja Rady Miejskiej w Polkowicach"
    hi = None
    for i, l in enumerate(lines):
        if re.search(r"Sesja Rady Miejskiej w Polkowicach", l):
            hi = i
            break
    if hi is None:
        return "(glosowanie)"
    parts = []
    for l in lines[hi + 1:]:
        if "Typ głosowania" in l or "Liczba uprawnionych" in l:
            break
        if not l or l == "Głosowanie":
            continue
        if re.match(r"^\d{1,3}$", l):  # licznik głosu (np. "1", "2")
            continue
        parts.append(l)
    topic = re.sub(r"\s+", " ", " ".join(parts)).strip(" .:,;-")
    # usuń wiodący "1 " (numer głosu) i "N. " (numer porządkowy tematu)
    topic = re.sub(r"^\d+\s+", "", topic)
    topic = re.sub(r"^\d+\.\s*", "", topic)
    return topic or "(glosowanie)"


def _agg_from_lines(text):
    agg = {}
    for key, pat in [("za", r"Głosy\s+za\s+(\d+)"),
                     ("przeciw", r"Głosy\s+przeciw\s+(\d+)"),
                     ("wstrzym", r"Głosy\s+wstrzymujące\s+się\s+(\d+)"),
                     ("uprawnionych", r"Liczba\s+uprawnionych\s+(\d+)"),
                     ("obecnych", r"Liczba\s+obecnych\s+(\d+)"),
                     ("nieobecnych", r"Liczba\s+nieobecnych\s+(\d+)"),
                     ("obecni_nieglosujacy", r"Obecni\s+niegłosujący\s+(\d+)")]:
        m = re.search(pat, text)
        if m:
            agg[key] = int(m.group(1))
    return agg


def _parse_pdf(data):
    votes = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        cur = None
        for page in pdf.pages:
            words = page.extract_words()
            text = page.extract_text() or ""
            has_agg = ("Kworum" in text or "Liczba uprawnionych" in text)
            has_table = ("Uprawnieni do głosowania" in text)
            up = [w for w in words if _nk(w["text"]) == "uprawnieni" and w["top"] > 150]
            table_min = (min(w["top"] for w in up) + 6) if up else 150.0

            if has_agg:
                if not has_table:
                    # strona-nagłówek bez tabeli (wydruk sesji XX duplikuje każdy głos na
                    # 2 stronach: nagłówek+agregaty oraz nagłówek+agregaty+tabela). Pomijamy.
                    cur = None
                    continue
                # nowy głos (nagłówek + agregaty + Tabela imienna)
                tg = re.search(r"Typ głosowania\s+(\w+)", text)
                if tg and "tajne" in tg.group(1).lower():
                    # głosowanie TAJNE: brak atrybucji per-radny (tylko OBECNY/NIEOBECNY)
                    cur = None
                    continue
                topic = _header_polkowice(words, text)
                agg = _agg_from_lines(text)
                cur = {"topic": topic, "agg": agg, "matched": []}
                votes.append(cur)
                rows = _col_tokens(words, table_min)
            else:
                if cur is None:
                    continue
                rows = _col_tokens(words, table_min)
            if cur is None:
                continue
            for rt in rows:
                vote = rt["vote"]; vx = rt["x0"]; vt = rt["top"]
                lps = [w for w in words if re.match(r"^\d+\.$", w["text"]) and w["x0"] < vx and abs(w["top"] - vt) <= 12]
                xlo = (max(lps, key=lambda w: w["x0"])["x0"] + 4) if lps else 0.0
                name_toks = []
                for w in words:
                    if w["top"] < table_min:
                        continue
                    if w["x0"] < xlo or w["x0"] > vx - 4:
                        continue
                    if abs(w["top"] - vt) > 12:
                        continue
                    if re.match(r"^\d+\.$", w["text"]):
                        continue
                    if w["text"] in ("SIĘ", "się", "Się"):
                        continue
                    if _is_vote_token(w["text"]):
                        continue
                    if _nk(w["text"]) in ("lp", "nazwisko", "imie", "glos", "imię"):
                        continue
                    name_toks.append(w)
                name_toks.sort(key=lambda w: (w["top"], w["x0"]))
                name = " ".join(w["text"] for w in name_toks).strip()
                nt = name.split()
                if len(nt) >= 2:
                    name = " ".join(nt[1:] + [nt[0]])
                cur["matched"].append((name, vote))
    return votes


def records_from_pdf(data):
    votes = _parse_pdf(data)
    out = []
    for v in votes:
        agg = v["agg"] or {}
        counter = Counter()
        for name, vote_txt in v["matched"]:
            norm = _is_vote_token(vote_txt)
            if norm is None:
                continue
            counter[norm] += 1
        ok = (
            agg.get("za") is not None and
            counter.get("za", 0) == agg.get("za") and
            counter.get("przeciw", 0) == agg.get("przeciw", 0) and
            counter.get("wstrzymal_sie", 0) == agg.get("wstrzym", 0) and
            counter.get("nieobecni", 0) == agg.get("nieobecnych", 0)
        )
        named = {"za": [], "przeciw": [], "wstrzymal_sie": [], "nieobecni": []}
        for name, vote_txt in v["matched"]:
            norm = _is_vote_token(vote_txt)
            if norm in named:
                named[norm].append(name)
        out.append({"topic": v["topic"], "named": named, "agg": agg or {}, "ok": ok,
                    "n_matched": len(v["matched"])})
    return out


# ---------------- output ----------------
def make_slug(name):
    repl = {'ą':'a','ć':'c','ę':'e','ł':'l','ń':'n','ó':'o','ś':'s','ź':'z','ż':'z',
            'Ą':'A','Ć':'C','Ę':'E','Ł':'L','Ń':'N','Ó':'O','Ś':'S','Ź':'Z','Ż':'Z'}
    slug = name.lower()
    for pl, a in repl.items():
        slug = slug.replace(pl, a)
    return re.sub(r"[^a-z0-9]+", "", slug)


def build_output(records, club_assign=None):
    club_assign = club_assign or {}
    all_votes = []; vid = 0; sessions_by_date = {}
    for rec in records:
        d = rec["date"]
        if not d or d < KAD_START:
            continue
        if d not in sessions_by_date:
            sessions_by_date[d] = {"date": d, "number": rec.get("num", ""), "vote_count": 0,
                                   "attendees": set(), "speakers": []}
        sessions_by_date[d]["vote_count"] += 1
        named = {k: list(v) for k, v in rec["named"].items()}
        for cat in ("za", "przeciw", "wstrzymal_sie"):
            sessions_by_date[d]["attendees"].update(rec["named"].get(cat, []))
        vid += 1
        all_votes.append({"id": str(vid), "session_date": d, "session_number": rec.get("num", ""),
                          "topic": rec.get("topic", ""), "named_votes": named,
                          "counts": {k: len(named.get(k, [])) for k in ("za", "przeciw", "wstrzymal_sie")}})
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
    for name in all_names:
        councilors_data[name] = {"name": name, "club": club_assign.get(name, "NZ"), "district": None,
            "votes_za": 0, "votes_przeciw": 0, "votes_wstrzymal": 0, "votes_brak": 0,
            "votes_nieobecny": 0, "rebellions": []}
    for v in all_votes:
        for cat, names in v["named_votes"].items():
            if cat == "nieobecni":
                for nm in names:
                    if nm in councilors_data:
                        councilors_data[nm]["votes_nieobecny"] += 1
                continue
            for nm in names:
                if nm not in councilors_data:
                    continue
                if cat == "za":
                    councilors_data[nm]["votes_za"] += 1
                elif cat == "przeciw":
                    councilors_data[nm]["votes_przeciw"] += 1
                else:
                    councilors_data[nm]["votes_wstrzymal"] += 1
    total_votes = len(all_votes); total_sessions = len(sessions_data)
    councillor_sess = defaultdict(set)
    for v in all_votes:
        for cat, names in v["named_votes"].items():
            for nm in names:
                councillor_sess[nm].add(v["session_date"])
    councilors_list = []
    for c in sorted(councilors_data.values(), key=lambda x: x["name"]):
        present = c["votes_za"] + c["votes_przeciw"] + c["votes_wstrzymal"]
        aktywn = (present / total_votes * 100) if total_votes else 0
        frekw = (len(councillor_sess.get(c["name"], set())) / total_sessions * 100) if total_sessions else 0
        councilors_list.append({"name": c["name"], "club": c["club"], "district": None,
            "frekwencja": round(frekw, 1), "aktywnosc": round(aktywn, 1), "zgodnosc_z_klubem": 0.0,
            "votes_za": c["votes_za"], "votes_przeciw": c["votes_przeciw"],
            "votes_wstrzymal": c["votes_wstrzymal"], "votes_brak": c["votes_brak"],
            "votes_nieobecny": c["votes_nieobecny"], "votes_total": total_votes,
            "rebellion_count": 0, "rebellions": [], "has_activity_data": False, "activity": None})
    from itertools import combinations
    vectors = defaultdict(dict)
    for v in all_votes:
        for cat in ("za", "przeciw", "wstrzymal_sie"):
            for nm in v["named_votes"].get(cat, []):
                vectors[nm][v["id"]] = cat
    pairs = []; names_sorted = sorted(vectors.keys())
    for a, b in combinations(names_sorted, 2):
        common = set(vectors[a].keys()) & set(vectors[b].keys())
        if len(common) < 10:
            continue
        same = sum(1 for vid in common if vectors[a][vid] == vectors[b][vid])
        pairs.append({"a": a, "b": b, "club_a": club_assign.get(a, ""), "club_b": club_assign.get(b, ""),
                      "score": round(same / len(common) * 100, 1), "common_votes": len(common)})
    pairs.sort(key=lambda x: x["score"], reverse=True)
    kad = {"id": KADENCJA_ID, "label": KADENCJA_LABEL,
           "clubs": dict(Counter(club_assign.get(c["name"], "NZ") for c in councilors_list)),
           "sessions": sessions_data, "total_sessions": total_sessions,
           "total_votes": total_votes, "total_councilors": len(councilors_list),
           "councilors": councilors_list, "votes": all_votes,
           "similarity_top": pairs[:20], "similarity_bottom": pairs[-20:][::-1]}
    return {"generated": datetime.now().isoformat(), "default_kadencja": KADENCJA_ID, "kadencje": [kad]}, total_votes, total_sessions


def build_profiles(records, club_assign=None):
    club_assign = club_assign or {}
    cv = defaultdict(lambda: {"za": 0, "przeciw": 0, "wstrzymal_sie": 0, "nieobecni": 0, "votes": []})
    for rec in records:
        d = rec["date"]
        if not d or d < KAD_START:
            continue
        for cat, names in rec["named"].items():
            for nm in names:
                cv[nm][cat] += 1
                cv[nm]["votes"].append({"session": d, "vote": cat})
    profiles = []
    sess_set = {r["date"] for r in records if r["date"] >= KAD_START}
    n_sessions = len(sess_set) or 1
    for nm in sorted(cv.keys()):
        vd = cv[nm]
        total = sum(vd[k] for k in ("za", "przeciw", "wstrzymal_sie")) or 1
        sess = len({v["session"] for v in vd["votes"]})
        aktywn = (vd["za"] + vd["przeciw"] + vd["wstrzymal_sie"]) / n_sessions * 100
        profiles.append({"name": nm, "slug": make_slug(nm),
            "kadencje": {KADENCJA_ID: {"club": club_assign.get(nm, "NZ"), "has_voting_data": True,
                "has_activity_data": False, "frekwencja": round(sess / n_sessions * 100, 1),
                "aktywnosc": round(aktywn, 1), "zgodnosc_z_klubem": 0.0,
                "votes_za": vd["za"], "votes_przeciw": vd["przeciw"],
                "votes_wstrzymal": vd["wstrzymal_sie"], "votes_brak": 0, "votes_nieobecny": 0,
                "votes_total": total, "rebellion_count": 0, "rebellions": [],
                "roles": [], "notes": "", "former": False, "mid_term": False}}})
    return {"profiles": profiles, "total": len(profiles)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--city-dir", required=True)
    ap.add_argument("--cache-dir", default=None)
    ap.add_argument("--skip-download", action="store_true")
    args = ap.parse_args()
    city_dir = Path(args.city_dir)
    cache = Path(args.cache_dir) if args.cache_dir else None
    cfg = {}
    if (city_dir / "config.json").is_file():
        cfg = json.loads((city_dir / "config.json").read_text(encoding="utf-8"))
    club_assign = cfg.get("club_assignments", {}) or {}

    sessions = discover_sessions(cache)
    print(f"[polkowice] {len(sessions)} sesji IX kad. (I..{max((_ROM.get(s['num'],0) for s in sessions), default=0)})")

    pdf_dir = city_dir / "pdfs"; pdf_dir.mkdir(parents=True, exist_ok=True)
    records = []; vstat = {"v": 0, "ok": 0, "fail": 0}
    for se in sessions:
        if not se.get("pdf"):
            print(f"  [no voting pdf {se['date']}] skip")
            continue
        fn = f"{_ROM.get(se['num'], 0):02d}.pdf"
        pf = pdf_dir / fn
        if not (pf.is_file() and pf.stat().st_size > 1000) and not args.skip_download:
            data = _get(se["pdf"], cache, binary=True)
            pf.write_bytes(data)
        if not pf.is_file():
            continue
        try:
            recs = records_from_pdf(pf.read_bytes())
            nok = sum(1 for r in recs if r["ok"])
            vstat["v"] += len(recs); vstat["ok"] += nok; vstat["fail"] += len(recs) - nok
            for r in recs:
                r["date"] = se["date"]; r["num"] = se["num"]
            records += recs
            flag = "OK" if nok == len(recs) else f"VALID={nok}/{len(recs)}"
            print(f"  [ok {se['date']}] sesja {se['num']} votes={len(recs)} {flag}")
        except Exception as e:
            print(f"  [ERR {se['date']}] {type(e).__name__}: {e}")

    output, total_votes, total_sessions = build_output(records, club_assign)
    profiles = build_profiles(records, club_assign)
    docs = city_dir / "docs"; docs.mkdir(parents=True, exist_ok=True)
    (docs / "kadencja-2024-2029.json").write_text(json.dumps(output["kadencje"][0], ensure_ascii=False, indent=1), encoding="utf-8")
    data = {"generated": output["generated"], "default_kadencja": KADENCJA_ID,
            "kadencje": [{"id": KADENCJA_ID, "label": KADENCJA_LABEL}]}
    (docs / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    (docs / "profiles.json").write_text(json.dumps(profiles, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[polkowice] DONE votes={total_votes} sessions={total_sessions} "
          f"councilors={len(profiles['profiles'])} validated={vstat['ok']}/{vstat['v']} fail={vstat['fail']}")


if __name__ == "__main__":
    main()
