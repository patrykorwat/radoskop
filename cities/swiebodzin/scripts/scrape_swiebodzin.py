#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop Świebodzin — Tier-2 (roster / "model berliński") scraper.

Brak głosowań imiennych (esesja PM-B; bip.net.pl 404) — strona pokazuje
SKŁAD RADY (roster) + KALENDARZ SESJI IX kadencji.

Źródło: BIP Gminy Świebodzin https://bip.swiebodzin.eu (platforma idcom/JST):
  - skład rady (roster): wpisany w config.json club_assignments (20 radnych)
    — zaczerpnięty z /618/Rada_Miejska_2024-2029/ (lista wg okręgów);
    kluby niepublikowane na BIP → club_assignments puste, clubs {}.
  - sesje: /357/Protokoly_z_sesji_Rady/ (tytuł "Protokół nr <N>.<rok> z sesji
    Rady Miejskiej z dnia DD.MM.YYYY") — paginacja przez kolejne strony.

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

BASE = "https://bip.swiebodzin.eu"
SESS_PATH = "/357/Protokoly_z_sesji_Rady/"
IX_START = "2024-05-07"

_DATEFORM = [
    (r"z dnia\s+(\d{2})\.(\d{2})\.(\d{4})", "%d.%m.%Y"),
    (r"(?:\s|_)(\d{2})\.(\d{2})\.(\d{4})", "%d.%m.%Y"),
]


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
    s = s.lower()
    s = s.replace("ł", "l")
    return re.sub(r"[^a-z0-9]+", "", s) or "radny"


def _extract_date(text):
    for pat, fmt in _DATEFORM:
        m = re.search(pat, text)
        if m:
            try:
                return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
            except Exception:
                pass
    return None


def find_sessions():
    """Collect session dates >= IX_START across pages of /357/."""
    dates = set()
    seen_pages = set()
    queue = [SESS_PATH]
    while queue:
        path = queue.pop(0)
        if path in seen_pages:
            continue
        seen_pages.add(path)
        html = _http(path if path.startswith("http") else BASE + path)
        for m in re.finditer(r'href="([^"]+)"[^>]*>\s*(.*?)</a>', html, re.S | re.I):
            href = m.group(1)
            txt = re.sub(r"<[^>]+>", " ", m.group(2))
            txt = re.sub(r"\s+", " ", txt).strip()
            if "Protok" not in txt and "sesji" not in txt:
                continue
            d = _extract_date(txt)
            if d and d >= IX_START:
                dates.add(d)
            # pagination: linki w obrębie kategorii Protokoły (pełne URL-e wzg. ścieżki)
            if "/357/" in href and re.search(r"\d", href.split("/")[-1]) is None and href != SESS_PATH:
                if href.startswith("http"):
                    queue.append(href)
                else:
                    queue.append(BASE + href if href.startswith("/") else SESS_PATH)
    return sorted(dates)


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

    import datetime
    profiles = {
        "scraped_at": datetime.datetime.now().isoformat(),
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
    if "--discover" in sys.argv:
        print("sessions:", len(find_sessions()))
        for s in find_sessions():
            print("  ", s)
        sys.exit(0)
    raise SystemExit(build(city_dir))
