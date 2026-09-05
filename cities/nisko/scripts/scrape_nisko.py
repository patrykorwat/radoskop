#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop Nisko — Tier-2 ("model berliński"): skład rady + sesje, HAS_VOTING_DATA=false.

Nisko (powiat niżański, podkarpackie, TERYT 1812054, QID Q1172666).

Źródła (live, IX kadencja 2024–2029):
- Roster: www.nisko.pl/samorzad/rada-miejska-w-nisku — 21 radnych, sekcja
  "Skład Rady Miejskiej w Nisku", format NAZWISKO Imię (swap), role z linii
  "Przewodniczący:" / "Wiceprzewodniczący:" przed nazwiskiem.
- Sesje: bip.nisko.pl/organy/1081/dokumenty/3804/lista/N (Protokoły Sesji RM) —
  tytuły "Protokół z XVIII sesji Rady Miejskiej w Nisku, która odbyła się dnia
  13 sierpnia 2025...". Najnowszy protokół: XVIII 2025-08-13.
- Brak głosowań imiennych w KAŻDYM źródle (zweryfikowane 2026-09-05):
  nisko.esesja.pl = wildcard DNS (strona korporacyjna eSesja), rada.nisko.pl =
  brak DNS, nisko.bip.net.pl = brak, bip.nisko.pl protokoły bez tabel ZA/PRZECIW
  per-radny, www.nisko.pl/sesje/ = indeks 403, załączniki sesji to uchwały/
  protokoły zbiorcze (brak wykazów imiennych).

Użycie: python scrape_nisko.py [--city-dir cities/nisko]
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
ROSTER_URL = "https://www.nisko.pl/samorzad/rada-miejska-w-nisku"
PROTO_BASE = "https://bip.nisko.pl/organy/1081/dokumenty/3804/lista/"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15"}

KAD = "2024-2029"
KAD_LABEL = "IX kadencja (2024–2029)"
KAD_START = "2024-05-07"

PL_MONTHS = {'stycznia': 1, 'lutego': 2, 'marca': 3, 'kwietnia': 4, 'maja': 5,
             'czerwca': 6, 'lipca': 7, 'sierpnia': 8, 'września': 9,
             'października': 10, 'listopada': 11, 'grudnia': 12}

# Fallback roster (zweryfikowany 2026-09-05 z www.nisko.pl; scraper preferuje live)
FALLBACK_ROSTER = [
    ("Paweł Tofil", "Przewodniczący Rady Miejskiej"),
    ("Tadeusz Błażejczak", "Wiceprzewodniczący Rady Miejskiej"),
    ("Tadeusz Wolak", "Wiceprzewodniczący Rady Miejskiej"),
    ("Grzegorz Bednarz", ""),
    ("Grzegorz Błądek", ""),
    ("Łukasz Chwiej", ""),
    ("Marcin Folta", ""),
    ("Krzysztof Klimka", ""),
    ("Zbigniew Kotuła", ""),
    ("Wiktoria Król", ""),
    ("Maria Lechocińska", ""),
    ("Adam Madej", ""),
    ("Marek Pachla", ""),
    ("Piotr Pachla", ""),
    ("Barbara Potocka", ""),
    ("Bogdan Rodzeń", ""),
    ("Ewa Surowiak", ""),
    ("Waldemar Ślusarczyk", ""),
    ("Krzysztof Tabian", ""),
    ("Eugeniusz Trzuskot", ""),
    ("Edyta Waszkiewicz", ""),
]

# Fallback sessions IX kad. (zweryfikowany 2026-09-05 z protokołów BIP)
FALLBACK_SESSIONS = [
    ("XVIII", "2025-08-13"), ("XVII", "2025-06-23"), ("XVI", "2025-06-17"),
    ("XV", "2025-05-20"), ("XIV", "2025-04-22"), ("XIII", "2025-03-26"),
    ("XII", "2025-01-20"), ("XI", "2024-12-30"), ("X", "2024-12-17"),
    ("IX", "2024-11-27"), ("V", "2024-07-12"),
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


def _roman(s):
    out, prev = 0, 0
    for ch in reversed(s.upper()):
        v = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100}.get(ch)
        if v is None:
            return None
        if v < prev:
            out -= v
        else:
            out += v
            prev = v
    return out


NAME_RE = (r"^[A-Z\u0141\u015a\u0179\u017b\u0106\u0143\u00d3\u0104\u0118]"
           r"[a-z\u0142\u015b\u017a\u017c\u0107\u0144\u00f3\u0105\u0119]+"
           r"(?:-[A-Z\u0141\u015a\u0179\u017b\u0106\u0143\u00d3\u0104\u0118]"
           r"[a-z\u0142\u015b\u017a\u017c\u0107\u0144\u00f3\u0105\u0119]+)?"
           r"(?:\s+[A-Z\u0141\u015a\u0179\u017b\u0106\u0143\u00d3\u0104\u0118]"
           r"[a-z\u0142\u015b\u017a\u017c\u0107\u0144\u00f3\u0105\u0119'-]+){1,2}$")


