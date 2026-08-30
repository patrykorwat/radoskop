#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop Ryki — Tier-2 (roster / "model berliński") scraper.

Brak imiennych głosowań (BIP umryki.bip.lubelskie.pl publikuje protokoły
NARRACYJNE — agregaty, bez tabeli per-radny). Miasto dodawane jako Tier-2:
skład rady (roster) + kalendarz sesji IX kadencji.

Źródła (platforma "Wrota Lubelszczyzny", ta sama co parczew/lukow):
  - Skład Rady Miejskiej: /index.php?id=407 — tabela radnych z kolumną
    kadencji; IX kadencja = kad_id 1134 (15 radnych).
  - Kalendarz sesji: /index.php?id=526&action=list-ajax "Protokoły z sesji
    IX kadencji" — 43 protokołów (data_utworzenia = data sesji).

has_voting_data:false, voting_display:faction (roster-mode).
"""
import json
import re
import sys
import unicodedata
import urllib.parse
import urllib.request
import ssl
from pathlib import Path

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE
HEADERS = {"User-Agent": "Mozilla/5.0 (Radoskop cron)", "X-Requested-With": "XMLHttpRequest",
           "Accept": "application/json, text/javascript, */*; q=0.01"}

BASE = "https://umryki.bip.lubelskie.pl"
KAD_START = "2024-05-07"
KAD = "2024-2029"
IX_KAD_ID = "1134"          # kadencja id na stronie "Skład Rady Miejskiej" (id=407)
SESSIONS_CAT = "526"        # "Protokoły ... IX kadencji Rady Miejskiej"
ROSTER_CAT = "407"


def _http(url, data=None):
    headers = dict(HEADERS)
    req = urllib.request.Request(url, headers=headers)
    if data:
        req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode(), headers=headers)
    with urllib.request.urlopen(req, timeout=45, context=_CTX) as r:
        return r.read().decode("utf-8", "replace")


def _norm_name(raw):
    """'Tomasz Jerzy Kujda' -> 'Tomasz Kujda' (drop middle given name)."""
    parts = raw.split()
    if len(parts) >= 2:
        return f"{parts[0]} {' '.join(parts[1:])}".strip()
    return raw


def fetch_roster():
    """15 radnych IX kadencji z tabeli 'Skład Rady' (id=407, kad_id 1134)."""
    html = _http(f"{BASE}/index.php?id=407")
    names = {}
    for m in re.finditer(r'<tr>(.*?)</tr>', html, re.S):
        row = m.group(1)
        kid = re.search(r'szer_zero">(\d+)</td>', row)
        nm = re.search(r'id=osoba[^>]*>\s*([^<]+?)\s*</a>', row)
        if kid and nm and kid.group(1) == IX_KAD_ID:
            n = _norm_name(re.sub(r"\s+", " ", nm.group(1)).strip())
            if n and n not in names:
                names[n] = n
    return sorted(names.keys(), key=lambda n: n.split()[-1])


def fetch_sessions():
    """45 protokołów z sesji IX kad (id=526, list-ajax) — data sesji."""
    import json as _json
    data = {"draw": "1", "start": "0", "length": "200",
            "id": SESSIONS_CAT, "action": "list-ajax"}
    raw = _http(f"{BASE}/index.php?id={SESSIONS_CAT}&action=list-ajax", data=data)
    d = _json.loads(raw)
    sessions = []
    for a in d.get("aaData", []):
        dt = a.get("data_utworzenia", "")
        if not dt or dt < KAD_START:
            continue
        sessions.append({"date": dt, "number": "", "title": a.get("tresc", "")[:80]})
    sessions.sort(key=lambda s: s["date"])
    # dedupe by date
    seen = set(); out = []
    for s in sessions:
        if s["date"] in seen:
            continue
        seen.add(s["date"]); out.append(s)
    return out


def _slug(name):
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c)).lower().replace("ł", "l")
    s = re.sub(r"[^a-z0-9]+", "", s)
    return s or "radny"


def build(city_dir: Path) -> int:
    cfg = json.loads((city_dir / "config.json").read_text(encoding="utf-8"))
    docs = city_dir / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    names = fetch_roster()
    sessions = fetch_sessions()
    print(f"  ryki roster: {len(names)}  sessions IX: {len(sessions)}")

    kadencja = {
        "id": KAD, "label": cfg["kadencje"][KAD]["label"], "clubs": {},
        "sessions": [{"date": s["date"], "number": s["number"], "vote_count": 0,
                      "attendee_count": None, "attendees": [], "speakers": []} for s in sessions],
        "total_sessions": len(sessions), "total_votes": 0, "total_councilors": len(names),
        "councilors": [{"name": n, "club": "", "district": None, "frekwencja": None,
                        "aktywnosc": None, "zgodnosc_z_klubem": None, "votes_total": 0,
                        "rebellion_count": 0, "has_activity_data": False} for n in names],
        "votes": [], "similarity_top": [], "similarity_bottom": [],
    }
    (docs / f"kadencja-{KAD}.json").write_text(json.dumps(kadencja, ensure_ascii=False, indent=1), encoding="utf-8")

    profiles = {
        "scraped_at": __import__("datetime").datetime.now().isoformat(),
        "profiles": [{"name": n, "slug": _slug(n),
                      "kadencje": {KAD: {"club": "", "has_voting_data": False,
                                         "has_activity_data": False, "former": False, "mid_term": False}}}
                     for n in names],
        "total": len(names),
    }
    (docs / "profiles.json").write_text(json.dumps(profiles, ensure_ascii=False, indent=1), encoding="utf-8")

    data = {"generated": __import__("datetime").datetime.now().isoformat(),
            "default_kadencja": KAD,
            "kadencje": [{"id": KAD, "label": cfg["kadencje"][KAD]["label"]}]}
    (docs / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    (docs / "config.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    city_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent
    raise SystemExit(build(city_dir))
