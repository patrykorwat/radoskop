#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop Busko-Zdrój — Tier-2 (roster / "model berliński") scraper.

Brak głosowań imiennych online: strona Rady Miasta (umig.busko.pl, custom CMS)
publikuje protokoły sesji jako PDF-y (dl.umig.busko.pl/protokoly/ROK/...) z
głosowaniami WYŁĄCZNIE agregatami ('za – 20 głosów, przeciw – 0 ...'); brak
kategorii 'imienne wykazy głosowań', eSesja = PM dead-end (pusty sessions-list),
AlfaTV/Nefeni b.d. Miasto jako Tier-2: skład rady + kalendarz sesji IX kadencji.

Źródła:
  - Skład: /rada-miejska-top-menu/sklad-rady-miejskiej.html, sekcja
    'SKŁAD RADY MIEJSKIEJ' — role (Przewodniczący/Wiceprzewodniczący/Radny/Radna)
    + nazwisko 'Nazwisko Imię'.
  - Sesje: /rada-miejska-top-menu/program-sesji.html (paginacja ?start=N) —
    artykuły 'porządek ... sesji ... zwołanej na dzień 19 marca 2026r.' oraz
    /rada-miejska-top-menu/protokoly-z-sesji.html (sluggy 'protokol-z-posiedzenia-
    xxiii-sesji').
has_voting_data:false.
"""
import html
import json
import re
import ssl
import sys
import time
import unicodedata
import urllib.request
from datetime import datetime
from pathlib import Path

BASE = "https://www.umig.busko.pl"
ROSTER_URL = BASE + "/rada-miejska-top-menu/sklad-rady-miejskiej.html"
PROGRAM_URL = BASE + "/rada-miejska-top-menu/program-sesji.html"
PROTOKOLY_URL = BASE + "/rada-miejska-top-menu/protokoly-z-sesji.html"
KAD_START = "2024-05-07"
KAD = "2024-2029"

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE
_UA = {"User-Agent": "Mozilla/5.0 (Radoskop; info@radoskop.eu)"}

MONTHS = {"stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5,
          "czerwca": 6, "lipca": 7, "sierpnia": 8, "września": 9, "wrzesnia": 9,
          "października": 10, "pazdziernika": 10, "listopada": 11, "grudnia": 12}

ROLES = {"Przewodniczący", "Przewodnicząca", "Wiceprzewodniczący",
         "Wiceprzewodnicząca", "Radny", "Radna"}
NAME_RE = re.compile(r"^[A-ZŁŚŹŻĆŃ][a-ząćęłńóśźż]+(?:\s+[A-ZŁŚŹŻĆŃ][a-ząćęłńóśźż]+){1,2}$")
ROMAN = (r"(?:i|ii|iii|iv|v|vi|vii|viii|ix|x|xi|xii|xiii|xiv|xv|xvi|xvii|xviii|xix|"
         r"xx|xxi|xxii|xxiii|xxiv|xxv|xxvi|xxvii|xxviii|xxix|xxx|xxxi|xxxii|xxxiii|"
         r"xxxiv|xxxv)")


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


def _text_lines(raw: str) -> list[str]:
    body = re.sub(r"<(script|style).*?</\1>", "", raw, flags=re.S)
    text = html.unescape(re.sub(r"<[^>]+>", "\n", body))
    return [l.strip() for l in text.split("\n") if l.strip()]


def fetch_roster() -> list[str]:
    lines = _text_lines(_get(ROSTER_URL))
    try:
        start = max(i for i, l in enumerate(lines) if "SKŁAD RADY MIEJSKIEJ" in l.upper())
    except ValueError:
        start = 0
    names: dict[str, str] = {}
    for i in range(start, len(lines) - 1):
        if lines[i] in ROLES and NAME_RE.match(lines[i + 1]):
            parts = lines[i + 1].split()
            norm = " ".join(reversed(parts))  # 'Nazwisko Imię' -> 'Imię Nazwisko'
            names.setdefault(norm.lower(), norm)
    return sorted(names.values(), key=lambda n: n.split()[-1])


def _date_from_text(t: str):
    m = re.search(r"zwołan\w+ na dz(e|ł)e\u0144 (\d{1,2}) ([a-ząćęłńóśźż]+) (\d{4})", t, re.I) \
        or re.search(r"na dzien (\d{1,2}) ([a-z]+) (\d{4})", t, re.I)
    if m:
        mo = MONTHS.get(m.group(2).lower())
        if mo:
            return f"{m.group(3)}-{mo:02d}-{int(m.group(1)):02d}"
    m = re.search(r"z dnia (\d{1,2}) ([a-ząćęłńóśźż]+) (\d{4})", t, re.I)
    if m:
        mo = MONTHS.get(m.group(2).lower())
        if mo:
            return f"{m.group(3)}-{mo:02d}-{int(m.group(1)):02d}"
    return None


def _roman_from(t: str) -> str:
    m = re.search(ROMAN + r"[\s-]*(?:nadzwyczajnej\s+)?sesj", t, re.I)
    return m.group(0).split()[0].upper() if m else ""


def fetch_sessions() -> list[dict]:
    found: dict[str, dict] = {}
    from datetime import date
    today = date.today().isoformat()

    def add(date, roman, title):
        if not date or date < KAD_START or date > today:
            return
        cur = found.get(date)
        if cur:
            if roman and not cur["number"]:
                cur["number"] = roman
            return
        found[date] = {"date": date, "number": roman, "title": title[:80]}

    # program sesji (porządki) — paginacja ?start=N
    for start in range(0, 400, 10):
        url = PROGRAM_URL + ("" if start == 0 else f"?start={start}")
        try:
            raw = _get(url)
        except RuntimeError:
            break
        arts = sorted(set(re.findall(
            r'href="(/rada-miejska-top-menu/program-sesji/\d+-[^"]+)"', raw)))
        slugs = [(a, a.split("-", 1)[1] if "-" in a else a) for a in arts]
        new = 0
        for _href, slug in slugs:
            if "sesj" not in slug:
                continue
            d = _date_from_text(slug.replace("-", " "))
            if d and d not in found:
                new += 1
            add(d, _roman_from(slug.replace("-", " ")), slug.replace("-", " "))
        time.sleep(0.3)
        if not arts or new == 0:
            break

    # protokoły — tytuły artykułów dają numery + daty
    for start in range(0, 300, 10):
        url = PROTOKOLY_URL + ("" if start == 0 else f"?start={start}")
        try:
            raw = _get(url)
        except RuntimeError:
            break
        arts = sorted(set(re.findall(
            r'href="(/rada-miejska-top-menu/protokoly-z-sesji/\d+-[^"]+)"', raw)))
        for a in arts:
            slug = a.split("-", 1)[1] if "-" in a else a
            if "sesj" not in slug:
                continue
            d = _date_from_text(slug.replace("-", " "))
            add(d, _roman_from(slug.replace("-", " ")), slug.replace("-", " "))
        time.sleep(0.3)
        if not arts:
            break

    # uzupełnij brakujące daty z tytułów artykułów porządków (treść strony)
    for date, s in list(found.items()):
        pass
    return sorted(found.values(), key=lambda s: s["date"])


def _slug(name: str) -> str:
    t = unicodedata.normalize("NFKD", name)
    t = "".join(c for c in t if not unicodedata.combining(c)).lower().replace("ł", "l")
    t = re.sub(r"[^a-z0-9]+", "", t)
    return t or "radny"


def build(city_dir: Path) -> int:
    cfg = json.loads((city_dir / "config.json").read_text(encoding="utf-8"))
    docs = city_dir / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    names = fetch_roster()
    sessions = fetch_sessions()
    print(f"  busko-zdroj roster: {len(names)}  sessions IX: {len(sessions)}")
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
