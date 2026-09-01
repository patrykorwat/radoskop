#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop Sułkowice — Tier-2 (model berliński): skład rady + kalendarz sesji, BEZ imiennych.

Źródło: BIP "Małopolska BIP" bip.malopolska.pl (context 'umsulkowice', platforma
Madkom/Angular SPA + JSON API bez auth):
  * Skład IX kadencji: /api/menu/433826/articles  (Rada > Skład Rady > 2024-2029) —
    artykuły per radny, columnFields 7442=Nazwisko, 7441=Imię, 7443=Stanowisko.
  * Kalendarz sesji IX: /api/menu/433827/articles (Rada > Sesje > 2024-2029) —
    artykuł 'N sesja Rady Miejskiej', data sesji w treści ('Data sesji: YYYY-MM-DD').
  * Protokoły: /api/menu/77314/submenu → kategorie roczne (475676=2026, 304571=2025,
    77323=2024), załącznik PDF przez /api/files/{attachmentId}.

Głosowania imienne: NIE PUBLIKOWANE. Protokoły sesji podają WYŁĄCZNIE agregaty
('Głosowało za: 14 / przeciw: 0 / wstrzymało się od głosu: 0 (14 Radnych obecnych)'),
zero wystąpień 'imien' w całym protokole; osobny 'Protokół głosowania' jest wymieniony
jako załącznik nr N w treści, ale nie jest dołączany jako pobieralny plik.
sesja.pl (sulkowice.sesja.pl) = ApiPlatform 'Session' portal bez publicznych głosowań
(/api/v1 → tylko cms/document/vod view, /portal/pages total=0); eSesja.pl wildcard;
AlfaTV rada.sulkowice.pl = placeholder hostingu; bip.net.pl 404.

Użycie: python scrape_sulkowice.py --city-dir <cities/sulkowice> [--cache-dir dir]
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
from html import unescape
from pathlib import Path

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

API = "https://bip.malopolska.pl/api"
CONTEXT = "umsulkowice"
ROSTER_MENU = "433826"      # Rada > Skład Rady > 2024-2029
SESSIONS_MENU = "433827"    # Rada > Sesje > 2024-2029
PROTOCOL_MENUS = ("475676", "304571", "77323")   # Protokoły: 2026 / 2025 / 2024
KAD_START = "2024-05-07"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Radoskop/1.0 (info@radoskop.eu)",
           "Accept-Language": "pl,en"}
REQ_DELAY = 0.45
_LAST = 0.0

_ROM_MAP = {}
for _v, _r in [(1, "I"), (2, "II"), (3, "III"), (4, "IV"), (5, "V"), (6, "VI"), (7, "VII"),
               (8, "VIII"), (9, "IX"), (10, "X"), (11, "XI"), (12, "XII"), (13, "XIII"),
               (14, "XIV"), (15, "XV"), (16, "XVI"), (17, "XVII"), (18, "XVIII"), (19, "XIX"),
               (20, "XX"), (21, "XXI"), (22, "XXII"), (23, "XXIII"), (24, "XXIV"),
               (25, "XXV"), (26, "XXVI"), (27, "XXVII"), (28, "XXVIII"), (29, "XXIX"),
               (30, "XXX"), (31, "XXXI"), (32, "XXXII"), (33, "XXXIII"), (34, "XXXIV"),
               (35, "XXXV"), (36, "XXXVI"), (37, "XXXVII"), (38, "XXXVIII"), (39, "XXXIX"),
               (40, "XL")]:
    _ROM_MAP[_r] = _v


def _rate():
    global _LAST
    now = time.time()
    if now - _LAST < REQ_DELAY:
        time.sleep(REQ_DELAY - (now - _LAST))
    _LAST = time.time()


def _get_json(url, cache=None):
    cf = None
    if cache is not None:
        Path(cache).mkdir(parents=True, exist_ok=True)
        cf = Path(cache) / (hashlib.md5(url.encode()).hexdigest() + ".json")
        if cf.is_file():
            try:
                return json.loads(cf.read_text(encoding="utf-8"))
            except Exception:
                pass
    _rate()
    j = None
    for attempt in range(4):
        try:
            r = requests.get(url, headers=HEADERS, timeout=60, verify=False)
            r.raise_for_status()
            j = r.json()
            break
        except Exception:
            if attempt == 3:
                raise
            time.sleep(2 + 2 * attempt)
    if cf is not None:
        cf.write_text(json.dumps(j, ensure_ascii=False), encoding="utf-8")
    return j


def _menu_articles(menu_id, cache=None):
    out, offset = [], 0
    while True:
        d = _get_json(f"{API}/menu/{menu_id}/articles?limit=50&offset={offset}", cache)
        arts = d.get("articles") or []
        out.extend(arts)
        total = d.get("total") or 0
        offset += 50
        if not arts or offset >= total:
            break
    return d.get("menuPath") or [], out


def _title_of(a):
    for x in (a.get("aliasFields") or []):
        if x.get("alias") == "title":
            return re.sub(r"\s+", " ", unescape(str(x.get("value") or ""))).strip()
    return ""


def _fields_of(a):
    cf = {}
    for x in (a.get("columnFields") or []):
        fid = str(x.get("fieldId", ""))
        if fid.isdigit():
            cf[int(fid)] = re.sub(r"\s+", " ", unescape(str(x.get("value") or ""))).strip()
    return cf


