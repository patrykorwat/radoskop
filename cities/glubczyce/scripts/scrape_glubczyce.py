#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop Głubczyce — Tier-2 (roster / 'model berliński') scraper.

Brak głosowań imiennych w żadnym źródle (sprawdzone 2026-09-07):
 - glubczyce.esesja.pl = wildcard redirect (marketing eSesja); rada.glubczyce.pl brak DNS;
   glubczyce.bip.net.pl 404; AlfaTV brak.
 - BIP bip.glubczyce.pl (skycms bip_v4): protokoły per sesja (kategoria 5658) = TEKSTOWE
   PDF-y z AGREGATAMI tylko ('Za jej przyjęciem głosowało - 16 radnych, przeciw - 0');
   rada przyznała wprost (protokół XXVIII/26) że NIE udostępnia imiennych tabel; załączniki
   = petycje/uchwały bez tabel; transmisje = transmisjaobrad.info (wideo bez głosowań).

Dane aktywności:
 - roster 21 radnych IX kad. z BIP kategoria 'Oświadczenia majątkowe — kadencja 2024-2029'
   (5837; pliki 'nazwisko-imie-YYYYMMDD.pdf', radni m.in. wg protokołów; bez burmistrza/skarbnika),
 - kalendarz sesji z dat w protokołów PDF (kategoria 5658, 'Sesji ... w dniu D MONTH YYYY').
"""
import datetime
import io
import json
import re
import ssl
import sys
import unicodedata
import urllib.request
from pathlib import Path

KAD = "2024-2029"
KAD_START = "2024-05-07"
BIP = "https://bip.glubczyce.pl"
PROTOKOLY = f"{BIP}/5658/protokoly-kadencja-2024-2029.html"
OSWIADCZENIA = f"{BIP}/5837/oswiadczenia-majatkowe-kadencja-2024-2029.html"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x64) Chrome/126.0 Safari/537.36"}
_ctx = ssl.create_default_context()
_ctx.check_hostname = False
_ctx.verify_mode = ssl.CERT_NONE

# nazwisko-imie (małe, bez diakrytyków) -> poprawna forma z protokołów/BIP
NAME_FIX = {
    "bak-bozena": "Bożena Bąk",
    "belec-michal": "Michał Belec",
    "borszowski-roman": "Roman Borszowski",
    "buczek-pawel": "Paweł Buczek",
    "fedorowicz-krzysztof": "Krzysztof Fedorowicz",
    "glogowski-czeslaw": "Czesław Głogowski",
    "kupina-kazimierz": "Kazimierz Kupina",
    "litwin-jerzy": "Jerzy Litwin",
    "monasterski-marek": "Marek Monasterski",
    "naumczyk-kazimierz": "Kazimierz Naumczyk",
    "ogar-jan": "Jan Ogar",
    "piwowar-ryszard": "Ryszard Piwowar",
    "przybylska-dorota": "Dorota Przybylska",
    "robak-wieslaw": "Wiesław Robak",
    "serwetnicki-zbigniew": "Zbigniew Serwetnicki",
    "tkacz-ryszard": "Ryszard Tkacz",
    "tomczak-anna": "Anna Tomczak",
    "woloszyn-andrzej": "Andrzej Wołoszyn",
    "wysoczanski-jan": "Jan Wysoczański",
    "zielinska-justyna": "Justyna Zielińska",
    "zwarycz-rafal": "Rafał Zwarycz",
}
# urzędnicy nie-będący radnymi (pojawiają się w protokołach)
OFFICIALS = {"Adam Krupa", "Ewa Pomes", "Bartosz Dzieża", "Joanna Tokarska"}

MONTHS = {
    "stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5, "czerwca": 6,
    "lipca": 7, "sierpnia": 8, "września": 9, "wrzesnia": 9, "pazdziernika": 10,
    "października": 10, "listopada": 11, "grudnia": 12,
}


def _get(url: str, binary: bool = False, timeout: int = 40):
    req = urllib.request.Request(url, headers=UA)
    d = urllib.request.urlopen(req, timeout=timeout, context=_ctx).read()
    return d if binary else d.decode("utf-8", "replace")


def _slug(name: str) -> str:
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c)).lower().replace("ł", "l")
    s = re.sub(r"[^a-z0-9]+", "", s)
    return s or "radny"


def fetch_roster():
    t = _get(OSWIADCZENIA)
    keys = sorted(set(re.findall(
        r"download/attachment/\d+/([a-z]+(?:-[a-z]+)*)-\d{8}", t)))
    names = []
    for k in keys:
        if k in NAME_FIX:
            names.append(NAME_FIX[k])
    return names


def fetch_sessions():
    import pdfplumber
    t = _get(PROTOKOLY)
    urls = re.findall(
        r'(https://bip\.glubczyce\.pl/download[^"]+protokol-nr-[ivx]+-sesji[^"]+\.pdf[^"]*)', t)
    out = {}
    for u in urls:
        try:
            d = _get(u, binary=True)
            with pdfplumber.open(io.BytesIO(d)) as pdf:
                head = (pdf.pages[0].extract_text() or "")[:400]
        except Exception:
            continue
        m = re.search(
            r"[Ss]esji[^\.]{0,60}?w dniu (\d{1,2})\s+([a-ząęółśżźćń]+)\.?s*\??\s*(\d{4})", head)
        if not m:
            m = re.search(r"w dniu (\d{1,2})[ .-](\d{1,2})[ .-](\d{4})", head)
            if m:
                iso = f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
            else:
                continue
        else:
            mo = MONTHS.get(m.group(2).lower())
            if not mo:
                continue
            iso = f"{m.group(3)}-{mo:02d}-{int(m.group(1)):02d}"
        if iso >= KAD_START:
            num = re.search(r"protokol-nr-([ivx]+)-sesji", u).group(1).upper()
            out[iso] = num
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
        {"date": d, "number": d, "label": f"Sesja {num} ({d})", "vote_count": 0}
        for d, num in sess
    ]
    print(f"  glubczyce roster: {len(names)} radnych, sesje IX: {len(sessions_data)}")
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
