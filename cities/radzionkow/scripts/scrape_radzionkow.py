#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop Radzionków — Tier-2 (roster / "model berliński") scraper.

Brak imiennych głosowań online: BIP Next.js Nefeni (bip.radzionkow.pl)
w protokołach sesji publikuje JEDEN załącznik (protokół główny, tekstowy) —
wzmianki "Protokół z imiennego głosowania ... stanowi załącznik nr N"
dotyczą załączników NIEUPUBLIKOWNYCH online (kategoria protokołów ma tylko
artykuł z protokołem; kategorie 'Sesje' i 'Terminy' bez załączników imiennych;
rada.radzionkow.pl = domena parked; eSesja/Nefeni-bip.net/posiedzenia.pl b.d.).
Miasto jako Tier-2: skład rady (15 radnych) + kalendarz sesji IX kadencji.

Źródła:
  - Skład: artykuł /kategorie/953-.../artykuly/2748-radni-rady-miasta-radzionkow-kadencja-20242029
    ("Okręg wyborczy nr N / Imię Nazwisko <br> e-mail" — payload Next.js z
    escapowanymi \\u003c; 15 radnych, okręgi 1-15).
  - Sesje: kategorie 938 (Terminy sesji IX kad.) + 1011/1163/1391 (Protokoły
    2024/2025/2026) — daty sesji w slugach artykułów ('w-dniu-22-stycznia-2026-roku').

