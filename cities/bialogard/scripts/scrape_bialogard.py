#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Radoskop Białogard — imienne głosowania Rady Miejskiej Białogardu (IX kadencja).

Źródło: BIP bip.bialogard.info (platforma "info"-BIP / art-a-type CMS), sekcja
Rada Miejska → "Porządek sesji i wyniki głosowania" (/sesje/3079). Dla KAŻDEJ sesji
strona /sesja/{id}/... zawiera listę załączników PDF per głosowanie
("Głosowanie N z dnia DD.MM.YYYY" → /attachments/download/{id}).

Każdy PDF = 1 głosowanie, układ DWUKOLUMNOWY bez kolumny Lp:
    "Nazwisko i imię  Głos | Nazwisko i imię  Głos"  (x0≈70 i x0≈357)
agregaty w nagłówku: "Liczba uprawionych N", "Głosy za N", "Liczba obecnych N",
"Głosy przeciw N", "Liczba nieobecnych N", "Głosy wstrzymujące się N",
"Obecni niegłosujący N", "Data głosowania YYYY-MM-DD HH:MM".
Głosy per radny: ZA / PRZECIW / WSTRZYMUJĘ SIĘ / OBECNY / NIEOBECNY(NA).

Granica kolumn = środek między dwoma nagłówkami "Nazwisko". Walidacja per głos:
liczby imienne == agregaty (za/przeciw/wstrzymujące/nieobecni); OBECNY =
obecny-niegłosujący (do frekwencji, poza named_votes).

Użycie:
    python scrape_bialogard.py --city-dir <cities/bialogard> [--cache-dir dir]
