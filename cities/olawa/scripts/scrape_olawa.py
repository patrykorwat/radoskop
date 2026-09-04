#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Radoskop Oława — imienne głosowania Rady Miejskiej w Oławie (IX kadencja 2024-2029).

Źródło: BIP bip.um.olawa.pl (platforma Madkom, React SPA). JSON API (odkryte z
static/js/main.*.chunk.js):
  GET /api/menu/{menuId}                -> drzewo menu (children zagnieżdżone)
  GET /api/menu/{menuId}/articles       -> lista artykułów kategorii {articles:[{id,link,...}]}
  GET /api/articles/{id}                -> artykuł + attachments[{id,name,link,extension}]
  GET /{attachment.link}                -> plik (link = "e,pobierz,get.html?id=NNN")

Struktura BIP: Rada Miejska (m186) → Sesje (m171) → Kadencja IX 2024-2029 (m434) —
31 artykułów "Sesja Rady Miejskiej w Oławie w dniu <D> <miesiąc> <R> r. o godz. ..."
(+ "Nadzwyczajna sesja ..."). Każdy artykuł sesji ma załączniki porządkowe ORAZ
per-głosowanie PDF-y z "_głosowanie" w nazwie (np. "Uchwała Nr XIII.108.25_głosowanie.pdf",
"Przyjęcie protokołu Nr 12.2025_głosowanie.pdf", "N_Wprowadzenie do porządku Sesji pkt.5_głosowanie.pdf").

PDF = wydruk eSesja (format "plain"): jedna strona = jedno głosowanie; dwukolumnowa
tabela "Lp | Nazwisko i imię | Głos" (ZA/PRZECIW/WSTRZYMUJE SIĘ/OBECNY/A/NIEOBECNY/A),
agregaty "Głosy za N / Głosy przeciw N / Głosy wstrzymujące się N / Liczba nieobecnych N /
Obecni niegłosujący N" (etykieta w linii, wartość w NASTEPNEJ linii — inaczej niż
miedzyrzecz). Temat = linie przed "Sesja Nr" (z leading numerem strony do usunięcia).

Parser token-ankorowany (wzorzec miedzyrzecz): nazwisko = słowa między Lp wiersza a
tokenem głosu w oknie pionowym ±12pt; walidacja per głos: liczone == agregaty.
Roster = unia nazwisk (zmiany w trakcie kadencji). Obrót "Nazwisko Imię" -> "Imię Nazwisko".

Użycie:
    python scrape_olawa.py --city-dir <cities/olawa> [--cache-dir dir] [--skip-download]
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

BIP = "https://bip.um.olawa.pl"
SESJE_KAD_IX_MENU = 434
KAD_START = "2024-05-07"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"

REQ_DELAY = 0.4
_LAST = 0.0

MONTHS = {"stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5, "czerwca": 6,
          "lipca": 7, "sierpnia": 8, "wrzesnia": 9, "pazdziernika": 10, "listopada": 11,
          "grudnia": 12}


def _nk(s):
    s = s.lower().replace("ł", "l")
    s = unicodedata.normalize("NFKD", s)
    return re.sub(r"[^a-z0-9]", "", "".join(c for c in s if not unicodedata.combining(c)))


_VOTE_CATS = ("za", "przeciw", "wstrzymal_sie", "nieobecni", "obecny")


def _is_vote_token(txt):
    if txt != txt.upper():
        return None
    k = _nk(txt)
    if k in ("za", "z", "ża"):
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


def _get(url, cache_dir, is_json=False):
    key = hashlib.md5(url.encode()).hexdigest()
    cf = None
    if cache_dir:
        cd = Path(cache_dir); cd.mkdir(parents=True, exist_ok=True)
        cf = cd / (key + ".dat")
        if cf.is_file():
            return cf.read_bytes()
    _rate()
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0 (Radoskop)"}, timeout=60, verify=False)
    r.raise_for_status()
    data = r.content
    if cf is not None:
        cf.write_bytes(data)
    return data


def _get_json(url, cache_dir):
    return json.loads(_get(url, cache_dir).decode("utf-8", "replace"))


# ---------------- discovery ----------------
def _find_node(nodes, want):
    for n in nodes or []:
        if str(n.get("id")) == str(want):
            return n
        r = _find_node(n.get("children"), want)
        if r:
            return r
    return None