has_voting_data:false, voting_display niepotrzebny (roster z configu SPA).
"""
import json
import re
import ssl
import sys
import time
import unicodedata
import urllib.request
from datetime import datetime
from pathlib import Path

BASE = "https://bip.radzionkow.pl"
ROSTER_ART = ("/kategorie/953-radni-miasta-radzionkow-oraz-okregi-wyborcze-"
              "przez-nich-reprezentowane/artykuly/2748-radni-rady-miasta-"
              "radzionkow-kadencja-20242029")
SESSION_CATS = (
    "/kategorie/938-terminy-sesji-i-porzadek-obrad-kadencja-2024-2029",
    "/kategorie/1011-rok-2024",
    "/kategorie/1163-rok-2025",
    "/kategorie/1391-rok-2026",
)
KAD_START = "2024-05-07"
KAD = "2024-2029"

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE
_UA = {"User-Agent": "Mozilla/5.0 (Radoskop; info@radoskop.eu)"}

MONTHS = {"stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5,
          "czerwca": 6, "lipca": 7, "sierpnia": 8, "wrzesnia": 9,
          "września": 9, "pazdziernika": 10, "października": 10,
          "listopada": 11, "grudnia": 12}

ROMAN = re.compile(r"(?:^|/|_)(i{1,3}|iv|vi?|ix|x|xi{1,2}|xiv|xv|xvi|xviii|"
                   r"xix|xx|xxi|xxii|xxiii|xxiv|xxv|xxvi|xxvii|xxviii|xxix|"
                   r"xxx|xxxi|xxxii|xxxiii|xxxiv|xxxv|xxxvi)(?=-sesja|-s-|-r)",
                   re.I)


def _get(url: str) -> str:
    last = None
    for att in range(4):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=_UA),
                                        timeout=40, context=_CTX) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 + att * 3)
    raise RuntimeError(f"fetch fail {url}: {last}")


def _unescape(t: str) -> str:
    for _ in range(3):
        t = t.replace("\\\\u003c", "<").replace("\\\\u003e", ">")
        t = t.replace("\\u003c", "<").replace("\\u003e", ">")
    return t


def fetch_roster() -> list[str]:
    t = _unescape(_get(BASE + ROSTER_ART + "?lang=PL"))
    names = re.findall(
        r"<p>([A-ZŁŚŹŻ][a-ząćęłńóśźż]+(?: [A-ZŁŚŹŻ][a-ząćęłńóśźż]+){1,2})<br>"
        r".{0,40}?mailto:([\w.\-]+@radzionkow\.pl)", t)
    out: dict[str, str] = {}
    for nm, _em in names:
        nm = re.sub(r"\s+", " ", nm).strip()
        if nm not in out:
            out[nm] = nm
    return sorted(out.keys(), key=lambda n: n.split()[-1])


def _date_from_slug(slug: str):
    m = re.search(r"w-dniu-(\d{1,2})-([a-ząęłńóśźż]+)-(\d{4})-roku", slug)
    if not m:
        return None
    mo = MONTHS.get(m.group(2))
    if not mo:
        return None
    return f"{m.group(3)}-{mo:02d}-{int(m.group(1)):02d}"


def _roman_from_slug(slug: str) -> str:
    m = re.match(r"(i|ii|iii|iv|v|vi|vii|viii|ix|x|xi|xii|xiii|xiv|xv|xvi|"
                 r"xvii|xviii|xix|xx|xxi|xxii|xxiii|xxiv|xxv|xxvi|xxvii|"
                 r"xxviii|xxix|xxx|xxxi|xxxii|xxxiii|xxxiv|xxxv|xxxvi)-sesja",
                 slug, re.I)
    return m.group(1).upper() if m else ""


def fetch_sessions() -> list[dict]:
    found: dict[str, dict] = {}
    for cat in SESSION_CATS:
        for page in range(1, 6):
            q = f"{cat}?lang=PL" + ("" if page == 1 else f"&page={page}")
            try:
                t = _get(BASE + q)
            except RuntimeError:
                break
            slugs = set(re.findall(r'href="/kategorie/\d+[^"]*artykuly/\d+-([^"?]+)', t))
            new = 0
            for s in slugs:
                if "sesj" not in s:
                    continue
                d = _date_from_slug(s)
                if not d or d < KAD_START:
                    continue
                if d not in found:
                    new += 1
                found.setdefault(d, {"date": d, "number": _roman_from_slug(s),
                                     "title": s.replace("-", " ")[:80]})
            time.sleep(0.4)
            if not slugs or new == 0:
                break
    return sorted(found.values(), key=lambda s: s["date"])


def _slug(name: str) -> str:
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
    print(f"  radzionkow roster: {len(names)}  sessions IX: {len(sessions)}")
    if len(names) < 10 or len(sessions) < 5:
        raise SystemExit("za malo danych — przerywam")

    kadencja = {
        "id": KAD, "label": cfg["kadencje"][KAD]["label"], "clubs": {},
        "sessions": [{"date": s["date"], "number": s["number"], "vote_count": 0,
                      "attendee_count": None, "attendees": [], "speakers": []}
                     for s in sessions],
        "total_sessions": len(sessions), "total_votes": 0,
        "total_councilors": len(names),
        "councilors": [{"name": n, "club": "", "district": None, "frekwencja": None,
                        "aktywnosc": None, "zgodnosc_z_klubem": None, "votes_total": 0,
                        "rebellion_count": 0, "has_activity_data": False}
                       for n in names],
        "votes": [], "similarity_top": [], "similarity_bottom": [],
    }
    (docs / f"kadencja-{KAD}.json").write_text(
        json.dumps(kadencja, ensure_ascii=False, indent=1), encoding="utf-8")

    profiles = {
        "scraped_at": datetime.now().isoformat(),
        "profiles": [{"name": n, "slug": _slug(n),
                      "kadencje": {KAD: {"club": "", "has_voting_data": False,
                                         "has_activity_data": False,
                                         "former": False, "mid_term": False}}}
                     for n in names],
        "total": len(names),
    }
    (docs / "profiles.json").write_text(
        json.dumps(profiles, ensure_ascii=False, indent=1), encoding="utf-8")

    data = {"generated": datetime.now().isoformat(),
            "default_kadencja": KAD,
            "kadencje": [{"id": KAD, "label": cfg["kadencje"][KAD]["label"]}]}
    (docs / "data.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    (docs / "config.json").write_text(
        json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    city_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent
    raise SystemExit(build(city_dir))
