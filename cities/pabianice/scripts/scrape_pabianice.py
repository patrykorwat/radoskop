#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop Pabianice — Tier-2 (roster / "model berliński") scraper.

Brak imiennych głosowań: BIP bip.um.pabianice.pl publikuje protokoły sesji
z AGREGATAMI ("Głosowanie jawne imienne -za- 21, przeciw-0...") i wynikami
imiennymi tylko jako załączniki "do wglądu w Biurze Rady" (brak plików).
Miasto dodawane jako Tier-2: skład rady + kalendarz sesji IX kadencji.

Źródła (BIP custom CMS "Solidarnościowy" — nie Nefeni/Sputnik):
  - Skład: /artykul/89/20969 = "Radni Rady Miejskiej IX kadencji" — lista
    nazwisk w treści (23 radnych, z funkcjami po myślniku).
  - Sesje: /artykuly/1228|1205|1150/{2026,2025,2024}-rok — artykuły
    "XXX sesja Rady Miejskiej w Pabianicach - 2 września 2026 r."

has_voting_data:false, voting_display:faction (roster-mode).
"""
import json
import re
import ssl
import sys
import unicodedata
import urllib.request
from datetime import datetime
from pathlib import Path

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE
HEADERS = {"User-Agent": "Mozilla/5.0 (Radoskop cron)"}

BASE = "https://bip.um.pabianice.pl"
KAD_START = "2024-05-07"
KAD = "2024-2029"
ROSTER_ART = "/artykul/89/20969"
SESSION_CATS = {
    "1228": "2026-rok",
    "1205": "2025-rok",
    "1150": "2024-rok",
}

MONTHS_PL = {
    "stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5, "czerwca": 6,
    "lipca": 7, "sierpnia": 8, "września": 9, "października": 10, "listopada": 11,
    "grudnia": 12,
}


def _http(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=45, context=_CTX) as r:
        return r.read().decode("utf-8", "replace")


def _main_text(html):
    m = re.search(r"<main.*?</main>", html, re.S)
    txt = m.group(0) if m else html
    txt = re.sub(r"<script.*?</script>|<style.*?</style>", " ", txt, flags=re.S)
    return txt


def fetch_roster():
    """23 radnych IX kadencji z artykułu 'Radni Rady Miejskiej IX kadencji'.
    Funkcje (Przewodnicząca / Wiceprzewodniczący) po ' - ' w tekście."""
    html = _http(f"{BASE}{ROSTER_ART}/x")
    txt = _main_text(html)
    plain = re.sub(r"<[^>]+>", " ", txt)
    plain = re.sub(r"\s+", " ", plain)
    i = plain.find("IX kadencji")
    seg = plain[i: i + 1600] if i > 0 else plain
    # cut at address
    j = seg.find("95-200")
    if j > 0:
        seg = seg[:j]
    roles = {}
    _NOISE = {"miejskiej", "miejski", "rady", "radnych", "rada", "biuro", "urząd",
              "kadencji", "sesji", "komisji", "rady", "piętro", "pokój"}
    for m in re.finditer(
        r"([A-ZĄĆĘŁŃÓŚŹŻ][a-ząćęłńóśźż]+(?:-[A-ZĄĆĘŁŃÓŚŹŻ][a-ząćęłńóśźż]+)?\s+[A-ZĄĆĘŁŃÓŚŹŻ][a-ząćęłńóśźż]+)"
        r"(?:\s*-\s*(Przewodnicząca|Przewodniczący|Wiceprzewodnicząca|Wiceprzewodniczący))?",
        seg,
    ):
        raw = re.sub(r"\s+", " ", m.group(1)).strip()
        parts = raw.split()
        if len(parts) < 2:
            continue
        if any(p.lower() in _NOISE for p in parts):
            continue
        # BIP podaje 'Nazwisko Imię' (np. 'Dychto Emilia') -> zamień na 'Imię Nazwisko'
        name = " ".join(parts[1:] + [parts[0]])
        if name not in roles:
            roles[name] = m.group(2) or ""
    return roles


def fetch_sessions():
    out = []
    for cat, slug in SESSION_CATS.items():
        try:
            html = _http(f"{BASE}/artykuly/{cat}/{slug}")
        except Exception:
            continue
        for aid, ttl in re.findall(
            r'artykul/\d+/(\d+)/[^"]+"[^>]*>\s*([^<]{5,140})<', html
        ):
            if "sesja Rady" not in ttl:
                continue
            m = re.search(r"(\d{1,2})\s+(" + "|".join(MONTHS_PL) + r")\s+(\d{4})", ttl)
            if not m:
                continue
            d = f"{m.group(3)}-{int(MONTHS_PL[m.group(2)]):02d}-{int(m.group(1)):02d}"
            if d < KAD_START:
                continue
            num = re.match(r"([IVXL]+)\s+sesja", ttl.strip())
            out.append({"date": d, "number": num.group(1) if num else "",
                        "title": re.sub(r"\s+", " ", ttl).strip()[:80]})
    seen = set(); uniq = []
    for s in sorted(out, key=lambda x: x["date"]):
        k = (s["date"], s["number"])
        if k in seen:
            continue
        seen.add(k); uniq.append(s)
    return uniq


def _slug(name):
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c)).lower().replace("ł", "l")
    s = re.sub(r"[^a-z0-9]+", "", s)
    return s or "radny"


def _proper(name):
    """'Dychto Emila' (kolejność nazwisko-imię z załącznika) nie użyta;
    artykuł podaje 'Imię Nazwisko' — zwracamy jak jest."""
    return re.sub(r"\s+", " ", name).strip()


def build(city_dir: Path) -> int:
    cfg = json.loads((city_dir / "config.json").read_text(encoding="utf-8"))
    docs = city_dir / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    roles = fetch_roster()
    sessions = fetch_sessions()
    names = sorted(roles.keys(), key=lambda n: n.split()[-1])
    print(f"  pabianice roster: {len(names)}  sessions IX: {len(sessions)}")
    if len(names) < 10:
        print("  [ERR] roster zbyt mały — przerywam (nie fabrykujemy)")
        return 1

    kadencja = {
        "id": KAD, "label": cfg["kadencje"][KAD]["label"], "clubs": {},
        "sessions": [{"date": s["date"], "number": s["number"], "vote_count": 0,
                      "attendee_count": None, "attendees": [], "speakers": []}
                     for s in sessions],
        "total_sessions": len(sessions), "total_votes": 0, "total_councilors": len(names),
        "councilors": [{"name": n, "club": "", "district": None, "frekwencja": None,
                        "aktywnosc": None, "zgodnosc_z_klubem": None, "votes_total": 0,
                        "rebellion_count": 0, "has_activity_data": False} for n in names],
        "votes": [], "similarity_top": [], "similarity_bottom": [],
    }
    (docs / f"kadencja-{KAD}.json").write_text(
        json.dumps(kadencja, ensure_ascii=False, indent=1), encoding="utf-8")
    (docs / "data.json").write_text(json.dumps(
        {"generated": datetime.now().isoformat(), "default_kadencja": KAD,
         "kadencje": [{"id": KAD, "label": cfg["kadencje"][KAD]["label"]}]},
        ensure_ascii=False), encoding="utf-8")

    profiles = []
    for n in names:
        role = roles.get(n, "")
        profiles.append({
            "name": n, "slug": _slug(n),
            "kadencje": {KAD: {
                "club": "", "has_voting_data": False, "has_activity_data": False,
                "frekwencja": None, "aktywnosc": None, "zgodnosc_z_klubem": None,
                "votes_za": 0, "votes_przeciw": 0, "votes_wstrzymal": 0,
                "votes_brak": 0, "votes_nieobecny": 0, "votes_total": 0,
                "rebellion_count": 0, "rebellions": [],
                "roles": [role] if role else [], "notes": "",
                "former": False, "mid_term": False}},
        })
    (docs / "profiles.json").write_text(
        json.dumps({"profiles": profiles, "total": len(profiles)},
                   ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"  [ok] kadencja + profiles ({len(profiles)})")
    return 0


if __name__ == "__main__":
    city_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]
    raise SystemExit(build(city_dir))
