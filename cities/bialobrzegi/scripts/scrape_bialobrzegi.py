#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop Białobrzegi — Tier-2 ("model berliński"): skład rady + sesje, HAS_VOTING_DATA=false.

Białobrzegi (powiat białobrzeski, mazowieckie, TERYT 1401014, QID Q855976, pop 6615).

Źródła (live, IX kadencja 2024–2029), BIP bip.bialobrzegi.pl (BIP-E.PL):
- Roster: /bia/rada/sklad-osobowy/31367,...Kadencja-2024-2029.html — 15 radnych,
  role w liniach 'Przewodniczący Rady - Nazwisko Imię' (format NAZWISKO Imię — swap).
- Sesje: /bia/rada/sesje/porzadek-sesji-i-projekty-uchw — per-sesja link z datą
  w tytule/slugu 'XXVII SESJA ... IX KADENCJI - 17.08.2026 R.'. XXVII sesja 2026-08-17.
- Brak głosowań imiennych w KAŻDYM źródle (zweryfikowane 2026-09-01):
  bialobrzegi.esesja.pl = PM porzucony (1 sesja 2025-07-16, 2 głosy — archiwum),
  rada.bialobrzegi.pl (AlfaTV) = brak TLS/strony, bialobrzegi.bip.net.pl = 404,
  transmisje = posiedzenia.pl (WAF 403 dla API, wideo bez wykazów), protokoły BIP
  bez wyników imiennych.

