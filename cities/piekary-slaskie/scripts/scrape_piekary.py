#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop Piekary Śląskie — Tier-2 (roster / "model berliński") scraper.

Brak głosowań imiennych (esesja wildcard, Nefeni bez per-radny głosów) —
strona pokazuje SKŁAD RADY (roster) + KALENDARZ SESJI IX kadencji.

Źródło: Nefeni BIP https://piekaryslaskie.bip.net.pl (Next.js "Nefeni"):
  - skład rady: /kategorie/302-sklad-i-pelnione-funkcje  (art. 1835-1857 radny)
  - kluby: na stronie każdego radnego ("Przynależność do Klubu ...")
  - sesje: /kategorie/311-protokoly-sesji-rady-miasta (title z datą w slug)

has_voting_data:false, has_speaker_activity:false.
"""

import json
import re
import sys
import unicodedata
import urllib.request
import ssl
from pathlib import Path

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE

BASE = "https://piekaryslaskie.bip.net.pl"
ROSTER_CAT = "/kategorie/302-sklad-i-pelnione-funkcje"
SESS_CAT = "/kategorie/311-protokoly-sesji-rady-miasta"
IX_START = "2024-05-07"

_MON = {'stycznia': 1, 'lutego': 2, 'marca': 3, 'kwietnia': 4, 'maja': 5,
        'czerwca': 6, 'lipca': 7, 'sierpnia': 8, 'września': 9, 'października': 10,
        'listopada': 11, 'grudnia': 12,
        'wrzesnia': 9, 'pazdziernika': 10}


def _http(url):
    import time
    last = None
    for _ in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 Radoskop/1.0"})
            with urllib.request.urlopen(req, timeout=45, context=_CTX) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2)
    raise last


def _slug(name: str) -> str:
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace("ł", "l")
    return re.sub(r"[^a-z0-9]+", "", s) or "radny"


def find_radny_articles():
    hrefs = set()
    for pg in ("", "?page=1", "?page=2"):
        html = _http(BASE + ROSTER_CAT + pg)
        for h in re.findall(r'href=["\'](/kategorie/302[^"\']*/artykuly/18\d\d-[^"\']*)["\']', html):
            hrefs.add(h.split("?")[0])
    return sorted(hrefs)


def parse_radny(href):
    raw = _http(BASE + href)
    body = re.sub(r"<[^>]+>", " ", raw)
    body = re.sub(r"\s+", " ", body)
    m = re.search(r"Radn[ay]\s+([A-ZŻŁŚÓŃ][a-zążśłóćńę-]+)\s+([A-ZŻŁŚÓŃ][a-zążśłóćńę-]+)", body)
    name = None
    if m:
        name = f"{m.group(1)} {m.group(2)}"
    if not name:
        return (None, "")
    name = name.rstrip("-").strip()
    # kluby: znane nazwy klubów z kategorii 303 (keyword match w ciele)
    low = body.lower()
    club = ""
    if "prawa i sprawiedliwości" in low:
        club = "PiS"
    elif "twój głos" in low or "twoj glos" in low:
        club = "TGL"
    elif "obywatelski" in low:
        club = "OKR"
    return (name, club)


def find_sessions():
    dates = {}
    for pg in ("", "?page=1", "?page=2", "?page=3", "?page=4"):
        html = _http(BASE + SESS_CAT + pg)
        for m in re.finditer(r"artykuly/(\d+)-protokol-nr-[\w]+-sesji[^\"]*-(\d{1,2})-(\w+)-(\d{4})-r", html):
            aid, day, monw, yr = m.group(1), m.group(2), m.group(3).lower(), m.group(4)
            mon = _MON.get(monw)
            if not mon:
                continue
            date = f"{yr}-{mon:02d}-{int(day):02d}"
            if date >= IX_START:
                dates[aid] = date
    return sorted(dates.values())


def build(city_dir) -> int:
    cfg_path = city_dir / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    kad = cfg["kadencja_active"]
    docs = city_dir / "docs"
    docs.mkdir(parents=True, exist_ok=True)

    assignments = cfg.get("club_assignments", {}) or {}
    clubs = cfg.get("clubs", {}) or {}
    names = sorted(assignments.keys(), key=lambda n: n.split()[-1])
    sessions = find_sessions()
    print(f"  sessions: {len(sessions)}  councilors: {len(names)}")
    if not names:
        print("  [warn] pusty roster — brak club_assignments w config.json")
        return 1

    kadencja = {
        "id": kad, "label": cfg["kadencje"][kad]["label"],
        "clubs": {k: v.get("name", k) for k, v in clubs.items()},
        "sessions": [{"date": s, "number": "", "vote_count": 0,
                      "attendee_count": None, "attendees": [], "speakers": []} for s in sessions],
        "total_sessions": len(sessions), "total_votes": 0,
        "total_councilors": len(names),
        "councilors": [{"name": n, "club": assignments.get(n, ""), "district": None,
                        "frekwencja": None, "aktywnosc": None, "zgodnosc_z_klubem": None,
                        "votes_total": 0, "rebellion_count": 0, "has_activity_data": False}
                       for n in names],
        "votes": [], "similarity_top": [], "similarity_bottom": [],
    }
    (docs / f"kadencja-{kad}.json").write_text(json.dumps(kadencja, ensure_ascii=False, indent=1), encoding="utf-8")

    profiles = {
        "scraped_at": __import__("datetime").datetime.now().isoformat(),
        "profiles": [{"name": n, "slug": _slug(n),
                      "kadencje": {kad: {"club": assignments.get(n, ""),
                                         "has_voting_data": False, "has_activity_data": False,
                                         "former": False, "mid_term": False}}}
                     for n in names],
        "total": len(names),
    }
    (docs / "profiles.json").write_text(json.dumps(profiles, ensure_ascii=False, indent=1), encoding="utf-8")
    (docs / "data.json").write_text(json.dumps({"generated": profiles["scraped_at"],
                                                "default_kadencja": kad,
                                                "kadencje": [{"id": kad, "label": cfg["kadencje"][kad]["label"]}]},
                                               ensure_ascii=False, indent=1), encoding="utf-8")
    (docs / "config.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    city_dir = Path(__file__).resolve().parent.parent
    if len(sys.argv) > 1:
        city_dir = Path(sys.argv[1])
    # dry-run descobrir
    if "--discover" in sys.argv:
        for h in find_radny_articles():
            n, c = parse_radny(h)
            print(f"  {n or h.split('/')[-1]:<34} {c}")
        print("sessions:", len(find_sessions()))
        sys.exit(0)
    raise SystemExit(build(city_dir))
