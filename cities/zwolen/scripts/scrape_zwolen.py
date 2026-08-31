#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop zwolen — Tier-2 ("model berliński"): skład rady + sesje, HAS_VOTING_DATA=false.

Zwoleń (powiat zwoleński, mazowieckie, SIMC 1436054, QID Q1897640, pop 7417).

Źródła (wszystkie live, IX kadencja 2024–2029):
- Roster + prezydium + komisje: https://samorzad.gov.pl/web/gmina-zwolen/rada-miejska
  i /komisje-rady-miejskiej (SSDIP gov.pl; bip.zwolen.pl = legacy, zamrożony na VIII kad.).
- Sesje (numer + data + URL): /kadencja-ix-2024-2028?page=1..4, 31 sesji
  (I 07-05-2024 ... XXXI 24-07-2026; paginacja ?page=N po 10).
- Brak głosowań imiennych w KAŻDYM źródle (zweryfikowane 2026-08-31): eSesja
  zwolen.esesja.pl = BIP Rady POWIATU (inny organ), rada.zwolen.pl brak DNS,
  zwolen.bip.net.pl 404, bip.zwolen.pl i gov.pl bez kategorii protokołów/głosowań.

Użycie: python scrape_zwolen.py [--city-dir cities/zwolen]
"""
import json
import re
import sys
import time
import unicodedata
from datetime import datetime
from pathlib import Path

import requests

BASE = "https://samorzad.gov.pl/web/gmina-zwolen"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Radoskop/1.0 (info@radoskop.eu)"}
KAD = "2024-2029"
KAD_LABEL = "IX kadencja (2024–2029)"
KAD_START = "2024-05-07"
MON = {
    "stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5, "czerwca": 6,
    "lipca": 7, "sierpnia": 8, "września": 9, "wrzesnia": 9, "października": 10,
    "listopada": 11, "grudnia": 12,
}
# Znane imiona radnych (z komisji gov.pl) — do normalizacji "Nazwisko Imię" -> "Imię Nazwisko"
FIRST = {"piotr", "teresa", "leszek", "krzysztof", "arkadiusz", "bożena", "marcin",
         "izabela", "anna", "radosław", "damian", "mirosław", "janusz", "edyta",
         "roman", "henryk", "stanisław", "włodzimierz", "marek", "tomasz", "adam"}
# Fallback roster (zweryfikowany 2026-08-31 z gov.pl /rada-miejska; scraper preferuje live)
FALLBACK_ROSTER = [
    ("Piotr Pawelec", "Przewodniczący Rady Miejskiej"),
    ("Teresa Kacperczyk-Baran", "Wiceprzewodniczący Rady Miejskiej"),
    ("Leszek Michalski", "Wiceprzewodniczący Rady Miejskiej"),
    ("Krzysztof Figurski", "Wiceprzewodniczący Rady Miejskiej"),
    ("Arkadiusz Figura", ""),
    ("Bożena Karaś", ""),
    ("Marcin Liberadzki", ""),
    ("Izabela Młyńska", ""),
    ("Anna Muszyńska", ""),
    ("Radosław Papiewski", ""),
    ("Damian Ogonowski", ""),
    ("Mirosław Walewski", ""),
    ("Janusz Wojas", ""),
    ("Arkadiusz Wrześniewski", ""),
    ("Edyta Zyzek", ""),
]

_LAST = 0.0


def _rate(delay=0.8):
    global _LAST
    d = time.time() - _LAST
    if d < delay:
        time.sleep(delay - d)
    _LAST = time.time()


def fetch(url, timeout=30):
    r = requests.get(url, timeout=timeout, headers=UA)
    r.raise_for_status()
    return r.text


def bodytext(t):
    t = re.sub(r"<script.*?</script>|<style.*?</style>", " ", t, flags=re.S | re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    t = re.sub(r"(&nbsp;|&#160;)", " ", t)
    t = re.sub(r"(&ndash;|&mdash;)", "–", t)
    t = t.replace("&oacute;", "ó")
    t = re.sub(r"\s+", " ", t)
    return t.strip()


def norm_name(name: str) -> str:
    n = re.sub(r"\s+", " ", name).strip(" .:–-")
    n = re.sub(r"-\s+", "-", n)  # 'Kacperczyk- Baran' -> 'Kacperczyk-Baran'
    parts = n.split()
    if len(parts) == 2:
        a, b = parts
        if b.lower() in FIRST and a.lower() not in FIRST:
            return f"{b} {a}"
    return n


def parse_roster(t: str) -> tuple[list, dict, bool]:
    """15 radnych + roles from gov.pl rada-miejska."""
    txt = bodytext(t)
    m = re.search(r"Radni Rady Miejskiej w Zwoleniu\s*-\s*kadencja 2024\s*-\s*2029(.{0,1500})", txt, re.S)
    if not m:
        return [], {}, False
    seg = m.group(1).split('{"register"')[0]
    items = re.findall(
        r"(\d{1,2})\.\s*([A-ZŁŚŻŹÓ][\w'’\-]+(?:[&\w;]?\s(?:[A-ZŁŚŻŹÓ][\w'’\-]+))*)"
        r"(?:\s*[–-]\s*([^\d]{5,60}?))?(?=\s*\d{1,2}\.\s|\s*$)",
        seg,
    )
    roster, roles = [], {}
    for _num, name, role in items:
        name = norm_name(name)
        if not name or name in roster or len(roster) >= 15:
            continue
        roster.append(name)
        role = re.sub(r"\s+", " ", (role or "")).strip(" .–-")
        if role and ("Przewodnicz" in role or "Wice" in role):
            roles[name] = role
    return roster, roles, len(roster) >= 12


def parse_committees(t: str) -> list:
    """Committee names (info-only; club_assignments PENDING)."""
    txt = bodytext(t)
    cut = txt.rfind("Komisje Rady Miejskiej Powrót")
    if cut < 0:
        return []
    txt = txt[cut:].split('{"register"')[0]
    comms = []
    for nm in re.findall(r"Komisja ([A-ZŁŚŻŹÓ][^:\d]{5,80}?)\s+(?=(?:[A-ZŁŚŻŹÓ{}\n]))", txt):
        nm = re.sub(r"\s+", " ", nm).strip(" .–-")
        if nm and nm not in comms and len(comms) < 8:
            comms.append(nm)
    return comms


def parse_sessions(pages: int = 4) -> list:
    """Session list from /kadencja-ix-2024-2028?page=1..P ('XXXI sesja ... 24-07-2026')."""
    sessions, seen = [], set()
    for p in range(1, pages + 1):
        try:
            t = fetch(f"{BASE}/kadencja-ix-2024-2028?page={p}")
        except Exception:
            break
        items = re.findall(
            r'<a href="(/web/gmina-zwolen/[\w-]*sesja[\w-]*)">\s*<div>\s*'
            r'<div class="title">([^<]*sesja[^<]*?)</div>',
            t, re.I,
        )
        new = 0
        for href, title in items:
            dm = re.search(r"(\d{2})-(\d{2})-(\d{4})", title)
            if not dm:
                continue
            iso = f"{dm.group(3)}-{dm.group(2)}-{dm.group(1)}"
            if iso < KAD_START:  # tylko IX kadencja
                continue
            m = re.search(r"([IVXLCD]{1,6})\s+sesja", title)
            num = m.group(1) if m else ""
            key = (num, iso)
            if key in seen:
                continue
            seen.add(key)
            sessions.append({
                "date": iso, "number": num,
                "label": f"Sesja {num} ({iso})".strip(),
                "url": "https://samorzad.gov.pl" + href,
                "vote_count": 0,
            })
            new += 1
        if new == 0:
            break
        _rate()
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
        roster, roles, ok = parse_roster(fetch(f"{BASE}/rada-miejska"))
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

    comms: list = []
    try:
        comms = parse_committees(fetch(f"{BASE}/komisje-rady-miejskiej"))
    except Exception as e:
        print(f"  [warn] komisje failed: {e}")

    sessions = parse_sessions(pages=4)
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
    print(f"  zwolen: roster={len(councilors)} sessions={len(sessions)} komisje={len(comms)} (club_assignments PENDING)")
    return 0


if __name__ == "__main__":
    if "--city-dir" in sys.argv:
        city_dir = Path(sys.argv[sys.argv.index("--city-dir") + 1])
    else:
        city_dir = Path(__file__).resolve().parent.parent
    raise SystemExit(build(city_dir))
