#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop nasielsk — Tier-2 (roster / "model berliński") scraper.

Brak głosowań imiennych IX kadencji (zestawienie sprawdzone 2026-08-31):
 * nasielsk.esesja.pl = wildcard korporacyjny (strona marketingowa esesja.pl),
 * nowa platforma nasielsk.sesja.pl ("Portal") — SPA Vue; /wyniki-glosowan 301→/,
   API /api/v1/vote/votes i /api/v1/user/voters wymagają JWT (401),
   jedyny publiczny endpoint: /api/v1/portal/video-recordings (NAGRANIA sesji),
 * rada.nasielsk.pl (AlfaTV) — brak DNS/instancji,
 * nasielsk.bip.net.pl — 404 (brak Nefeni),
 * BIP bip.nasielsk.pl — protokoły sesji to SKANY PDF; "szczegóły głosowania"
   są w załącznikach DSSS Vote, ale jako skany bez warstwy tekstowej
   (OCR: lista radnych czytelna, znaczniki głosów graficzne → brak wiarygodnej
   atrybucji per radny; wzorzec Pultusk),
 * Rejestr Klubów Radnych w BIP dotyczy kadencji 2018-2023 (stary).

Dodawane jako Tier-2: skład rady (15 radnych IX kadencji, roles z
www.nasielsk.pl) + kalendarz sesji z nasielsk.sesja.pl API (video-recordings,
bez auth — 30 sesji I..XXX).
"""
import json
import re
import sys
import time
import unicodedata
from datetime import datetime
from pathlib import Path

import requests

KAD = "2024-2029"
KAD_LABEL = "IX kadencja (2024–2029)"
KAD_START = "2024-05-07"
SESJA_API = "https://nasielsk.sesja.pl/api/v1/portal/video-recordings"
HDRS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Radoskop/1.0 (info@radoskop.eu)",
        "Accept": "application/json"}

# Roster statyczny — zweryfikowany 2026-08-31 z www.nasielsk.pl
# (/radni-rady-miejskiej-w-nasielsku-wybrani-na-ix-kadencje-samorzadu-gminy-nasielsk.html)
ROLES = {
    "Marek Gerasik": "Przewodniczący Rady Miejskiej w Nasielsku",
    "Zbigniew Wóltański": "Wiceprzewodniczący Rady Miejskiej w Nasielsku",
    "Antoni Kalinowski": "Radny",
    "Bogumiła Rębecka": "Radna",
    "Dariusz Kordowski": "Radny",
    "Dariusz Sawicki": "Radny",
    "Dawid Domała": "Radny",
    "Henryk Antosik": "Radny",
    "Hubert Kuczborski": "Radny",
    "Iwona Wróblewska": "Radna",
    "Jan Lewandowski": "Radny",
    "Paulina Szczerbicka": "Radna",
    "Piotr Fronczak": "Radny",
    "Robert Mateusiak": "Radny",
    "Roman Jaskulski": "Radny",
}
NAMES = sorted(ROLES)


def _slug(name):
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c)).lower().replace("ł", "l")
    s = re.sub(r"[^a-z0-9]+", "", s)
    return s or "radny"


def fetch_sessions(cache_dir: Path | None = None):
    """Sesje IX kadencji z publicznego API platformy sesja.pl (nagrania).

    POST/GET /api/v1/portal/video-recordings?page=N&perPage=50 →
    items[].title = 'Sesja Nr XXX z dnia 19.08.2026'.
    """
    out = {}
    page = 1
    while page <= 10:
        try:
            r = requests.get(f"{SESJA_API}?page={page}&perPage=50",
                             headers=HDRS, timeout=30)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            print(f"[nasielsk] API page {page} fail: {e}")
            break
        items = data.get("items") or []
        if not items:
            break
        for it in items:
            title = it.get("title", "")
            m = re.search(r"Sesja\s+Nr\s+([IVXLCDM]+)\s+z\s+dnia\s+(\d{1,2})\.(\d{1,2})\.(\d{4})"
                          r"|Sesja\s+Nr\s+([IVXLCDM]+)\s+z\s+dnia\s+(\d{1,2})[-.](\d{1,2})[-.](\d{4})",
                          title, re.I)
            if not m:
                continue
            roman = m.group(1) or m.group(5)
            d, mo, y = (m.group(2), m.group(3), m.group(4)) if m.group(2) \
                else (m.group(6), m.group(7), m.group(8))
            date = f"{y}-{int(mo):02d}-{int(d):02d}"
            if date < KAD_START:
                continue
            out[date] = {
                "number": roman, "date": date,
                "label": f"Sesja Nr {roman} ({date})",
                "video_url": f"https://nasielsk.sesja.pl/sesja-online/{it.get('id')}",
                "title": title,
            }
        if page >= (data.get("pages") or 1):
            break
        page += 1
        time.sleep(0.4)
    sessions = [out[k] for k in sorted(out, reverse=True)]
    return sessions


def build(city_dir: Path) -> int:
    cfg = json.loads((city_dir / "config.json").read_text(encoding="utf-8"))
    docs = city_dir / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    names = list(NAMES)
    sessions = fetch_sessions()
    print(f"[nasielsk] roster: {len(names)} radnych; sesje z API: {len(sessions)}")
    if not sessions:
        print("[nasielsk] WARN: sesje z API puste — fallback: brackets bez sesji")

    kadencja = {
        "id": KAD, "label": cfg["kadencje"][KAD]["label"], "clubs": {},
        "sessions": [{"date": s["date"], "number": s["number"],
                      "label": s["label"], "url": s["video_url"],
                      "vote_count": 0} for s in sessions],
        "total_sessions": len(sessions),
        "total_votes": 0, "total_councilors": len(names),
        "councilors": [{"name": n, "club": "", "role": ROLES.get(n, ""),
                        "district": None, "frekwencja": None, "aktywnosc": None,
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
                      "kadencje": {KAD: {"club": "", "role": ROLES.get(n, ""),
                                         "has_voting_data": False,
                                         "has_activity_data": False,
                                         "frekwencja": None, "aktywnosc": None,
                                         "zgodnosc_z_klubem": None,
                                         "votes_za": 0, "votes_przeciw": 0,
                                         "votes_wstrzymal": 0, "votes_total": 0,
                                         "rebellion_count": 0,
                                         "former": False, "mid_term": False}}}
                     for n in names],
        "total": len(names),
    }
    (docs / "profiles.json").write_text(
        json.dumps(profiles, ensure_ascii=False, indent=1), encoding="utf-8")
    data = {"generated": datetime.now().isoformat(), "default_kadencja": KAD,
            "kadencje": [{"id": KAD, "label": KAD_LABEL}]}
    (docs / "data.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    (docs / "config.json").write_text(
        json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[nasielsk] OUT: {len(names)} radnych / {len(sessions)} sesji / 0 głosowań")
    return 0


if __name__ == "__main__":
    raise SystemExit(build(Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()))
