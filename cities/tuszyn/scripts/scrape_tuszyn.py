#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop Tuszyn — Tier-2 ("model berliński"): skład rady + sesje, HAS_VOTING_DATA=false.

Tuszyn (powiat łódzki wschodni, łódzkie, SIMC 1006114, QID Q403941, pop 7221).

Źródła (live, IX kadencja 2024–2029):
- Roster: https://tuszyn.info.pl/artykul/sklad-rady-1 (BIP WART) —
  RADA MIEJSKA W TUSZYNIE: Przewodniczący Mirosław Popecki, Wiceprzewodniczący
  Iwona Parczewska + Tomasz Sobolewski, Radni (13): Barbara Brych, Agata Kłos,
  Anna Krajewska-Lesiak, Kazimierz Sęk, Łukasz Wójcik, Piotr Zarzycki,
  Michał Ścibor, Kamil Pokora, Janusz Miara, Krzysztof Krajewski,
  Włodzimierz Janiczek, Juliusz Defeciński (14 osób łącznie = 15 radnych, 1 wakat).
- Sesje: /artykul/zaproszenia-na-sesje-rady-miejskiej-w-tuszynie-1 — per-sesja
  wiersze 'Zaproszenie na [N] sesję (nadzwyczajną) Rady Miejskiej w Tuszynie,
  która odbędzie się w dniu DD miesięca RRRR'. XXXII sesja 2026-08-28.
- Brak głosowań imiennych w KAŻDYM źródle (zweryfikowane 2026-08-31):
  transmicje wideo bez wykazów, uchwały bez wyników per-radny, brak kategorii
  protokołów imiennych — tuszyn.esesja.pl = wildcard, bip.gov.pl SSDIP pusto,
  bip.net.pl 404, archiwum.tuszyn.pl NXDOMAIN, bip.wart.com.pl NXDOMAIN.

