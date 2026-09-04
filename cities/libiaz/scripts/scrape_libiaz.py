#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop Libiąż — Tier-2 (roster / "model berliński") scraper.

Brak głosowań imiennych w formie maszynowo czytelnej: BIP eBOI malopolska
(bip.malopolska.pl/umlibiaz) publikuje przy każdej uchwale załącznik
"Protokół głosowania" jako SKAN (obraz 1653x2338, brak warstwy tekstu) —
OCR tabeli kolumnowej bez wiarygodnej atrybucji per radny.
eSesja libiaz.esesja.pl = PM-instance B (sessions-list 0 sesji IX kad.).
Strona pokazuje SKŁAD RADY (21 radnych, IX kadencja) + KALENDARZ SESJI.

Skład: artykuł /api/articles/2730966 ("Dane kontaktowe do radnych (email)
- kadencja 2024-2029", nazwisko imię + email służbowy @libiaz.pl).
Kalendarz: kategoria /api/menu/433975/articles ("Rada > Sesje > IX kadencja")
— artykuły "Zawiadomienie o <RZYMSKA> sesji - DD.MM.RRRR r.".
"""
import json
import re
import ssl
import sys
import unicodedata
import urllib.request
from datetime import datetime
from pathlib import Path

API = "https://bip.malopolska.pl/api"
ROSTER_ART = 2730966
SESSIONS_CAT = 433975
KAD_START = "2024-05-07"
KAD = "2024-2029"

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE
_UA = {"User-Agent": "Mozilla/5.0 (Radoskop; info@radoskop.eu)"}


def _get_json(url):
    import time
    for att in range(4):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=_UA),
                                        timeout=40, context=_CTX) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except Exception as e:  # noqa: BLE001
            if att == 3:
                raise
            time.sleep(2 + att * 3)


MONTHS = {"stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5,
          "czerwca": 6, "lipca": 7, "sierpnia": 8, "września": 9,
          "października": 10, "listopada": 11, "grudnia": 12}


def _slug(name):
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace("ł", "l")
    s = re.sub(r"[^a-z0-9]+", "", s)
    return s or "radny"


def fetch_roster():
    d = _get_json(f"{API}/articles/{ROSTER_ART}")
    import html as h
    c = h.unescape(re.sub("<[^>]+>", "\n", d.get("content") or ""))
    pairs = re.findall(
        r"([A-ZŁŚŻŹĆĘÓĄ][\wŁŚŻŹĆĘÓĄ\-]+)\s+([A-ZŁŚŻŹĆĘÓĄ][\wŁŚŻŹĆĘÓĄ\-]+)\s*-\s*\n?\s*[\w.\-]+@[\w.\-]+",
        c.replace("\r", ""))
    names = [f"{first} {fam}" for fam, first in pairs]
    return sorted(set(names), key=lambda n: n.split()[-1])


ROMAN_VAL = 0


def _roman(s):
    vals = {"I": 1, "V": 5, "X": 10, "L": 50}
    total, prev = 0, 0
    for ch in reversed(s):
        v = vals.get(ch, 0)
        total += v if v >= prev else -v
        prev = max(prev, v)
    return total


def fetch_sessions():
    out = []
    j = _get_json(f"{API}/menu/{SESSIONS_CAT}/articles?limit=100&offset=0")
    for a in (j.get("articles") or []):
        title = (a.get("aliasFields") or [{}])[0].get("value") or a.get("title") or ""
        m = re.search(r"([IVXL]+)\s+sesji?\s*-\s*(\d{1,2})\.(\d{1,2})\.(\d{4})", title)
        if not m:
            continue
        roman, dd, mm, yy = m.group(1), int(m.group(2)), int(m.group(3)), int(m.group(4))
        date = f"{yy:04d}-{mm:02d}-{dd:02d}"
        if date < KAD_START:
            continue
        out.append({"date": date, "number": _roman(roman)})
    seen = {}
    for s in out:
        seen.setdefault(s["date"], s)
    return sorted(seen.values(), key=lambda s: s["date"])


def build(city_dir):
    cfg = json.loads((Path(city_dir) / "config.json").read_text(encoding="utf-8"))
    docs = Path(city_dir) / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    names = fetch_roster()
    print(f"  roster: {len(names)}")
    if len(names) < 10:
        raise SystemExit("roster too small — abort")
    sessions = fetch_sessions()
    print(f"  sessions: {len(sessions)}")
    if len(sessions) < 5:
        raise SystemExit("too few sessions — abort")

    clubs = cfg.get("clubs", {}) or {}
    assignments = cfg.get("club_assignments", {}) or {}
    kadencja = {
        "id": KAD, "label": cfg["kadencje"][KAD]["label"],
        "clubs": {k: v.get("name", k) for k, v in clubs.items()},
        "sessions": [{"date": s["date"], "number": s["number"], "vote_count": 0,
                      "attendee_count": None, "attendees": [], "speakers": []}
                     for s in sessions],
        "total_sessions": len(sessions), "total_votes": 0,
        "total_councilors": len(names),
        "councilors": [{"name": n, "club": assignments.get(n, ""), "district": None,
                        "frekwencja": None, "aktywnosc": None,
                        "zgodnosc_z_klubem": None, "votes_total": 0,
                        "rebellion_count": 0, "has_activity_data": False}
                       for n in names],
        "votes": [], "similarity_top": [], "similarity_bottom": [],
    }
    (docs / f"kadencja-{KAD}.json").write_text(
        json.dumps(kadencja, ensure_ascii=False, indent=1), encoding="utf-8")
    profiles = {
        "scraped_at": datetime.now().isoformat(),
        "profiles": [{"name": n, "slug": _slug(n),
                      "kadencje": {KAD: {"club": assignments.get(n, ""),
                                         "has_voting_data": False,
                                         "has_activity_data": False,
                                         "former": False, "mid_term": False}}}
                     for n in names],
        "total": len(names),
    }
    (docs / "profiles.json").write_text(
        json.dumps(profiles, ensure_ascii=False, indent=1), encoding="utf-8")
    (docs / "data.json").write_text(json.dumps({
        "generated": datetime.now().isoformat(),
        "default_kadencja": KAD,
        "kadencje": [{"id": KAD, "label": cfg["kadencje"][KAD]["label"]}]},
        ensure_ascii=False, indent=1), encoding="utf-8")
    (docs / "config.json").write_text(
        json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"  OK {len(names)} radnych, {len(sessions)} sesji")
    return 0


if __name__ == "__main__":
    city_dir = Path(__file__).resolve().parent.parent
    if len(sys.argv) > 1:
        city_dir = Path(sys.argv[1])
    raise SystemExit(build(city_dir))