def parse_roster(t: str):
    body = re.sub(r"<(script|style).*?</\1>", "", t, flags=re.S)
    txt = re.sub(r"<[^>]+>", "\n", body).replace("&nbsp;", " ")
    lines = [re.sub(r"\s+", " ", l).strip() for l in txt.splitlines() if l.strip()]
    roster, roles = [], {}
    _seen_keys = set()
    pending_role = ""
    started = False
    pat = re.compile(NAME_RE)
    skip = re.compile(r"Rada|Miasta|Partnerskie|Biblioteka|Ośrodek|Dom Samopomocy|Klub|Herb|Biuro|Fundusze|Mapa|adres pocztowy")
    for l in lines:
        if l.startswith("Skład Rady Miejskiej"):
            started = True
            continue
        if not started:
            continue
        if l.startswith("Przewodniczący:"):
            pending_role = "Przewodniczący Rady Miejskiej"
            continue
        if l.startswith("Wiceprzewodniczący:"):
            pending_role = "Wiceprzewodniczący Rady Miejskiej"
            continue
        if skip.search(l):
            continue
        if pat.match(l):
            parts = l.split(" ")
            name = " ".join(parts[1:] + [parts[0]])  # NAZWISKO Imię -> Imię NAZWISKO
            key = frozenset(parts)  # dedupe niezależnie od szyku (strona podaje 2 szyki)
            if key not in _seen_keys:
                _seen_keys.add(key)
                roster.append(name)
                if pending_role:
                    roles[name] = pending_role
                    pending_role = ""
        if len(roster) >= 30:
            break
    return roster, roles, len(roster) >= 15


def parse_sessions():
    """Sesje IX kad. z DWÓCH katalogów BIP: Protokoły Sesji RM (3804, titles
    'Protokół z XVIII sesji ... dnia 13 sierpnia 2025') + Zaproszenia na Sesje RM
    (3805, 'Porządek obrad XXIX Sesji ... dnia 16 czerwca 2026 r.') — protokoły są
    opóźnione, zaproszenia aktualne do bieżącego miesiąca."""
    sessions, seen = {}, set()
    for cat in ("3804", "3805"):
        for pg in range(1, 6):
            try:
                t = fetch(PROTO_BASE.replace("3804", cat) + str(pg))
            except Exception:
                break
            t2 = re.sub(r"<[^>]+>", " ", re.sub(r"<(script|style).*?</\1>", "", t, flags=re.S))
            found = 0
            pats = [
                r"Protok[óo][łl] z ([IVXLCDM]+) sesji[^<\n]{0,90}?(\d{1,2})\s+([a-z]+)\.?\s*(\d{4})",
                r"([IVXLCDM]+)(?:\s+nadzwyczajnej)?\s*[Ss]esji[^.]{0,120}?(\d{1,2})\s+([a-z]+)\s+(20[2-3]\d)\b",
            ]
            for pat in pats:
                for m in re.finditer(pat, t2):
                    roman, d, mon, y = m.group(1), int(m.group(2)), m.group(3).lower().rstrip("."), m.group(4)
                    if mon not in PL_MONTHS:
                        continue
                    iso = f"{y}-{PL_MONTHS[mon]:02d}-{d:02d}"
                    if iso < KAD_START or roman in seen:
                        continue
                    seen.add(roman)
                    sessions[roman] = {"date": iso, "number": roman,
                                       "label": f"Sesja {roman} ({iso})",
                                       "url": PROTO_BASE + "1", "vote_count": 0}
                    found += 1
            if found == 0 and pg > 1:
                break
    out = sorted(sessions.values(), key=lambda s: s["date"], reverse=True)
    return out


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
        roster, roles, ok = parse_roster(fetch(ROSTER_URL))
    except Exception as e:
        print(f"  [warn] roster live failed: {e}")
    if not ok:
        print("  [info] roster live niepełny — fallback (zweryfikowany 2026-09-05)")
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
        sessions = parse_sessions()
    except Exception as e:
        print(f"  [warn] sesje failed: {e}")
    if not sessions:
        print("  [info] sesje live puste — fallback (zweryfikowany 2026-09-05)")
        sessions = [{"date": iso, "number": roman, "label": f"Sesja {roman} ({iso})",
                     "url": PROTO_BASE + "1", "vote_count": 0}
                    for roman, iso in FALLBACK_SESSIONS]

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
    print(f"  nisko: roster={len(councilors)} sessions={len(sessions)} (club_assignments PENDING)")
    return 0


if __name__ == "__main__":
    if "--city-dir" in sys.argv:
        city_dir = Path(sys.argv[sys.argv.index("--city-dir") + 1])
    else:
        city_dir = Path(__file__).resolve().parent.parent
    raise SystemExit(build(city_dir))