Użycie: python scrape_tuszyn.py [--city-dir cities/tuszyn]
"""
import json
import re
import sys
import time
import unicodedata
from datetime import datetime
from pathlib import Path

import urllib.request
import urllib.error
import ssl

CTX = ssl.create_default_context(); CTX.check_hostname = False; CTX.verify_mode = ssl.CERT_NONE
BASE = "https://tuszyn.info.pl"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Radoskop/1.0 (info@radoskop.eu)"}

KAD = "2024-2029"
KAD_LABEL = "IX kadencja (2024–2029)"
KAD_START = "2024-05-07"

MON = {"stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5, "czerwca": 6,
       "lipca": 7, "sierpnia": 8, "września": 9, "wrzesnia": 9, "października": 10,
       "pazdziernika": 10, "listopada": 11, "grudnia": 12}
ROM = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6, "VII": 7, "VIII": 8,
       "IX": 9, "X": 10, "XI": 11, "XII": 12, "XIII": 13, "XIV": 14, "XV": 15,
       "XVI": 16, "XVII": 17, "XVIII": 18, "XIX": 19, "XX": 20, "XXI": 21, "XXII": 22,
       "XXIII": 23, "XXIV": 24, "XXV": 25, "XXVI": 26, "XXVII": 27, "XXVIII": 28,
       "XXIX": 29, "XXX": 30, "XXXI": 31, "XXXII": 32, "XXXIII": 33, "XXXIV": 34,
       "XXXV": 35, "XXXVI": 36, "XXXVII": 37, "XXXVIII": 38, "XXXIX": 39, "XL": 40}

# Fallback roster (zweryfikowany 2026-08-31 z /artykul/sklad-rady-1; scraper preferuje live)
FALLBACK_ROSTER = [
    ("Mirosław Popecki", "Przewodniczący Rady Miejskiej"),
    ("Iwona Parczewska", "Wiceprzewodniczący Rady Miejskiej"),
    ("Tomasz Sobolewski", "Wiceprzewodniczący Rady Miejskiej"),
    ("Barbara Brych", ""),
    ("Agata Kłos", ""),
    ("Anna Krajewska-Lesiak", ""),
    ("Kazimierz Sęk", ""),
    ("Łukasz Wójcik", ""),
    ("Piotr Zarzycki", ""),
    ("Michał Ścibor", ""),
    ("Kamil Pokora", ""),
    ("Janusz Miara", ""),
    ("Krzysztof Krajewski", ""),
    ("Włodzimierz Janiczek", ""),
    ("Juliusz Defeciński", ""),
]

_LAST = 0.0


def _rate(delay=0.8):
    global _LAST
    d = time.time() - _LAST
    if d < delay:
        time.sleep(delay - d)
    _LAST = time.time()


def fetch(url, timeout=30):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
        raw = r.read()
        try:
            return raw.decode("utf-8")
        except Exception:
            return raw.decode("cp1250", errors="replace")


def bodytext(t):
    t = re.sub(r"<script.*?</script>|<style.*?</style>", " ", t, flags=re.S | re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    t = re.sub(r"(&nbsp;|&#160;)", " ", t)
    t = t.replace("&oacute;", "ó").replace("&amp;", "&")
    t = re.sub(r"\s+", " ", t)
    return t.strip()


def norm_name(name: str) -> str:
    n = re.sub(r"\s+", " ", name).strip(" .:–-")
    return n


def _unesc(s: str) -> str:
    import html as _h
    s = s.replace("&oacute;", "ó").replace("&Oacute;", "Ó")
    s = s.replace("&nbsp;", " ").replace("&amp;", "&")
    return _h.unescape(s)


def parse_roster(t: str):
    """15 radnych + role z gov.pl rada-miejska."""
    # tresc container
    m = re.search(r'id="tresc[^"]*"[^>]*>(.*?)(?:<div class="|<!-- METADANE|<section)', t, re.S)
    seg = m.group(1) if m else t
    seg = seg.split("<!-- METADANE")[0]
    paras = []
    for p in re.findall(r"<p[^>]*>(.*?)</p>", seg, re.S):
        x = re.sub(r"<[^>]+>", " ", p)
        x = _unesc(x)
        x = re.sub(r"\s+", " ", x).strip()
        if x:
            paras.append(x)
    roster, roles = [], {}
    cur_role = ""
    for p in paras:
        if re.search(r"RADA MIEJSKA", p, re.I):
            continue
        if re.match(r"^Przewodnicz", p, re.I):
            cur_role = "Przewodniczący Rady Miejskiej"
            continue
        if re.match(r"^Wiceprzewodnicz", p, re.I):
            cur_role = "Wiceprzewodniczący Rady Miejskiej"
            continue
        if re.match(r"^Radni", p, re.I):
            cur_role = ""
            continue
        nm = norm_name(p)
        if not nm or len(nm) < 4:
            continue
        if re.match(r"^[A-ZŁŚŻŹÓ][\w\-]+(?:\s+[A-ZŁŚŻŹÓ][\w\-]+)+$", nm) and nm not in roster and len(roster) < 16:
            roster.append(nm)
            if cur_role:
                roles[nm] = cur_role
    return roster, roles, len(roster) >= 12


def parse_sessions(t: str) -> list:
    """Sesje z /zaproszenia... — per wiersz '... na <roman> sesję (nadzwyczajną)
    Rady Miejskiej w Tuszynie, która odbędzie się w dniu DD <month> RRRR'."""
    sessions, seen = [], set()
    txt = bodytext(t)
    # rows: <tr><td>Zaproszenie na ... sesję ... w dniu DD m RRRR ...</td><td>DATA-PUB</td></tr>
    for m in re.finditer(
            r"Zaproszenie\s+na\s+([IVXLCDM]+)\s+sesj[ęy]\s+(nadzwyczajn\w*\s+)?(?:Rady Miejskiej w Tuszynie[^\.]{0,160}?)"
            r"w dniu\s+(\d{1,2})\s+(stycznia|lutego|marca|kwietnia|maja|czerwca|lipca|sierpnia|września|października|listopada|grudnia)\s+(\d{4})",
            txt, re.I):
        rom, nad, dd, mn, yy = m.group(1), m.group(2) or "", m.group(3), m.group(4), m.group(5)
        if rom not in ROM:
            continue
        iso = f"{yy}-{MON[mn]:02d}-{int(dd):02d}"
        if iso < KAD_START:
            continue
        key = rom
        if key in seen:
            continue
        seen.add(key)
        sessions.append({
            "date": iso, "number": rom,
            "label": f"Sesja {rom}{(' nadzwyczajna' if nad else '')} ({iso})",
            "url": f"{BASE}/artykul/zaproszenia-na-sesje-rady-miejskiej-w-tuszynie-1",
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
        t = fetch(f"{BASE}/artykul/sklad-rady-1")
        roster, roles, ok = parse_roster(t)
    except Exception as e:
        print(f"  [warn] roster live failed: {e}")
    if not ok:
        print("  [info] roster live niepełny — fallback (zweryfikowany 2026-08-31)")
        roster = [n for n, _ in FALLBACK_ROSTER]
        roles = {n: r for n, r in FALLBACK_ROSTER if r}
    roster = [norm_name(n) for n in roster]

    councilors = []
    for n in roster:
        role = roles.get(n) or ""
        councilors.append({
            "name": n, "club": "",
            "role": role or "Radny/Radna",
            "district": None, "frekwencja": None, "aktywnosc": None,
            "zgodnosc_z_klubem": None, "votes_total": 0, "rebellion_count": 0,
            "has_activity_data": False,
        })

    sessions: list = []
    try:
        sessions = parse_sessions(fetch(f"{BASE}/artykul/zaproszenia-na-sesje-rady-miejskiej-w-tuszynie-1"))
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
    print(f"  tuszyn: roster={len(councilors)} sessions={len(sessions)} (club_assignments PENDING)")
    return 0


if __name__ == "__main__":
    if "--city-dir" in sys.argv:
        city_dir = Path(sys.argv[sys.argv.index("--city-dir") + 1])
    else:
        city_dir = Path(__file__).resolve().parent.parent
    raise SystemExit(build(city_dir))