def discover_sessions(cache_dir=None):
    """Menu 434 -> sesje IX kad. z datami z tytułu."""
    d = _get_json(f"{BIP}/api/menu/{SESJE_KAD_IX_MENU}/articles?limit=200", cache_dir)
    sessions = []
    seen = set()
    for a in d.get("articles", []):
        aid = a.get("id")
        if aid in seen:
            continue
        seen.add(aid)
        title = ""
        for cf in a.get("columnFields", []):
            if cf.get("fieldId") == 22 and cf.get("value"):
                title = cf["value"]
                break
        if not title:
            for af in a.get("aliasFields", []):
                if af.get("alias") == "title":
                    title = af.get("value", "")
                    break
        dm = re.search(r"w dniu (\d{1,2})\s+(\w+)\s+(\d{4})", title)
        if not dm:
            continue
        mon = MONTHS.get(_nk(dm.group(2)))
        if not mon:
            continue
        date = f"{dm.group(3)}-{mon:02d}-{int(dm.group(1)):02d}"
        if date < KAD_START:
            continue
        sessions.append({"aid": aid, "date": date, "title": title})
    sessions.sort(key=lambda s: s["date"])
    # attachments per session
    for se in sessions:
        art = _get_json(f"{BIP}/api/articles/{se['aid']}", cache_dir)
        atts = []
        for at in art.get("attachments") or []:
            nm = at.get("name") or ""
            ext = (at.get("extension") or "").lower()
            if ext == "pdf" and "glosowanie" in _nk(nm):
                link = at.get("link") or ""
                if not link.startswith("http"):
                    link = BIP + "/" + link
                atts.append({"id": at.get("id"), "name": nm, "url": link})
        se["atts"] = atts
    return sessions


# ---------------- PDF parsing ----------------
def _agg_from_text(text):
    """Agregaty. Dwa układy wydruku Oławy:
    (a) 'Liczba uprawnionych 21 ... Głosy za 16' — wartość w tej SAMEJ linii co etykieta;
    (b) dwa wiersze: etykiety w 1. linii, wartości w następnej (naprzemiennie kolumny).
    Dlatego wzorce dopuszczają max jeden przełamania linii między etykietą a liczbą."""
    agg = {}
    t = re.sub(r"[ \t]+", " ", text)
    # układy przeplatane (dwa wiersze: dwie etykiety, potem dwie wartości) — najpierw
    for pat, k1, k2 in [
        (r"Liczba\s+uprawnionych\s+Głosy\s+za\s*\n\s*(\d+)\s+(\d+)", "uprawnionych", "za"),
        (r"Liczba\s+obecnych\s+Głosy\s+przeciw\s*\n\s*(\d+)\s+(\d+)", "obecnych", "przeciw"),
    ]:
        m = re.search(pat, t)
        if m:
            agg[k1] = int(m.group(1))
            agg[k2] = int(m.group(2))
    for key, pat in [
        ("za", r"(?<![\d.])Głosy\s+za\s*\n?\s*(\d+)"),
        ("przeciw", r"(?<![\d.])Głosy\s+przeciw\s*\n?\s*(\d+)"),
        ("wstrzym", r"(?<![\d.])Głosy\s+wstrzymujące\s+się\s*\n?\s*(\d+)"),
        ("uprawnionych", r"(?<![\d.])Liczba\s+uprawnionych\s*\n?\s*(\d+)"),
        ("obecnych", r"(?<![\d.])Liczba\s+obecnych\s*\n?\s*(\d+)"),
        ("nieobecnych", r"(?<![\d.])Liczba\s+nieobecnych\s*\n?\s*(\d+)"),
        ("obecni_nieglosujacy", r"(?<![\d.])Obecni\s+niegłosujący\s*\n?\s*(\d+)"),
    ]:
        if key in agg:
            continue  # układ przeplatany już ustalony — nie nadpisywać błędnym dopasowaniem pojedynczym
        m = re.search(pat, t)
        if m:
            agg[key] = int(m.group(1))
    return agg


