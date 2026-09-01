#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop Skarszewy — Tier-2 (roster / "model berliński") scraper.

BIP bip.skarszewy.pl = React-SPA Madkom z REST API:
  GET /api/menu/{id}            -> drzewo menu (dzieci zagnieżdżone w węźle)
  GET /api/menu/{id}/articles   -> lista artykułów kategorii
  GET /api/articles/{id}        -> artykuł (title, content HTML, attachments)
Brak głosowań imiennych: kategorie 'Protokoły Sesji' (1150) i 'Uchwały Rady'
(1151) są puste; imienne głosowania tylko w portalu sesji
skarszewy.posiedzenia.pl (DSSS) — za WAF (403). Skład rady (15 radnych, IX
kad.) z artykułu 'Skład Rady Miejskiej 2024-2029'; kalendarz sesji IX kad. z
tytułów 'Projekty uchwał na <R> sesję ... w dniu <data>' (kategoria 1152).

has_voting_data:false — tylko skład + kalendarz sesji.
"""
import json
import re
import ssl
import sys
import unicodedata
import urllib.request
from datetime import datetime
from html import unescape
from pathlib import Path

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE

BIP = "https://bip.skarszewy.pl"
UA = {"User-Agent": "Mozilla/5.0 Radoskop/1.0 (info@radoskop.eu)",
      "Accept": "application/json"}
KAD_START = "2024-05-07"

MONTHS = {
    "stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5,
    "czerwca": 6, "lipca": 7, "sierpnia": 8, "września": 9, "wrzesnia": 9,
    "października": 10, "pazdziernika": 10, "listopada": 11, "grudnia": 12,
}
ROMAN = re.compile(r"\b(M{0,4}(?:CM|CD|D?C{0,3})(?:XC|XL|L?X{0,3})(?:IX|IV|V?I{0,3}))\b")


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=40, context=_CTX) as r:
        return r.read()


def _getj(url: str):
    return json.loads(_get(url).decode("utf-8", "replace"))


def _slug(name: str) -> str:
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace("ł", "l")
    s = re.sub(r"[^a-z0-9]+", "", s)
    return s or "radny"


def _roman_ok(tok: str) -> bool:
    return bool(tok) and len(tok) >= 1 and ROMAN.fullmatch(tok) is not None


def fetch_roster() -> list[dict]:
    """Skład IX kadencji z artykułu BIP 'Skład Rady Miejskiej 2024-2029'."""
    arts = _getj(f"{BIP}/api/menu/1147/articles?limit=50").get("articles") or []
    art_id = None
    for a in arts:
        aid = a.get("id")
        full = _getj(f"{BIP}/api/articles/{aid}")
        if re.search(r"2024\s*[-–]\s*2029", full.get("title") or ""):
            art_id = aid
            break
    if not art_id:
        raise RuntimeError("Nie znaleziono artykułu składu IX kadencji")
    content = unescape(_getj(f"{BIP}/api/articles/{art_id}").get("content") or "")
    txt = re.sub(r"<[^>]+>", "\n", content)
    lines = [re.sub(r"\s+", " ", l.replace("\u00a0", " ")).strip() for l in txt.split("\n")]
    lines = [l for l in lines if l]
    roster = []
    NAME_RE = re.compile(r"^[A-ZĄĆĘŁŃÓŚŹŻ][A-ZĄĆĘŁŃÓŚŹŻ'\-]+( [A-ZĄĆĘŁŃÓŚŹŻ'\-]+){1,3}$")
    for i, l in enumerate(lines):
        if not NAME_RE.match(l):
            continue
        # skip section headers in caps like 'SKŁAD RADY...' — must look like a person
        if any(w in l for w in ("RADY", "SKŁAD", "KADENCJ", "KOMISJ", "PRZEWODNICZĄCY RADY MIEJSKIEJ I")):
            continue
        role = ""
        for nxt in lines[i + 1:i + 3]:
            rm = re.match(r"^-\s*(.+)$", nxt)
            if rm and "@" not in nxt:
                role = rm.group(1).strip()
                break
            if "@" in nxt:
                break
        roster.append({"name": l.title().replace("Ł", "Ł"), "role": role})
    # title() z normalizacją:保持 polskie litery
    for r_ in roster:
        r_["name"] = re.sub(r"\s+", " ", r_["name"]).strip()
    if not (13 <= len(roster) <= 25):
        raise RuntimeError(f"Suspect roster size: {len(roster)}")
    return roster


def _parse_date(s: str):
    m = re.search(r"(\d{1,2})[.](\d{1,2})[.](\d{4})", s)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return f"{y:04d}-{mo:02d}-{d:02d}"
    m = re.search(r"(\d{1,2})\s+([a-ząęłńóśźż]+)\s+(\d{4})", s)
    if m and m.group(2) in MONTHS:
        return f"{int(m.group(3)):04d}-{MONTHS[m.group(2)]:02d}-{int(m.group(1)):02d}"
    return None


def fetch_sessions() -> list[dict]:
    """Kalendarz sesji IX kad. z tytułów 'Projekty uchwał na <R> sesję ... w dniu <data>'.

    Lista artykułów zwraca title=null — tytuł jest tylko w linku (slug) albo
    w pełnym artykule. Parsujemy link; gdy brak daty w slugu i link wygląda na
    sesję, doczytujemy pełny artykuł (tylko dla kandydatów).
    """
    sessions: dict[str, dict] = {}
    offset = 0
    while offset < 300:
        r = _getj(f"{BIP}/api/menu/1152/articles?limit=100&offset={offset}")
        arts = r.get("articles") or []
        if not arts:
            break
        for a in arts:
            title = a.get("title") or ""
            if not title:
                for al in (a.get("aliasFields") or []):
                    if al.get("alias") == "title" and al.get("value"):
                        title = al["value"]
                        break
            if not title:
                for cf in (a.get("columnFields") or []):
                    v = cf.get("value") or ""
                    if v and v not in ("<br/>",) and not re.match(r"^\d{4}-\d{2}", v):
                        title = v
                        break
            link = (a.get("link") or a.get("actualLink") or "")
            source = title or link
            low = source.lower()
            if "sesj" not in low:
                continue
            date = _parse_date(source)
            if not date and not title:
                # data zapisana bez separatorów sluga ('w-dniu-25-08-2026') — spróbuj z linku
                m = re.search(r"(\d{2})-(\d{2})-(\d{4})", link)
                if m:
                    date = f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
            if not date or date < KAD_START:
                continue
            head = re.split(r"sesj", low)[0]
            num = ""
            toks = re.split(r"[^a-z]+", head)
            for i2, tok in enumerate(toks):
                is_roman = len(tok) >= 2 and re.fullmatch(r"m{0,3}(?:cm|cd|d?c{0,3})(?:xc|xl|l?x{0,3})(?:ix|iv|v?i{0,3})", tok)
                is_short = len(tok) == 1 and tok in "ivxlcdm" and i2 > 0 and toks[i2 - 1] == "na"
                if is_roman or is_short:
                    num = tok.upper()
            if date not in sessions:
                sessions[date] = {"date": date, "number": num}
        if len(arts) < 100:
            break
        offset += 100
    # sesje bez daty w tytule pomijamy (nie inventujemy)
    return sorted(sessions.values(), key=lambda s: s["date"])


def build(city_dir: Path) -> int:
    cfg_path = city_dir / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    kad = cfg["kadencja_active"]
    docs = city_dir / "docs"
    docs.mkdir(parents=True, exist_ok=True)

    roster = fetch_roster()
    sessions = fetch_sessions()
    print(f"  roster: {len(roster)}  sessions: {len(sessions)}")
    if not sessions or sessions[-1]["date"] < "2026-01-01":
        print("  [warn] kalendarz sesji wygląda na nieświeży")

    names = sorted((r["name"] for r in roster))
    kadencja = {
        "id": kad,
        "label": cfg["kadencje"][kad]["label"],
        "clubs": {},
        "sessions": [
            {"date": s["date"], "number": s["number"], "vote_count": 0,
             "attendee_count": None, "attendees": [], "speakers": []}
            for s in sessions
        ],
        "total_sessions": len(sessions),
        "total_votes": 0,
        "total_councilors": len(names),
        "councilors": [
            {"name": n, "club": "", "district": None,
             "frekwencja": None, "aktywnosc": None, "zgodnosc_z_klubem": None,
             "votes_total": 0, "rebellion_count": 0, "has_activity_data": False}
            for n in names
        ],
        "votes": [],
        "similarity_top": [],
        "similarity_bottom": [],
    }
    (docs / f"kadencja-{kad}.json").write_text(
        json.dumps(kadencja, ensure_ascii=False, indent=1), encoding="utf-8")

    roles = {r["name"]: r["role"] for r in roster if r["role"]}
    profiles = {
        "scraped_at": datetime.now().isoformat(),
        "profiles": [
            {"name": n, "slug": _slug(n),
             "kadencje": {kad: {"club": "", "role": roles.get(n, ""),
                                "has_voting_data": False,
                                "has_activity_data": False,
                                "former": False, "mid_term": False}}}
            for n in names
        ],
        "total": len(names),
    }
    (docs / "profiles.json").write_text(
        json.dumps(profiles, ensure_ascii=False, indent=1), encoding="utf-8")

    data = {
        "generated": datetime.now().isoformat(),
        "default_kadencja": kad,
        "kadencje": [{"id": kad, "label": cfg["kadencje"][kad]["label"]}],
    }
    (docs / "data.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    (docs / "config.json").write_text(
        json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    city_dir = Path(__file__).resolve().parent.parent
    if len(sys.argv) > 1:
        city_dir = Path(sys.argv[1])
    raise SystemExit(build(city_dir))
