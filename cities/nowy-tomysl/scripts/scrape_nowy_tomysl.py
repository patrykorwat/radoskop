#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop Nowy Tomyśl — Tier-2 (roster / "model berliński") scraper.

Brak głosowań imiennych w żadnym źródle (sprawdzone 2026-09-06):
 - nowy-tomysl.esesja.pl = wildcard redirect (marketing eSesja)
 - rada.nowytomysl.pl / nowy-tomysl.bip.net.pl = brak (DNS/404)
 - portal-posiedzenia.pl (nowytomysl/nowy-tomysl/tomysl) = brak anonimi sesji
 - BIP bip.nowytomysl.pl = Madkom React-SPA z API /api/menu/{id}/articles +
   /api/articles/{id}: kategoria 'Protokoły z sesji RM → Kadencja 2024-2029' (menu 1791)
   ma 1 protokół (IX sesja 2024-10-30, sam HTML bez tabel imiennych); 'Uchwały' 0 art.;
   transmisje/archiwum sesji tylko wideo hdsystem.pl/fms (stream ntomstream) — bez głosowań.

Dane aktywności:
 - roster 21 radnych IX kad. z BIP article 34880 ('Radni Rady Miejskiej kadencji 2024-2029')
 - kalendarz sesji z archiwum wideo hdsystem (strumień ntomstream; per-sesja data+tytuł).
"""
import datetime
import html as htmllib
import json
import re
import ssl
import sys
import unicodedata
import urllib.request
from pathlib import Path

KAD = "2024-2029"
KAD_START = "2024-05-07"
BIP = "https://bip.nowytomysl.pl"
RADNI_ARTICLE_ID = "34880"
HDSYSTEM = "https://hdsystem.pl/fms/video/index.php?streamName=ntomstream"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/126.0 Safari/537.36"}
_ctx = ssl.create_default_context()
_ctx_noverify = ssl.create_default_context()
_ctx_noverify.check_hostname = False
_ctx_noverify.verify_mode = ssl.CERT_NONE


def _get(url, timeout=25):
    req = urllib.request.Request(url, headers=UA)
    try:
        r = urllib.request.urlopen(req, timeout=timeout, context=_ctx)
    except Exception:
        r = urllib.request.urlopen(req, timeout=timeout, context=_ctx_noverify)
    return r.read().decode("utf-8", "replace")


def _slug(name):
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c)).lower().replace("ł", "l")
    s = re.sub(r"[^a-z0-9]+", "", s)
    return s or "radny"


def fetch_roster():
    j = json.loads(_get(f"{BIP}/api/articles/{RADNI_ARTICLE_ID}"))
    txt = htmllib.unescape(re.sub(r"<[^>]+>", " ", j["content"]))
    txt = txt.replace("\xa0", " ")
    # numbered list "1. Imię Nazwisko"
    names = re.findall(r"\d{1,2}\.\s*([A-ZŚŁŻŹĆĄĘÓŃ][\wŚŁŻŹĆĄĘÓŃćąęóśłżźćń]*(?:\s+[A-ZŚŁŻŹĆĄĘÓŃ][\wŚŁŻŹĆĄĘÓŃćąęóśłżźćń]+){1,3})", txt)
    names = [re.sub(r"\s+", " ", n).strip() for n in names]
    return names


def fetch_sessions():
    b = _get(HDSYSTEM)
    txt = htmllib.unescape(re.sub(r"<[^>]+>", " ", b)).replace("\xa0", " ")
    txt = re.sub(r"\s+", " ", txt)
    pos = [(m.start(), f"{m.group(3)}-{m.group(2)}-{m.group(1)}")
           for m in re.finditer(r"(\d{2})-(\d{2})-(\d{4})", txt)]
    out = {}
    for i, (p, iso) in enumerate(pos):
        end = pos[i + 1][0] if i + 1 < len(pos) else len(txt)
        title = txt[p + 10:end].strip(" -,")[:90]
        if "sesja" not in title.lower():
            continue
        if iso >= KAD_START and iso not in out:
            out[iso] = title
    return sorted(out.items())


def build(city_dir: Path) -> int:
    cfg = json.loads((city_dir / "config.json").read_text(encoding="utf-8"))
    docs = city_dir / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    names = fetch_roster()
    if len(names) < 10:
        raise SystemExit(f"roster za krótki ({len(names)}) — przerywam")
    sess = fetch_sessions()
    sessions_data = [
        {"date": d, "number": d, "label": f"{t} ({d})" if not t.startswith(d) else t,
         "vote_count": 0}
        for d, t in sess
    ]
    print(f"  nowy-tomysl roster: {len(names)} radnych, sesje IX: {len(sessions_data)}")
    if not sessions_data:
        raise SystemExit("brak sesji — przerywam")
    kadencja = {
        "id": KAD, "label": cfg["kadencje"][KAD]["label"], "clubs": {},
        "sessions": sessions_data,
        "total_sessions": len(sessions_data), "total_votes": 0,
        "total_councilors": len(names),
        "councilors": [{"name": n, "club": "", "district": None, "frekwencja": None,
                        "aktywnosc": None, "zgodnosc_z_klubem": None, "votes_total": 0,
                        "rebellion_count": 0, "has_activity_data": False} for n in names],
        "votes": [], "similarity_top": [], "similarity_bottom": [],
    }
    (docs / f"kadencja-{KAD}.json").write_text(
        json.dumps(kadencja, ensure_ascii=False, indent=1), encoding="utf-8")
    now = datetime.datetime.now().isoformat()
    profiles = {
        "scraped_at": now,
        "profiles": [{"name": n, "slug": _slug(n),
                      "kadencje": {KAD: {"club": "", "has_voting_data": False,
                                         "has_activity_data": False, "former": False,
                                         "mid_term": False}}}
                     for n in names],
        "total": len(names),
    }
    (docs / "profiles.json").write_text(
        json.dumps(profiles, ensure_ascii=False, indent=1), encoding="utf-8")
    data = {"generated": now, "default_kadencja": KAD,
            "kadencje": [{"id": KAD, "label": cfg["kadencje"][KAD]["label"]}]}
    (docs / "data.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    (docs / "config.json").write_text(
        json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    city_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent
    raise SystemExit(build(city_dir))