def _topic_from_text(text):
    """Temat głosowania. Dwa układy wydruku:
    (a) temat PRZED linią 'Sesja Nr ...' (standard);
    (b) '75 Sesja Nr ...' na górze, temat między linią 'Głosowanie' a 'Typ głosowania'."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]

    def _clean(out):
        topic = " ".join(out)
        topic = re.sub(r"^\d+\s+", "", topic)             # numer strony wydruku
        topic = re.sub(r"^G[łl]osowanie\s+w\s+sprawie:?\s*", "", topic, flags=re.I)
        topic = re.sub(r"^(\d+\s+)?\d+\.\s*", "", topic)  # numer głosu + punkt porządku
        topic = re.sub(r"\s+", " ", topic).strip(" .:,;-")
        return topic

    # układ (a): tekst przed 'Sesja Nr'
    pre = []
    for l in lines:
        if re.match(r"^(\d+\s+)?Sesja\s+Nr\b", l) or _nk(l).startswith("glosowanie"):
            break
        pre.append(l)
    topic_a = _clean(pre)
    # układ (b): tekst między samotnym 'Głosowanie' a 'Typ'
    topic_b = ""
    gi = next((i for i, l in enumerate(lines) if re.fullmatch(r"G[łl]osowanie", l)), None)
    if gi is not None:
        mid = []
        for l in lines[gi + 1:]:
            if re.match(r"^Typ\b", l):
                break
            mid.append(l)
        topic_b = _clean(mid)
    # wybieramy dłuższy sensowny (a ma sens gdy pre jest wielolinijkowe; b gdy a pusty/liczba)
    def _sane(t):
        return t and not re.fullmatch(r"\d+", t) and len(t) > 5
    if _sane(topic_b) and not _sane(topic_a):
        return topic_b
    if _sane(topic_a):
        return topic_a
    return topic_b if _sane(topic_b) else "(glosowanie)"


def _parse_pdf(data):
    """Jedno głosowanie na stronę (wydruk eSesja plain). Zwraca listę głosów."""
    votes = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            if "Kworum" not in text and "uprawnionych" not in text:
                continue
            words = page.extract_words()
            agg = _agg_from_text(text)
            topic = _topic_from_text(text)
            up = [w for w in words if _nk(w["text"]) == "uprawnieni" and w["top"] > 200]
            table_min = (min(w["top"] for w in up) + 6) if up else 350.0
            matched = []
            for w in words:
                if w["top"] < table_min:
                    continue
                cat = _is_vote_token(w["text"])
                if not cat:
                    continue
                vx, vt = w["x0"], w["top"]
                lps = [x for x in words
                       if re.match(r"^\d+\.$", x["text"]) and x["x0"] < vx and abs(x["top"] - vt) <= 12]
                xlo = (max(lps, key=lambda x: x["x0"])["x0"] + 4) if lps else 0.0
                name_toks = []
                for x in words:
                    if x["top"] < table_min or x["x0"] < xlo or x["x0"] > vx - 4:
                        continue
                    if abs(x["top"] - vt) > 12:
                        continue
                    if re.match(r"^\d+\.?$", x["text"]) or x["text"] in ("SIĘ", "się", "Się"):
                        continue
                    if _is_vote_token(x["text"]):
                        continue
                    if _nk(x["text"]) in ("lp", "nazwisko", "imie", "glos", "i"):
                        continue
                    name_toks.append(x)
                name_toks.sort(key=lambda x: (x["top"], x["x0"]))
                name = " ".join(x["text"] for x in name_toks).strip()
                toks = name.split()
                if len(toks) >= 2:
                    name = " ".join(toks[1:] + [toks[0]])  # Nazwisko Imię -> Imię Nazwisko
                if name == "Karolina Kaczor":  # ten sama radna co Kaczor-Hanuszewicz (roster 21, zakresy dat rozłączne)
                    name = "Karolina Kaczor-Hanuszewicz"
                matched.append((name, cat))
            votes.append({"topic": topic, "agg": agg, "matched": matched})
    return votes


def records_from_pdf(data):
    out = []
    for v in _parse_pdf(data):
        agg = v["agg"] or {}
        counter = Counter(cat for _n, cat in v["matched"])
        ok = (
            agg.get("za") is not None
            and counter.get("za", 0) == agg.get("za", 0)
            and counter.get("przeciw", 0) == agg.get("przeciw", 0)
            and counter.get("wstrzymal_sie", 0) == agg.get("wstrzym", 0)
            and counter.get("nieobecni", 0) == agg.get("nieobecnych", 0)
            and counter.get("obecny", 0) == agg.get("obecni_nieglosujacy", 0)
        )
        named = {"za": [], "przeciw": [], "wstrzymal_sie": [], "nieobecni": []}
        for name, cat in v["matched"]:
            if cat in named:
                named[cat].append(name)
        m = re.search(r"Sesja\s+Nr\s+([IVXLCDM]+)", _meta_text(data)) or None
        out.append({"topic": v["topic"], "named": named, "agg": agg, "ok": ok,
                    "n_matched": len(v["matched"])})
    return out


_meta_cache = {}


def _meta_text(data):
    k = hashlib.md5(data).hexdigest()[:12]
    if k not in _meta_cache:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            _meta_cache[k] = pdf.pages[0].extract_text() or ""
    return _meta_cache[k]


# ---------------- output ----------------
def make_slug(name):
    repl = {'ą': 'a', 'ć': 'c', 'ę': 'e', 'ł': 'l', 'ń': 'n', 'ó': 'o', 'ś': 's', 'ź': 'z', 'ż': 'z',
            'Ą': 'A', 'Ć': 'C', 'Ę': 'E', 'Ł': 'L', 'Ń': 'N', 'Ó': 'O', 'Ś': 'S', 'Ź': 'Z', 'Ż': 'Z'}
    slug = name.lower()
    for pl, a in repl.items():
        slug = slug.replace(pl, a)
    return re.sub(r"[^a-z0-9]+", "", slug)


def build_output(records, club_assign=None):
    club_assign = club_assign or {}
    all_votes = []
    vid = 0
    sessions_by_date = {}
    for rec in records:
        d = rec["date"]
        if not d or d < KAD_START:
            continue
        if d not in sessions_by_date:
            sessions_by_date[d] = {"date": d, "number": rec.get("num", ""), "vote_count": 0,
                                   "attendees": set()}
        sessions_by_date[d]["vote_count"] += 1
        if rec.get("num") and not sessions_by_date[d]["number"]:
            sessions_by_date[d]["number"] = rec["num"]
        named = {k: list(v) for k, v in rec["named"].items()}
        for cat in ("za", "przeciw", "wstrzymal_sie"):
            sessions_by_date[d]["attendees"].update(named.get(cat, []))
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
                                 "votes_za": 0, "votes_przeciw": 0, "votes_wstrzymal": 0,
                                 "votes_brak": 0, "votes_nieobecny": 0}
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
    total_votes = len(all_votes)
    total_sessions = len(sessions_data)
    councillor_sess = defaultdict(set)
    for v in all_votes:
        for names in v["named_votes"].values():
            for nm in names:
                councillor_sess[nm].add(v["session_date"])
    councilors_list = []
    for c in sorted(councilors_data.values(), key=lambda x: x["name"]):
        present = c["votes_za"] + c["votes_przeciw"] + c["votes_wstrzymal"]
        aktywn = (present / total_votes * 100) if total_votes else 0
        frekw = (len(councillor_sess.get(c["name"], set())) / total_sessions * 100) if total_sessions else 0
        councilors_list.append({"name": c["name"], "club": c["club"], "district": None,
                                "frekwencja": round(frekw, 1), "aktywnosc": round(aktywn, 1),
                                "zgodnosc_z_klubem": 0.0,
                                "votes_za": c["votes_za"], "votes_przeciw": c["votes_przeciw"],
                                "votes_wstrzymal": c["votes_wstrzymal"], "votes_brak": c["votes_brak"],
                                "votes_nieobecny": c["votes_nieobecny"], "votes_total": total_votes,
                                "rebellion_count": 0, "rebellions": [], "has_activity_data": False,
                                "activity": None})
    vectors = defaultdict(dict)
    for v in all_votes:
        for cat in ("za", "przeciw", "wstrzymal_sie"):
            for nm in v["named_votes"].get(cat, []):
                vectors[nm][v["id"]] = cat
    from itertools import combinations
    pairs = []
    for a, b in combinations(sorted(vectors.keys()), 2):
        common = set(vectors[a].keys()) & set(vectors[b].keys())
        if len(common) < 10:
            continue
        same = sum(1 for vid_ in common if vectors[a][vid_] == vectors[b][vid_])
        pairs.append({"a": a, "b": b, "club_a": club_assign.get(a, ""), "club_b": club_assign.get(b, ""),
                      "score": round(same / len(common) * 100, 1), "common_votes": len(common)})
    pairs.sort(key=lambda x: x["score"], reverse=True)
    kad = {"id": KADENCJA_ID, "label": KADENCJA_LABEL,
           "clubs": dict(Counter(club_assign.get(c["name"], "NZ") for c in councilors_list)),
           "sessions": sessions_data, "total_sessions": total_sessions,
           "total_votes": total_votes, "total_councilors": len(councilors_list),
           "councilors": councilors_list, "votes": all_votes,
           "similarity_top": pairs[:20], "similarity_bottom": pairs[-20:][::-1]}
    return {"generated": datetime.now().isoformat(), "default_kadencja": KADENCJA_ID,
            "kadencje": [kad]}, total_votes, total_sessions


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
        aktywn = (vd["za"] + vd["przeciw"] + vd["wstrzymal_sie"]) / (n_sessions * 5) * 100
        profiles.append({"name": nm, "slug": make_slug(nm),
                         "kadencje": {KADENCJA_ID: {
                             "club": club_assign.get(nm, "NZ"), "has_voting_data": True,
                             "has_activity_data": False,
                             "frekwencja": round(sess / n_sessions * 100, 1),
                             "aktywnosc": round(min(aktywn, 100.0), 1),
                             "zgodnosc_z_klubem": 0.0,
                             "votes_za": vd["za"], "votes_przeciw": vd["przeciw"],
                             "votes_wstrzymal": vd["wstrzymal_sie"], "votes_brak": 0,
                             "votes_nieobecny": 0, "votes_total": total,
                             "rebellion_count": 0, "rebellions": [],
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
    print(f"[olawa] sesji IX kad: {len(sessions)} (najnowsza {sessions[-1]['date'] if sessions else '-'})")

    records = []
    vstat = {"v": 0, "ok": 0, "fail": 0}
    roman_pat = re.compile(r"Sesja\s+Nr\s+([IVXLCDM]+)")
    for se in sessions:
        se_records = []
        num = ""
        for att in se["atts"]:
            key = f"{se['aid']}-{att['id']}"
            pf = city_dir / "pdfs" / f"{key}.pdf"
            data = None
            if pf.is_file() and pf.stat().st_size > 1000:
                data = pf.read_bytes()
            elif not args.skip_download:
                try:
                    data = _get(att["url"], cache)
                    pf.parent.mkdir(parents=True, exist_ok=True)
                    pf.write_bytes(data)
                except Exception as e:
                    print(f"  [ERR dl {att['name']}] {type(e).__name__}: {e}")
                    continue
            if not data:
                continue
            rm = roman_pat.search(_meta_text(data))
            if rm and not num:
                num = rm.group(1)
            try:
                recs = records_from_pdf(data)
            except Exception as e:
                print(f"  [ERR parse {att['name']}] {type(e).__name__}: {e}")
                continue
            nok = sum(1 for r in recs if r["ok"])
            vstat["v"] += len(recs); vstat["ok"] += nok; vstat["fail"] += len(recs) - nok
            for r in recs:
                r["date"] = se["date"]; r["num"] = num
            se_records += recs
        records += se_records
        flag = "OK" if all(r["ok"] for r in se_records) else f"valid={sum(1 for r in se_records if r['ok'])}/{len(se_records)}"
        print(f"  [{se['date']}] sesja {num or '?'} votes={len(se_records)} {flag}")

    output, total_votes, total_sessions = build_output(records, club_assign)
    profiles = build_profiles(records, club_assign)
    docs = city_dir / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "kadencja-2024-2029.json").write_text(
        json.dumps(output["kadencje"][0], ensure_ascii=False, indent=1), encoding="utf-8")
    data = {"generated": output["generated"], "default_kadencja": KADENCJA_ID,
            "kadencje": [{"id": KADENCJA_ID, "label": KADENCJA_LABEL}]}
    (docs / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    (docs / "profiles.json").write_text(json.dumps(profiles, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[olawa] DONE votes={total_votes} sessions={total_sessions} "
          f"councilors={len(profiles['profiles'])} validated={vstat['ok']}/{vstat['v']} fail={vstat['fail']}")
    if vstat["fail"]:
        print(f"[olawa] UWAGA: {vstat['fail']} głosów niezwalidowanych")


if __name__ == "__main__":
    main()