def scrape_roster(cache=None):
    """(roster names, role map) z 'Skład Rady > 2024-2029'."""
    _, arts = _menu_articles(ROSTER_MENU, cache)
    names, role = [], {}
    for a in arts:
        cf = _fields_of(a)
        naz, imie, st = cf.get(7442, ""), cf.get(7441, ""), cf.get(7443, "")
        nm = re.sub(r"\s+", " ", f"{imie} {naz}".strip())
        if not nm:
            t = _title_of(a)
            parts = t.split()
            nm = " ".join(reversed(parts[:2])) if len(parts) >= 2 else ""
        if not nm or len(nm.split()) < 2:
            continue
        if nm not in names:
            names.append(nm)
        if st and st.lower() not in ("radny", "radna"):
            role[nm] = st
    return names, role


def scrape_sessions(cache=None):
    """Kalendarz sesji IX: numer rzymski + data z treści artykułu."""
    _, arts = _menu_articles(SESSIONS_MENU, cache)
    out = []
    for a in arts:
        title = _title_of(a) or ""
        det = _get_json(f"{API}/articles/{a['id']}", cache)
        txt = re.sub(r"<[^>]+>", "\n", det.get("content") or "")
        txt = re.sub(r"\s+", " ", unescape(txt))
        m = re.search(r"Data sesji:?\s*(\d{4}-\d{2}-\d{2})", txt)
        date = m.group(1) if m else ""
        if not date:
            continue
        rm = re.match(r"\s*([IVXLCDM]+)", title.upper())
        num = _ROM_MAP.get(rm.group(1)) if rm else None
        if date < KAD_START:
            continue
        out.append({"num": num, "date": date, "title": title,
                    "url": f"https://bip.malopolska.pl/{CONTEXT},a,{a['id']}.html"})
    out.sort(key=lambda x: x["date"])
    return out


def count_protocol_votes(cache=None):
    """Sprawdzenie, czy którykolwiek protokół IX zawiera wyniki IMIENNE (guard)."""
    hits = 0
    for mid in PROTOCOL_MENUS:
        _, arts = _menu_articles(mid, cache)
        for a in arts[:12]:
            det = _get_json(f"{API}/articles/{a['id']}", cache)
            for att in (det.get("attachments") or []):
                try:
                    raw = requests.get(f"{API}/files/{att['id']}", headers=HEADERS,
                                       timeout=90, verify=False).content
                except Exception:
                    continue
                low = raw[:3000]
                if not low.startswith(b"%PDF"):
                    continue
                try:
                    import pymupdf
                    d = pymupdf.open(stream=raw, filetype="pdf")
                    t = "".join(p.get_text() for p in d)
                except Exception:
                    continue
                if "imien" in t.lower():
                    hits += 1
    return hits


def slugify(name):
    s = unicodedata.normalize("NFKD", name.lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--city-dir", required=True)
    ap.add_argument("--cache-dir", default=None)
    ap.add_argument("--check-votes", action="store_true",
                    help="tylko diagnostyka: czy protokoły mają wyniki imienne")
    args = ap.parse_args()
    city_dir = Path(args.city_dir)
    cache = Path(args.cache_dir) if args.cache_dir else None
    docs = city_dir / "docs"
    docs.mkdir(parents=True, exist_ok=True)

    if args.check_votes:
        print("[sulkowice] protokoły z wynikami imiennymi:", count_protocol_votes(cache))
        return

    names, role = scrape_roster(cache)
    sessions = scrape_sessions(cache)
    print(f"[sulkowice] roster: {len(names)}, sessions IX: {len(sessions)}")
    if len(names) < 10 or len(sessions) < 5:
        raise SystemExit("[sulkowice] too little data — aborting")

    councilors = [{"name": n, "slug": slugify(n), "club": "", "role": role.get(n, ""),
                   "frekwencja": None, "aktywnosc": None, "votes": 0,
                   "zgodnosc_z_izba": None} for n in names]
    sess_list = [{"id": f"sesja-{s['num'] or s['date']}", "number": str(s["num"] or ""),
                  "date": s["date"], "label": s["title"] or f"Sesja {s['date']}",
                  "vote_count": 0} for s in sessions]

    kad = {"id": KADENCJA_ID, "label": KADENCJA_LABEL,
           "sessions": sess_list, "votes": [],
           "councilor_index": names, "councilors": councilors,
           "total_councilors": len(names), "total_votes": 0,
           "total_sessions": len(sess_list),
           "similarity_top": [], "similarity_bottom": []}
    (docs / f"kadencja-{KADENCJA_ID}.json").write_text(
        json.dumps(kad, ensure_ascii=False, indent=1), encoding="utf-8")

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

    data = {"city": "Sułkowice", "rada": "Rada Miejska w Sułkowicach",
            "kadencja_active": KADENCJA_ID,
            "kadencje": [{"id": KADENCJA_ID, "label": KADENCJA_LABEL}],
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "stats": {"total_votes": 0, "total_sessions": len(sess_list),
                      "total_councilors": len(names)},
            "source": {"bip": "https://bip.malopolska.pl/umsulkowice",
                       "type": "Tier-2: sklad + kalendarz sesji (Madkom BIP API)"}}
    (docs / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[sulkowice] DONE Tier-2: {len(sess_list)} sesji, 0 glosowan, {len(names)} radnych")


if __name__ == "__main__":
    main()
