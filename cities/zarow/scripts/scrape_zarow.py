#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop Żarów — Tier-2 (model berliński): skład rady + kalendarz sesji, BEZ imiennych.

Źródła:
  * Skład IX kadencji: BIP MegaBIP bip.um.zarow.pl /kadencja-2024-2029/873
    (nagłówek 'Skład Rady Miejskiej w Żarowie ... (2024 - 2029)', lista 'N. Imię Nazwisko',
     + Przewodniczący / Wiceprzewodniczący).
  * Kalendarz sesji IX: esesja.tv /transmisje_z_obrad/2142/rada-miejska-w-zarowie.htm
    (linki /transmisja/<id>/...-sesja-rady-miejskiej-w-zarowie-w-dniu-DD-mies-YYYY...htm).
Głosowania imienne: NIE PUBLIKOWANE — protokoły sesji w BIP (kategoria /809 'Uchwały')
są za wrapperem MegaBIP bez pobieralnego pliku (zalacznik/* zwraca stronę HTML), brak
kategorii 'wykaz głosowań' / wyników imiennych; eSesja.pl PM martwa (0 sesji).

Użycie: python scrape_zarow.py --city-dir <cities/zarow> [--cache-dir dir]
Zapisuje: docs/data.json, docs/kadencja-2024-2029.json, docs/profiles.json
"""
import argparse
import hashlib
import json
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import requests

BASE_BIP = "https://bip.um.zarow.pl"
ROSTER_URL = BASE_BIP + "/kadencja-2024-2029/873"
SESSIONS_URL = "https://esesja.tv/transmisje_z_obrad/2142/rada-miejska-w-zarowie.htm"
KAD_START = "2024-05-07"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Radoskop/1.0 (info@radoskop.eu)"}
REQ_DELAY = 0.5
_LAST = 0.0

_MONTHS = {"stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5, "czerwca": 6,
           "lipca": 7, "sierpnia": 8, "września": 9, "wrzesnia": 9, "pazdziernika": 10,
           "października": 10, "listopada": 11, "grudnia": 12}
_ROM = {}
for _v, _r in [(1, "I"), (2, "II"), (3, "III"), (4, "IV"), (5, "V"), (6, "VI"), (7, "VII"),
               (8, "VIII"), (9, "IX"), (10, "X"), (11, "XI"), (12, "XII"), (13, "XIII"),
               (14, "XIV"), (15, "XV"), (16, "XVI"), (17, "XVII"), (18, "XVIII"), (19, "XIX"),
               (20, "XX"), (21, "XXI"), (22, "XXII"), (23, "XXIII"), (24, "XXIV"),
               (25, "XXV"), (26, "XXVI"), (27, "XXVII"), (28, "XXVIII"), (29, "XXIX"),
               (30, "XXX"), (31, "XXXI"), (32, "XXXII"), (33, "XXXIII")]:
    _ROM[_r] = _v


def _rate():
    global _LAST
    now = time.time()
    if now - _LAST < REQ_DELAY:
        time.sleep(REQ_DELAY - (now - _LAST))
    _LAST = time.time()


def _fetch(url, cache=None):
    cf = None
    if cache is not None:
        Path(cache).mkdir(parents=True, exist_ok=True)
        cf = Path(cache) / (hashlib.md5(url.encode()).hexdigest() + ".html")
        if cf.is_file():
            return cf.read_text(encoding="utf-8", errors="ignore")
    _rate()
    for attempt in range(4):
        try:
            r = requests.get(url, headers=HEADERS, timeout=60)
            r.raise_for_status()
            break
        except Exception:
            if attempt == 3:
                raise
            time.sleep(2 + 2 * attempt)
    txt = r.content.decode("utf-8", "ignore")
    if cf is not None:
        cf.write_text(txt, encoding="utf-8")
    return txt


def _html_lines(h):
    t = re.sub(r"<script.*?</script>|<style.*?</style>", "", h, flags=re.S)
    t = re.sub(r"<[^>]+>", "\n", t)
    import html as H
    t = H.unescape(t)
    return [re.sub(r"\s+", " ", l.replace("\xa0", " ")).strip() for l in t.splitlines() if l.strip()]


def scrape_roster():
    h = _fetch(ROSTER_URL)
    lines = _html_lines(h)
    # anchor: list of 'N. Imię Nazwisko' after 'Skład Rady' heading with (2024
    roster = {}
    role = {}
    przewod, wice = None, []
    i = next((k for k, l in enumerate(lines) if re.search(r"Sk\u0142ad Rady.*2024", l, re.I)), None)
    if i is None:
        raise RuntimeError("roster anchor not found")
    zone = lines[i:i + 120]
    for j, l in enumerate(zone):
        if l == "Przewodniczący" and j + 2 < len(zone):
            nm = re.sub(r"\s+", " ", zone[j + 2]).strip()
            if re.match(r"^[A-ZŁŚŻŹĆŃÓĄĘ][a-złśżźćńóąę]+ [A-ZŁŚŻŹĆŃÓĄĘ]", nm):
                przewod = nm
        if l.startswith("Wiceprzewodniczący") and j + 2 < len(zone):
            nm = re.sub(r"\s+", " ", zone[j + 2]).strip()
            if re.match(r"^[A-ZŁŚŻŹĆŃÓĄĘ][a-złśżźćńóąę]+ [A-ZŁŚŻŹĆŃÓĄĘ]", nm):
                wice.append(nm)
        m = re.match(r"^\d{1,2}\.\s+([A-ZŁŚŻŹĆŃÓĄĘ][a-złśżźćńóąę]+(?:[-'’][a-złśżźćńóąę]+)?(?: [A-ZŁŚŻŹĆŃÓĄĘ][a-złśżźćńóąę]+(?:[-'’][a-złśżźćńóąę]+)?)*)$", l)
        if m:
            roster[m.group(1)] = ""
    if przewod:
        role[przewod] = "Przewodniczący Rady Miejskiej"
    for w in wice:
        role.setdefault(w, "Wiceprzewodniczący Rady Miejskiej")
    return roster, role


def scrape_sessions():
    h = _fetch(SESSIONS_URL)
    seen = {}
    for m in re.finditer(r'href="(/transmisja/(\d+)/([a-zóżźśćńł\-\d]+)\.htm)"[^>]*>([^<]+)<', h):
        mm = re.match(r"\s*([IVXLCDM]+)", m.group(4).strip().upper())
        rom = _ROM.get(mm.group(1)) if mm else None
        dm = re.search(r"w[-]dniu[-](\d{1,2})[-]([a-zó-ż]+)[-](\d{4})", m.group(3))
        if not dm:
            continue
        month = _MONTHS.get(dm.group(2).lower())
        if not month:
            continue
        date = f"{dm.group(3)}-{month:02d}-{int(dm.group(1)):02d}"
        title = re.sub(r"\s+", " ", m.group(4)).strip()
        if date < KAD_START:
            continue
        seen.setdefault(date, {"num": rom, "date": date, "title": title})
    return sorted(seen.values(), key=lambda x: x["date"])


def slugify(name):
    s = unicodedata.normalize("NFKD", name.lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--city-dir", required=True)
    ap.add_argument("--cache-dir", default=None)
    args = ap.parse_args()
    city_dir = Path(args.city_dir)
    cache = Path(args.cache_dir) if args.cache_dir else None
    docs = city_dir / "docs"
    docs.mkdir(parents=True, exist_ok=True)

    roster, role = scrape_roster()
    sessions = scrape_sessions()
    print(f"[zarow] roster: {len(roster)}, sessions IX: {len(sessions)}")
    if len(roster) < 10 or len(sessions) < 5:
        raise SystemExit("[zarow] too little data — aborting")

    names = sorted(set(roster) | set(role))
    councilors = [{"name": n, "slug": slugify(n), "club": "", "role": role.get(n, ""),
                   "frekwencja": None, "aktywnosc": None, "votes": 0,
                   "zgodnosc_z_izba": None} for n in names]
    sess_list = [{"id": f"sesja-{s['num'] or s['date']}", "number": str(s["num"] or ""),
                  "date": s["date"], "label": s["title"], "vote_count": 0} for s in sessions]

    kad = {"id": KADENCJA_ID, "label": KADENCJA_LABEL,
           "sessions": sess_list, "votes": [],
           "councilor_index": names, "councilors": councilors,
           "total_councilors": len(names), "total_votes": 0,
           "total_sessions": len(sess_list),
           "similarity_top": [], "similarity_bottom": []}
    (docs / f"kadencja-{KADENCJA_ID}.json").write_text(json.dumps(kad, ensure_ascii=False, indent=1), encoding="utf-8")

    profiles = [{"name": c["name"], "slug": c["slug"], "club": "", "role": c["role"],
                 "photo_url": "", "bio": "", "email": "", "social_links": {}, "voting": None,
                 "kadencje": {KADENCJA_ID: {"club": "", "has_voting_data": False,
                                             "role": c["role"], "frekwencja": 0.0,
                                             "aktywnosc": 0.0, "zgodnosc_z_klubem": None,
                                             "zgodnosc_z_izba": None, "rebellion_count": 0}}}
                for c in councilors]
    (docs / "profiles.json").write_text(json.dumps(
        {"scraped_at": datetime.now(timezone.utc).isoformat(), "profiles": profiles,
         "total": len(profiles)}, ensure_ascii=False, indent=1), encoding="utf-8")

    data = {"city": "Żarów", "rada": "Rada Miejska w Żarowie",
            "kadencja_active": KADENCJA_ID,
            "kadencje": [{"id": KADENCJA_ID, "label": KADENCJA_LABEL}],
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "stats": {"total_votes": 0, "total_sessions": len(sess_list),
                      "total_councilors": len(names)},
            "source": {"bip": BASE_BIP, "type": "Tier-2: sklad (BIP MegaBIP) + kalendarz sesji (esesja.tv)"}}
    (docs / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[zarow] DONE Tier-2: {len(sess_list)} sesji, 0 glosowan, {len(names)} radnych")


if __name__ == "__main__":
    main()