Zapisuje: docs/data.json, docs/kadencja-2024-2029.json, docs/profiles.json
"""
import argparse
import hashlib
import io
import json
import re
import ssl
import sys
import time
import urllib.request
import urllib.error
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import pdfplumber

BIP = "https://bip.bialogard.info"
SESSIONS_LIST = "/sesje/3079"
KAD_START = "2024-05-07"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"}
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

_MONTHS = {
    "stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5, "czerwca": 6,
    "lipca": 7, "sierpnia": 8, "września": 9, "pazdziernika": 10, "października": 10,
    "listopada": 11, "grudnia": 12,
}

_last_call = [0.0]


def _rate():
    el = time.time() - _last_call[0]
    if el < 1.2:
        time.sleep(1.2 - el)
    _last_call[0] = time.time()


def _get(url, cache_dir=None, binary=False):
    if cache_dir:
        key = hashlib.md5(url.encode()).hexdigest()
        cf = Path(cache_dir) / key
        if cf.is_file():
            return cf.read_bytes() if binary else cf.read_text(encoding="utf-8", errors="replace")
    for attempt in range(4):
        _rate()
        try:
            req = urllib.request.Request(url, headers=UA)
            r = urllib.request.urlopen(req, timeout=30, context=CTX)
            data = r.read()
            if cache_dir:
                Path(cache_dir).mkdir(parents=True, exist_ok=True)
                cf = Path(cache_dir) / hashlib.md5(url.encode()).hexdigest()
                cf.write_bytes(data)
            return data if binary else data.decode("utf-8", "replace")
        except Exception:
            if attempt == 3:
                raise
            time.sleep(2 + attempt * 3)


ROMAN = r"M{0,4}(?:CM|CD|D?C{0,3})(?:XC|XL|L?X{0,3})(?:IX|IV|V?I{0,3})"


def discover_sessions(cache_dir):
    """Return list of {url, num (roman), title} for all sessions on /sesje/3079 pages."""
    out = {}
    page = 1
    while page <= 12:
        url = f"{BIP}{SESSIONS_LIST}/{page}/25" if page > 1 else f"{BIP}{SESSIONS_LIST}"
        try:
            h = _get(url, cache_dir)
        except Exception:
            break
        found = 0
        for m in re.finditer(r'href="(https://bip\.bialogard\.info/sesja/\d+/[^"]+)"[^>]*>([^<]+)</a>', h):
            href, title = m.group(1), re.sub(r"\s+", " ", m.group(2)).strip()
            if href in out:
                continue
            rm = re.match(rf"^({ROMAN})\s+Sesja", title, re.I)
            out[href] = {"url": href, "num": rm.group(1).upper() if rm else "", "title": title}
            found += 1
        if found == 0:
            break
        page += 1
    return list(out.values())


def _vote_token(text):
    t = text.upper()
    if t.startswith("NIEOBECN"):
        return "nieobecni"
    if t.startswith("WSTRZ"):
        return "wstrzymal_sie"
    if t.startswith("PRZECIW"):
        return "przeciw"
    if t == "ZA":
        return "za"
    if t.startswith("OBECN"):
        return "obecny"
    return None


def parse_pdf(data, url=""):
    """Parse one per-vote PDF → record or None."""
    f = io.BytesIO(data)
    with pdfplumber.open(f) as pdf:
        page = pdf.pages[0]
        text = page.extract_text() or ""
        words = page.extract_words(use_text_flow=False)

    topic_m = re.search(r"Głosowanie\(\d+\):\s*(.+?)(?:\nWYNIKI|\nTyp)", text, re.S)
    if not topic_m:
        topic_m = re.search(r"\bGłosowanie\s*\n(\d+\.\s*.+?)\nTyp", text, re.S)
    topic = re.sub(r"\s+", " ", topic_m.group(1)).strip() if topic_m else ""
    dm = re.search(r"Data głosowania:?\s+(\d{4})-(\d{2})-(\d{2})", text)
    if dm:
        date = f"{dm.group(1)}-{dm.group(2)}-{dm.group(3)}"
    else:
        dm = re.search(r"Data głosowania:?\s+(\d{1,2})\.(\d{1,2})\.(\d{4})", text)
        date = f"{dm.group(3)}-{int(dm.group(2)):02d}-{int(dm.group(1)):02d}" if dm else None

    def agg(pat):
        m = re.search(pat, text)
        return int(m.group(1)) if m else None

    a_za = agg(r"Głosy za\s+(\d+)")
    a_prz = agg(r"Głosy przeciw\s+(\d+)")
    a_wsz = agg(r"Głosy wstrzymujące się\s+(\d+)")
    a_nieb = agg(r"Liczba nieobecnych\s+(\d+)")
    a_nieg = agg(r"Obecni niegłosujący\s+(\d+)")

    # Column boundary from the table header row: two "Nazwisko" and two "Głos" headers.
    # mid = midpoint between the first "Głos" header x and the second "Nazwisko" header x —
    # robust across both observed layouts (with/without Lp column).
    header_words = [w for w in words if w["text"] == "Nazwisko"]
    header_top = min((w["top"] for w in header_words), default=None)
    if header_top is None:
        return None
    glos_xs = sorted(w["x0"] for w in words if w["text"] == "Głos" and w["top"] < header_top + 6)
    naz_xs = sorted(w["x0"] for w in words if w["text"] == "Nazwisko" and w["top"] < header_top + 6)
    if len(glos_xs) < 2 or len(naz_xs) < 2:
        return None
    mid = (glos_xs[0] + naz_xs[1]) / 2.0

    body = [w for w in words if w["top"] > header_top + 2]
    # drop footer line ("Wydrukowano: ...")
    body = [w for w in body if not re.match(r"^Wydrukowano|^Wygenerowano", w["text"])]
    if not body:
        return None

    rows = defaultdict(list)  # (col, round(top/5)) → words
    for w in body:
        if w["text"] in ("SIĘ", "SIE") or w["text"] in ("i", "imię") or w["text"] == "Głos":
            continue
        col = 0 if w["x0"] < mid else 1
        rows[(col, round(w["top"] / 5.0))].append(w)

    named = {"za": [], "przeciw": [], "wstrzymal_sie": [], "nieobecni": []}
    obecny = []
    for key in sorted(rows.keys()):
        ws = sorted(rows[key], key=lambda w: w["x0"])
        # vote token = last all-uppercase word on the row matching a vote label
        vote = None
        name_words = []
        for i in range(len(ws) - 1, -1, -1):
            t = _vote_token(ws[i]["text"]) if ws[i]["text"].isupper() else None
            if t:
                vote = t
                name_words = ws[:i]
                break
        if not vote or not name_words:
            continue
        # drop Lp numeric tokens ("12.") if present
        name_words = [w for w in name_words if not re.match(r"^\d+\.?$", w["text"])]
        if not name_words:
            continue
        nm = " ".join(w["text"] for w in name_words).strip()
        if not nm or any(ch.isdigit() for ch in nm):
            continue
        # names are printed "Nazwisko Imię" → normalize to "Imię Nazwisko"
        parts = nm.split()
        if len(parts) >= 2:
            nm = " ".join(parts[1:]) + " " + parts[0]
        if vote == "obecny":
            obecny.append(nm)
        else:
            named[vote].append(nm)

    ok = (
        a_za is not None and len(named["za"]) == a_za
        and len(named["przeciw"]) == (a_prz or 0)
        and len(named["wstrzymal_sie"]) == (a_wsz or 0)
        and len(named["nieobecni"]) == (a_nieb or 0)
        and len(obecny) == (a_nieg or 0)
    )
    if not ok or not date:
        print(f"    [VALIDATION FAIL {url}] agg za/prz/wsz/nieb/nieg={a_za}/{a_prz}/{a_wsz}/{a_nieb}/{a_nieg} "
              f"parsed {len(named['za'])}/{len(named['przeciw'])}/{len(named['wstrzymal_sie'])}/"
              f"{len(named['nieobecni'])}/{len(obecny)}")
        return None
    return {"date": date, "topic": topic, "named": named, "obecni_nie_glosujacy": obecny}


def roman_to_int(s):
    vals = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
    tot = 0
    prev = 0
    for ch in reversed(s):
        v = vals.get(ch, 0)
        tot += v if v >= prev else -v
        prev = max(prev, v)
    return tot


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
        named = {k: list(v) for k, v in rec["named"].items()}
        for cat in ("za", "przeciw", "wstrzymal_sie"):
            sessions_by_date[d]["attendees"].update(rec["named"].get(cat, []))
        sessions_by_date[d]["attendees"].update(rec.get("obecni_nie_glosujacy", []))
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
                                 "votes_brak": 0, "votes_nieobecny": 0, "rebellions": []}
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
        for cat, names in v["named_votes"].items():
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
    names_sorted = sorted(vectors.keys())
    for a, b in combinations(names_sorted, 2):
        common = set(vectors[a].keys()) & set(vectors[b].keys())
        if len(common) < 10:
            continue
        same = sum(1 for v_id in common if vectors[a][v_id] == vectors[b][v_id])
        pairs.append({"a": a, "b": b, "club_a": "", "club_b": "",
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
                         "kadencje": {KADENCJA_ID: {
                             "club": club_assign.get(nm, "NZ"), "has_voting_data": True,
                             "has_activity_data": False, "frekwencja": round(sess / n_sessions * 100, 1),
                             "aktywnosc": round(aktywn, 1), "zgodnosc_z_klubem": 0.0,
                             "votes_za": vd["za"], "votes_przeciw": vd["przeciw"],
                             "votes_wstrzymal": vd["wstrzymal_sie"], "votes_brak": 0,
                             "votes_nieobecny": vd["nieobecni"], "votes_total": total,
                             "rebellion_count": 0, "rebellions": [], "roles": [], "notes": "",
                             "former": False, "mid_term": False}}})
    return {"profiles": profiles, "total": len(profiles)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--city-dir", required=True)
    ap.add_argument("--cache-dir", default=None)
    args = ap.parse_args()
    city_dir = Path(args.city_dir)
    cache = Path(args.cache_dir) if args.cache_dir else None
    cfg = {}
    if (city_dir / "config.json").is_file():
        cfg = json.loads((city_dir / "config.json").read_text(encoding="utf-8"))
    club_assign = cfg.get("club_assignments", {}) or {}

    sessions = discover_sessions(cache)
    print(f"[bialogard] {len(sessions)} sesji na liscie")
    records = []
    for se in sorted(sessions, key=lambda s: roman_to_int(s["num"] or "0")):
        try:
            art = _get(se["url"], cache)
        except Exception as e:
            print(f"  [ERR page {se['url']}] {e}")
            continue
        hrefs = re.findall(r'href="(https://bip\.bialogard\.info/attachments/download/\d+)"[^>]*>\s*Głosowanie\s+(\d+)', art)
        if not hrefs:
            print(f"  [skip {se['num']} no vote pdfs]")
            continue
        n_ok = 0
        for url, gnum in hrefs:
            try:
                data = _get(url, cache, binary=True)
                rec = parse_pdf(data, url)
            except Exception as e:
                print(f"  [ERR pdf {url}] {type(e).__name__}: {e}")
                continue
            if rec:
                rec["num"] = se["num"]
                rec["session_title"] = se["title"]
                records.append(rec)
                n_ok += 1
        print(f"  [ok] {se['num'] or '?':>4} votes_ok={n_ok}/{len(hrefs)}")

    output, total_votes, total_sessions = build_output(records, club_assign)
    profiles = build_profiles(records, club_assign)
    docs = city_dir / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "data.json").write_text(json.dumps(output, ensure_ascii=False, indent=1), encoding="utf-8")
    kad = output["kadencje"][0]
    (docs / f"kadencja-{KADENCJA_ID}.json").write_text(json.dumps(kad, ensure_ascii=False), encoding="utf-8")
    (docs / "profiles.json").write_text(json.dumps(profiles, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[bialogard] DONE votes={total_votes} sessions={total_sessions} councilors={kad['total_councilors']}")


if __name__ == "__main__":
    main()
