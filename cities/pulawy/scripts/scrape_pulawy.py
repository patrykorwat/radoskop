#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop Puławy — Tier-2 (roster / "model berliński") scraper.

Brak głosowań imiennych: BIP (bip.pulawy.pl + pulawy.bip.lubelskie.pl,
platforma Wrota Lubelszczyzny) publikuje protokoły NARRACYJNE bez tabel
per-radny; eSesja = wildcard, brak rady online. Miasto jako Tier-2:
skład rady IX kad. + kalendarz sesji (protokoły z sesji 2024-2026).

Źródła:
  - Skład: pulawy.bip.lubelskie.pl/index.php?id=2107 (karty radnych
    "Imię i nazwisko / Stanowisko"; kluby z fraz "klubu radnych X").
  - Sesje: bip.pulawy.pl "Protokoły z sesji - 2024/2025/2026"
    (kategorie id=2090/2222/2338), linki "Sesja z dnia D miesiąca R".

has_voting_data:false, voting_display:faction (roster-mode).
"""
import json
import re
import ssl
import sys
import time
import unicodedata
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE
UA = {"User-Agent": "Mozilla/5.0 (Radoskop cron)"}

KAD = "2024-2029"
KAD_START = "2024-05-07"
ROSTER_URL = "http://umpulawy.bip.lubelskie.pl/index.php?id=2107"
SESSION_CATS = ["2338", "2222", "2090"]   # protokoły z sesji 2026 / 2025 / 2024
MONTHS = {m: i for i, m in enumerate(
    "stycznia lutego marca kwietnia maja czerwca lipca sierpnia września "
    "października listopada grudnia".split(), 1)}
ROLE_RE = re.compile(r"^(Radn[ay]|Przewodnicz\w+|Wiceprzewodnicz\w+)\b")


def _http(url: str) -> str:
    for attempt in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA),
                                        timeout=40, context=CTX) as r:
                return r.read().decode("utf-8", "replace")
        except Exception:
            if attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))


def _lines(html: str, start_marker: str) -> list[str]:
    seg = html[html.rfind(start_marker):]
    txt = re.sub(r"<[^>]+>", "\n", seg)
    return [l.strip() for l in txt.splitlines() if l.strip()]


def fetch_roster() -> list[dict]:
    """Rada IX kad. z kart radnych; kluby z frazy 'klubu radnych X'."""
    lines = _lines(_http(ROSTER_URL), "Skład Rady")
    people: list[dict] = []
    for i, l in enumerate(lines):
        if ROLE_RE.match(l) and i > 0 and len(lines[i - 1]) < 40:
            name = re.sub(r"\s+", " ", lines[i - 1])
            if not re.match(r"^[A-ZŁŚŻ][a-ząćęłńóśźż]+( [A-ZŁŚŻ][a-ząćęłńóśźż-]+)+$", name):
                continue
            window = " ".join(lines[i:i + 30])
            mc = re.search(r"klubu radnych\s+([^.]+?)\s*(?:Zam|Prac|Wszystkie|Członek|Pracuje|$)", window)
            club = mc.group(1).strip() if mc else ""
            club = re.sub(r"\s+", " ", club)
            role = ""
            if l.startswith("Przewodnicz"):
                role = "Przewodniczący Rady"
            elif l.startswith("Wiceprzewodnicz"):
                role = "Wiceprzewodniczący Rady"
            if name not in [p["name"] for p in people]:
                people.append({"name": name, "club": club, "role": role})
    return people


def fetch_sessions() -> list[dict]:
    out = []
    for cat in SESSION_CATS:
        html = _http(f"https://bip.pulawy.pl/index.php?id={cat}")
        for h, t in re.findall(r'<a[^>]+href="(/index\.php\?id=\d+)"[^>]*>([^<]+)</a>', html):
            if "Sesja z dnia" not in t:
                continue
            m = re.search(r"z dnia\s+(\d{1,2})\s+([a-ząćł]+\w*)\.?\s+(\d{4})", t)
            if not m:
                continue
            mon = MONTHS.get(m.group(2).lower().rstrip("."))
            if not mon:
                continue
            date = f"{m.group(3)}-{mon:02d}-{int(m.group(1)):02d}"
            if date >= KAD_START:
                out.append({"date": date, "number": "", "title": re.sub(r"\s+", " ", t).strip()[:80],
                            "url": "https://bip.pulawy.pl" + h})
        time.sleep(0.4)
    uniq = {}
    for s in out:
        uniq[s["date"]] = s
    return sorted(uniq.values(), key=lambda s: s["date"])


def _slug(name: str) -> str:
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c)).lower().replace("ł", "l")
    return re.sub(r"[^a-z0-9]+", "", s) or "radny"


def build(city_dir: Path) -> int:
    cfg = json.loads((city_dir / "config.json").read_text(encoding="utf-8"))
    docs = city_dir / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    people = fetch_roster()
    sessions = fetch_sessions()
    print(f"  pulawy roster: {len(people)}  sessions IX: {len(sessions)}")
    if len(people) < 15:
        raise SystemExit(f"zły roster ({len(people)}) — przerywam, nie fabrykuję")

    clubs = {}
    councilors = []
    for p in people:
        club = cfg.get("club_assignments", {}).get(p["name"], "") or p["club"]
        councilors.append({"name": p["name"], "club": club, "district": None,
                           "frekwencja": None, "aktywnosc": None,
                           "zgodnosc_z_klubem": None, "votes_total": 0,
                           "rebellion_count": 0, "has_activity_data": False})
        if club:
            clubs.setdefault(club, {})
    kadencja = {
        "id": KAD, "label": cfg["kadencje"][KAD]["label"], "clubs": {},
        "sessions": [{"date": s["date"], "number": s["number"], "vote_count": 0,
                      "attendee_count": None, "attendees": [], "speakers": [],
                      "title": s["title"]} for s in sessions],
        "total_sessions": len(sessions), "total_votes": 0,
        "total_councilors": len(people),
        "councilors": councilors,
        "votes": [], "similarity_top": [], "similarity_bottom": [],
    }
    (docs / f"kadencja-{KAD}.json").write_text(json.dumps(kadencja, ensure_ascii=False, indent=1), encoding="utf-8")
    profiles = {"scraped_at": datetime.now(timezone.utc).isoformat(), "total": len(people),
                "profiles": [{"name": p["name"], "slug": _slug(p["name"]),
                              "kadencje": {KAD: {"club": p["club"], "has_voting_data": False,
                                                 "has_activity_data": False, "former": False,
                                                 "mid_term": False, "role": p["role"]}}}
                             for p in people]}
    (docs / "profiles.json").write_text(json.dumps(profiles, ensure_ascii=False, indent=1), encoding="utf-8")
    data = {"generated": datetime.now(timezone.utc).isoformat(), "default_kadencja": KAD,
            "kadencje": [{"id": KAD, "label": cfg["kadencje"][KAD]["label"]}]}
    (docs / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    (docs / "config.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(build(Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent))