Użycie: python scrape_bialobrzegi.py [--city-dir cities/bialobrzegi]
"""
import json
import re
import sys
import time
import unicodedata
from datetime import datetime
from pathlib import Path

import urllib.request
import ssl

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE
BASE = "https://bip.bialobrzegi.pl"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Radoskop/1.0 (info@radoskop.eu)"}

KAD = "2024-2029"
KAD_LABEL = "IX kadencja (2024–2029)"
KAD_START = "2024-05-07"

ROM = {}
_vals = [1, 5, 10, 50, 100, 500]
_syms = "ivxlcd"


def _roman(s):
    s = s.upper()
    out = 0
    prev = 0
    for ch in reversed(s):
        v = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}.get(ch)
        if v is None:
            return None
        if v < prev:
            out -= v
        else:
            out += v
            prev = v
    return out


# Fallback roster (zweryfikowany 2026-09-01 z /bia/rada/sklad-osobowy/31367; scraper preferuje live)
FALLBACK_ROSTER = [
    ("Marcin Paweł Osowski", "Przewodniczący Rady Miasta i Gminy"),
    ("Elżbieta Jolanta Kaczmarek", "Wiceprzewodniczący Rady Miasta i Gminy"),
    ("Zenon Jachowski", "Wiceprzewodniczący Rady Miasta i Gminy"),
    ("Sylwia Gurak", ""),
    ("Wioletta Małgorzata Gutowska", ""),
    ("Magdalena Jeżowska", ""),
    ("Monika Jodłowska", ""),
    ("Paweł Adam Kot", ""),
    ("Tadeusz Łukasiak", ""),
    ("Zofia Niezabitowska", ""),
    ("Mariusz Pawlak", ""),
    ("Ewelina Poziomecka", ""),
    ("Alina Teresa Witkowska", ""),
    ("Krzysztof Woźniak", ""),
    ("Marek Ziółek", ""),
]

_LAST = 0.0


def _rate(delay=0.8):
    global _LAST
    d = time.time() - _LAST
    if d < delay:
        time.sleep(delay - d)
    _LAST = time.time()


def fetch(url, timeout=30):
    _rate()
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
        raw = r.read()
        m = re.search(rb"charset=([\w-]+)", raw[:3000], re.I)
        enc = m.group(1).decode() if m else "utf-8"
        try:
            return raw.decode(enc, "replace")
        except Exception:
            return raw.decode("utf-8", "replace")


def parse_roster(t: str):
    """Lines 'Przewodniczący Rady - Osowski Marcin Paweł' / 'Radna - Gurak Sylwia'.
    Format NAZWISKO Imię(na) — swap do 'Imię Nazwisko'."""
    txt = re.sub(r"<[^>]+>", "\n", t)
    txt = txt.replace("&nbsp;", " ").replace("&#160;", " ")
    lines = [re.sub(r"\s+", " ", l).strip() for l in txt.split("\n")]
    roster, roles = [], {}
    pat = re.compile(r"^(Przewodniczący|Wiceprzewodnicz\w*|Radny|Radna)(?:\s+Rady[\w\sżł]*?)?\s*[-–:]?\s+(.+)$")
    for l in lines:
        m = pat.match(l)
        if not m:
            continue
        role_kw, names = m.group(1), m.group(2).strip()
        if not re.match(r"^[A-ZŁŚŻŹĆŃÓĄĘ][\wŁŚŻŹĆŃÓĄĘ\-]+(\s+[A-ZŁŚŻŹĆŃÓĄĘ][\wŁŚŻŹĆŃÓĄĘ\-]+){1,3}$", names):
            continue
        parts = names.split(" ")
        surname = parts[0]
        firsts = parts[1:]
        name = " ".join(firsts + [surname])
        role = ""
        if role_kw.startswith("Przewodnicz"):
            role = "Przewodniczący Rady Miasta i Gminy"
        elif role_kw.startswith("Wiceprzewodnicz"):
            role = "Wiceprzewodniczący Rady Miasta i Gminy"
        if name not in roster:
            roster.append(name)
            if role:
                roles[name] = role
    return roster, roles, len(roster) >= 13


def parse_sessions(t: str):
    """'XXVII SESJA RADY MIASTA I GMINY BIALOBRZEGI IX KADENCJI - 17.08.2026 R. GODZ. 15.00'"""
    sessions, seen = [], set()
    for m in re.finditer(r'href="(/bia/rada/sesje/porzadek[^"]+)"[^>]*>\s*([^<]+)', t):
        href, label = m.group(1), re.sub(r"\s+", " ", m.group(2)).strip()
        if "IX KADENCJI" not in label.upper():
            continue
        dm = re.search(r"-\s*(\d{1,2})\.(\d{1,2})\.(\d{4})", label)
        rm = re.match(r"^([IVXLCDM]+)", label.upper())
        if not dm or not rm:
            continue
        iso = f"{dm.group(3)}-{dm.group(2).zfill(2)}-{dm.group(1).zfill(2)}"
        if iso < KAD_START:
            continue
        num = _roman(rm.group(1))
        if num is None or num in seen:
            continue
        seen.add(num)
        sessions.append({
            "date": iso, "number": rm.group(1),
            "label": f"Sesja {rm.group(1)} ({iso})",
            "url": BASE + href,
            "vote_count": 0,
        })
    sessions.sort(key=lambda s: s["date"], reverse=True)
    return sessions


def slugify(name: str) -> str:
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c)).replace("ł", "l")
    s = re.sub(r"[^a-z0-9]+", "", s.lower())
    return s or "radny"


def build(city_dir: Path) -> int:
    cfg = json.loads((city_dir / "config.json").read_text(encoding="utf-8"))
    docs = city_dir / "docs"
    docs.mkdir(parents=True, exist_ok=True)

    roster, roles, ok = [], {}, False
    try:
        t = fetch(BASE + "/bia/rada/sklad-osobowy/31367,Sklad-osobowy-Rady-Miasta-i-Gminy-Bialobrzegi-Kadencja-2024-2029.html")
        roster, roles, ok = parse_roster(t)
    except Exception as e:
        print(f"  [warn] roster live failed: {e}")
    if not ok:
        print("  [info] roster live niepełny — fallback (zweryfikowany 2026-09-01)")
        roster = [n for n, _ in FALLBACK_ROSTER]
        roles = {n: r for n, r in FALLBACK_ROSTER if r}

    councilors = []
    for n in roster:
        councilors.append({
            "name": n, "club": "",
            "role": roles.get(n) or "Radny/Radna",
            "district": None, "frekwencja": None, "aktywnosc": None,
            "zgodnosc_z_klubem": None, "votes_total": 0, "rebellion_count": 0,
            "has_activity_data": False,
        })

    sessions = []
    try:
        sessions = parse_sessions(fetch(BASE + "/bia/rada/sesje/porzadek-sesji-i-projekty-uchw"))
    except Exception as e:
        print(f"  [warn] sesje failed: {e}")
    if not sessions:
        print("  [error] 0 sessions parsed — abort (nie fabrykuję)")
        return 2

    kadencja = {
        "id": KAD, "label": KAD_LABEL, "clubs": {},
        "sessions": sessions,
        "total_sessions": len(sessions), "total_votes": 0,
        "total_councilors": len(councilors),
        "councilors": councilors,
        "votes": [], "similarity_top": [], "similarity_bottom": [],
    }
    (docs / f"kadencja-{KAD}.json").write_text(
        json.dumps(kadencja, ensure_ascii=False, indent=1), encoding="utf-8")

    profiles = {"scraped_at": datetime.now().isoformat(), "profiles": [], "total": len(councilors)}
    for c in councilors:
        profiles["profiles"].append({
            "name": c["name"], "slug": slugify(c["name"]),
            "kadencje": {
                KAD: {
                    "club": "", "role": c["role"], "has_voting_data": False,
                    "has_activity_data": False, "former": False, "mid_term": False,
                    "frekwencja": None, "aktywnosc": None, "zgodnosc_z_klubem": None,
                    "votes_total": 0, "rebellion_count": 0,
                }
            },
        })
    (docs / "profiles.json").write_text(
        json.dumps(profiles, ensure_ascii=False, indent=1), encoding="utf-8")

    data = {"generated": datetime.now().isoformat(), "default_kadencja": KAD,
            "kadencje": [{"id": KAD, "label": KAD_LABEL}]}
    (docs / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    (docs / "config.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"  bialobrzegi: roster={len(councilors)} sessions={len(sessions)} (club_assignments PENDING)")
    return 0


if __name__ == "__main__":
    if "--city-dir" in sys.argv:
        city_dir = Path(sys.argv[sys.argv.index("--city-dir") + 1])
    else:
        city_dir = Path(__file__).resolve().parent.parent
    raise SystemExit(build(city_dir))
