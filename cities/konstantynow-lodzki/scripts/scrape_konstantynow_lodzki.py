#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop Konstantynów Łódzki — głosowania imienne (samorzad.gov.pl BIP).

Źródło: https://samorzad.gov.pl/web/gmina-konstantynow-lodzki/sprawozdania-z-glosowan
— seria załączników "Głosowanie(a) imienne <RZYMSKA> sesja" (/attachment/{uuid}).
Każdy PDF = jedna sesja; per głosowanie blok:
    <RZYMSKA> Sesja Rady Miejskiej w Konstantynowie Łódzkim
    Urząd Miejski w Konstantynowie Łódzkim
    <temat>
    <druk_id>
    Czas głosowania: YYYY-MM-DD HH:MM
    WYNIKI GŁOSOWANIA  Za: n | Przeciw: n | Wstrzymali się: n | Uprawnieni: n
    Lp. Imię i nazwisko głos
    1\nJan Kowalski\nza ...
Daty sesji: kategoria /sesje-rady-miejskiej (jeśli pusta — data pierwszego
głosowania z PDF). Kadencja IX = sesje I... od 2024-04.

Użycie: python scrape_konstantynow_lodzki.py [city_dir]
"""
import json
import re
import sys
import time
import unicodedata
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pymupdf

BASE = "https://samorzad.gov.pl"
CAT_URL = f"{BASE}/web/gmina-konstantynow-lodzki/sprawozdania-z-glosowan"
UA = {"User-Agent": "Mozilla/5.0 Radoskop/1.0 (info@radoskop.eu)"}
IX_START = "2024-05-07"  # otwarcie kadencji IX; sesje VIII-kad. (LXII...) odcinane


def _http(url: str) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def _html(url: str) -> str:
    return _http(url).decode("utf-8", "replace")


def slugify(name: str) -> str:
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace("ł", "l")
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-") or "radny"


ROMAN_VAL = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}


def roman2int(r: str) -> int:
    r = r.upper().strip()
    if not re.fullmatch(r"[MDCLXVI]+", r):
        return 0
    out = prev = 0
    for ch in reversed(r):
        v = ROMAN_VAL[ch]
        out += v if v >= prev else -v
        prev = max(prev, v)
    return out


def list_attachments() -> list[tuple[str, str]]:
    h = _html(CAT_URL)
    h = h.replace("&#8203;", "")
    out, seen = [], set()
    for m in re.finditer(r'href="(/attachment/[0-9a-f-]+)"[^>]*>\s*([^<]{4,90}?)\s*<', h, re.I):
        url, label = m.group(1), " ".join(m.group(2).split())
        if "imienne" not in label.lower():
            continue
        if url in seen:
            continue
        seen.add(url)
        out.append((url, label))
    return out


VOTE_MAP = {"za": "za", "przeciw": "przeciw", "wstrzym": "wstrzymal_sie",
            "wstrzyma": "wstrzymal_sie", "nieobecny": "nieobecni",
            "nieobecna": "nieobecni", "nie": "brak_glosu"}


def norm_vote(tok: str):
    t = tok.strip().lower().rstrip(".:")
    if t in ("za",):
        return "za"
    if t in ("przeciw",):
        return "przeciw"
    if t.startswith("wstrzym"):
        return "wstrzymal_sie"
    if t.startswith("nieobecn"):
        return "nieobecni"
    if t == "nie":
        return "brak_glosu"
    return None


HDR_RE = re.compile(
    r"\n([A-Z]{1,10}?)\s+"                      # rzymska (na pocz. wiersza)
    r"Czas g[łl]osowania:\s*(\d{4}-\d{2}-\d{2})\s+\d{2}:\d{2}\s*\n"
    r"WYNIKI G[ŁL]OSOWANIA\s+Za:\s*(\d+)\s*\|\s*Przeciw:\s*(\d+)\s*\|\s*"
    r"Wstrzymali? si[ęe]:\s*(\d+)\s*\|\s*Uprawnieni:\s*(\d+)", re.S)


def parse_pdf(pdf: bytes):
    doc = pymupdf.open(stream=pdf, filetype="pdf")
    pages = [p.get_text() for p in doc]
    doc.close()
    votes = []
    for ptext in pages:
        lines = [l.rstrip() for l in ptext.split("\n")]
        # wynik header w tej stronie
        m = re.search(r"WYNIKI GŁOSOWANIA\s+Za:\s*(\d+)\s*\|\s*Przeciw:\s*(\d+)\s*\|\s*"
                      r"Wstrzymali? się:\s*(\d+)\s*\|\s*Uprawnieni:\s*(\d+)", ptext)
        cm = re.search(r"Czas głosowania:\s*(\d{4}-\d{2}-\d{2})", ptext)
        if not (m and cm):
            continue
        date = cm.group(1)
        n_za, n_pr, n_ws, n_up = (int(m.group(i)) for i in range(1, 5))
        # temat: wiersz przed samotnym numerem druku przed "Czas głosowania"
        topic = ""
        idxs = [i for i, l in enumerate(lines) if re.fullmatch(r"\d{4,6}", l.strip())]
        for i in idxs:
            for j in range(i - 1, max(0, i - 4), -1):
                if lines[j].strip() and "Konstantynow" not in lines[j] and "Sesja" not in lines[j] and "Urząd" not in lines[j]:
                    topic = " ".join(lines[j].split())
                    break
            if topic:
                break
        roll = []
        i = 0
        while i < len(lines):
            if lines[i].strip().isdigit() and i + 2 < len(lines):
                nm = " ".join(lines[i + 1].split())
                key = norm_vote(lines[i + 2])
                if key and re.fullmatch(r"[A-ZŚŻŹĆĄŃÓŁ][\włżźćąóńś]+ [A-ZŚŻŹĆĄŃÓŁ][\włżźćąóńś]+", nm):
                    roll.append((nm, key))
                    i += 3
                    continue
            i += 1
        tally = {"za": 0, "przeciw": 0, "wstrzymal_sie": 0, "brak_glosu": 0, "nieobecni": 0}
        for _, key in roll:
            tally[key] += 1
        ok = (tally["za"] == n_za and tally["przeciw"] == n_pr and tally["wstrzymal_sie"] == n_ws)
        votes.append({"date": date, "topic": topic[:200] or "głosowanie",
                      "counts": {"uprawnieni": n_up, "za": n_za, "przeciw": n_pr, "wstrzymal_sie": n_ws},
                      "roll": roll, "ok": ok})
    return votes


def main(city_dir: Path):
    docs = city_dir / "docs"
    docs.mkdir(exist_ok=True)
    atts = list_attachments()
    print(f"[kkl] {len(atts)} załączników 'głosowanie imienne'")
    all_names, sessions, votes_out = [], [], []
    per_session = {}
    for url, label in atts:
        rm = re.search(r"([IVXLC]+)\s+sesja", label, re.I)
        roman = rm.group(1).upper() if rm else label
        try:
            pdf = _http(BASE + url)
        except Exception as e:
            print(f"  [warn] {label}: {e}")
            continue
        if pdf[:4] != b"%PDF":
            continue
        try:
            vs = parse_pdf(pdf)
        except Exception as e:
            print(f"  [warn] parse {label}: {e}")
            continue
        time.sleep(0.3)
        good = [v for v in vs if v["ok"]]
        if not good:
            print(f"  [skip] {label} (0/OK głosów)")
            continue
        dates = sorted(v["date"] for v in good)
        if dates[0] < IX_START:
            continue
        per_session[roman2int(roman)] = (dates[0], roman, good, BASE + url, label)
        print(f"  [ok] sesja {roman}: {len(good)}/{len(vs)} głosów, {dates[0]}")
    for key in sorted(per_session):
        date, roman, good, url, label = per_session[key]
        idx = {nm: n for n, nm in enumerate(all_names)}
        for v in good:
            nv = {"za": [], "przeciw": [], "wstrzymal_sie": [], "brak_glosu": [], "nieobecni": []}
            for nm, k in v["roll"]:
                if nm not in idx:
                    idx[nm] = len(all_names)
                    all_names.append(nm)
                nv[k].append(idx[nm])
            c = v["counts"]
            votes_out.append({
                "id": f"{v['date']}_{len(votes_out):03d}", "source_url": url,
                "session_date": v["date"], "session_number": roman,
                "topic": v["topic"], "druk": "None",
                "resolution": "przyjete" if c["za"] > c["przeciw"] else "odrzucone",
                "counts": c, "named_votes": nv})
        sess_dates = sorted({v["date"] for v in good})
        for d in sess_dates:
            cnt = sum(1 for v in good if v["date"] == d)
            sessions.append({"date": d, "number": roman,
                             "label": f"Sesja {roman} ({d})", "vote_count": cnt,
                             "attendee_count": None, "attendees": [], "speakers": []})
    councilors = []
    for nm in all_names:
        i = all_names.index(nm)
        z = p_ = w = b_ = nb = 0
        for v in votes_out:
            nv = v["named_votes"]
            if i in nv["za"]:
                z += 1
            elif i in nv["przeciw"]:
                p_ += 1
            elif i in nv["wstrzymal_sie"]:
                w += 1
            elif i in nv["brak_glosu"]:
                b_ += 1
            elif i in nv["nieobecni"]:
                nb += 1
        tot = z + p_ + w + b_
        councilors.append({"name": nm, "club": "", "district": None,
                           "votes_za": z, "votes_przeciw": p_, "votes_wstrzymal": w,
                           "votes_brak": b_, "votes_nieobecny": nb, "votes_total": tot,
                           "frekwencja": round(100.0 * (z + p_ + w) / tot, 1) if tot else None,
                           "aktywnosc": None, "zgodnosc_z_klubem": None,
                           "rebellion_count": 0, "has_activity_data": False})
    councilors.sort(key=lambda c: -c["votes_total"])
    sessions.sort(key=lambda x: x["date"], reverse=True)
    kad = {"id": "2024-2029", "label": "IX kadencja (2024–2029)",
           "names_normalized": True, "clubs": {},
           "sessions": sessions, "total_sessions": len(sessions),
           "total_votes": len(votes_out), "total_councilors": len(all_names),
           "councilors": councilors, "votes": votes_out,
           "similarity_top": [], "similarity_bottom": [],
           "councilor_index": list(all_names)}
    (docs / "kadencja-2024-2029.json").write_text(
        json.dumps(kad, ensure_ascii=False, indent=1), encoding="utf-8")
    profiles = {"scraped_at": datetime.now(timezone.utc).isoformat(), "profiles": [],
                "total": len(councilors)}
    for c in councilors:
        profiles["profiles"].append({"name": c["name"], "slug": slugify(c["name"]),
                                     "club": "", "role": "", "photo_url": "", "bio": "",
                                     "email": "", "social_links": {},
                                     "kadencje": {"2024-2029": {
                                         "club": "", "frekwencja": c["frekwencja"],
                                         "aktywnosc": 0, "zgodnosc_z_klubem": None,
                                         "votes_za": c["votes_za"],
                                         "votes_przeciw": c["votes_przeciw"],
                                         "votes_wstrzymal": c["votes_wstrzymal"],
                                         "votes_brak": c["votes_brak"],
                                         "votes_nieobecny": c["votes_nieobecny"],
                                         "votes_total": c["votes_total"],
                                         "rebellion_count": 0, "rebellions": [],
                                         "has_voting_data": True,
                                         "has_activity_data": False, "former": False,
                                         "mid_term": False}}})
    (docs / "profiles.json").write_text(
        json.dumps(profiles, ensure_ascii=False, indent=1), encoding="utf-8")
    (docs / "data.json").write_text(json.dumps({
        "generated": datetime.now(timezone.utc).isoformat(),
        "default_kadencja": "2024-2029",
        "kadencje": [{"id": "2024-2029", "label": "IX kadencja (2024–2029)"}],
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[kkl] DONE: {len(sessions)} sesji, {len(votes_out)} głosów, {len(all_names)} radnych")
    if not votes_out:
        sys.exit(2)


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent)
