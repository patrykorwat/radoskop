#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop slubice — Tier-2 (roster / "model berliński") scraper.

Zrodla (bip.slubice.pl, platforma Nefeni Next.js — SERWEROWY JSON w SSR payload):
  * sklad rady: artykul 142 kategoria 33 "Informacje o Radzie Miejskiej"
    (content JSON w strumieniu RSC, pole "content" z HTML listy nazwisk)
  * protokoly sesji: kategoria 31 "Protokoły Sesji Rady Miejskiej"
    (tytuly artykulow "Protokół ... sesji ... (DD.MM.YYYY r.)")
Brak imiennych glosow — has_voting_data:false.
"""
import datetime
import json
import re
import sys
import unicodedata
from pathlib import Path
from urllib.request import Request, urlopen

BASE = "https://bip.slubice.pl"
KAD = "2024-2029"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"}
MONTHS = {"stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5, "czerwca": 6,
          "lipca": 7, "sierpnia": 8, "września": 9, "wrzesnia": 9, "października": 10,
          "pazdziernika": 10, "listopada": 11, "grudnia": 12}

ROMAN = r"X{0,9}(?:IX|IV|VI?I{0,3}|IX|X[0-9]?)"


def fetch(url: str) -> str:
    req = Request(url, headers=UA)
    return urlopen(req, timeout=30).read().decode("utf-8", "replace")


def _slug(name: str) -> str:
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c)).lower().replace("ł", "l")
    s = re.sub(r"[^a-z0-9]+", "", s)
    return s or "radny"


def get_roster() -> list[dict]:
    """Artykul 142 — sklad rady z JSON 'content' w RSC payload."""
    url = BASE + "/kategorie/33-informacje-o-radzie-miejskiej-w-slubicach/artykuly/142-sklad-rady-miejskiej-w-slubicach-ix-kadencji-lata-20242029?lang=PL"
    t = fetch(url)
    m = None
    for cand in re.finditer(r'\\"content\\":\\"(.{200,30000}?)\\",\\"slug\\"', t):
        if "Przewodnicz" in cand.group(1):
            m = cand
            break
    if not m:
        raise RuntimeError("slubice: nie znaleziono content artykulu 142")
    raw = m.group(1)
    raw = re.sub(r"\\u([0-9a-fA-F]{4})", lambda g: chr(int(g.group(1), 16)), raw)
    html = raw.replace("\\n", "\n").replace('\\"', '"').replace("\\/", "/")
    # role markers
    rows = []
    for line in re.split(r"<br\s*/?>|</p>", html):
        line = re.sub(r"<[^>]+>", "", line).strip()
        if not line:
            continue
        role = ""
        mm = re.search(r"[-–]\s*(Przewodniczący|Wiceprzewodnicz\w+)", line)
        if mm:
            role = mm.group(1)
            line = line[: mm.start()].strip()
        name = re.sub(r"\s+", " ", line).strip(" -–")
        name = name.strip("\\ ").strip()
        name = re.sub(r"\d{4}.*$", "", name).strip()
        if not (3 < len(name) < 60) or re.search(r"kadencji|Skład|Rady", name):
            continue
        # BIP podaje szyk "Nazwisko Imię [Imię]" — normalizuj do "Imię ... Nazwisko"
        toks = name.split()
        if len(toks) >= 2:
            name = " ".join(toks[1:] + [toks[0]])
        rows.append({"name": name, "role": role})
    # dedup by name
    seen, out = set(), []
    for r in rows:
        if r["name"] not in seen:
            seen.add(r["name"])
            out.append(r)
    return out


def get_sessions() -> list[dict]:
    """Kategoria 31 protokoły — daty z tytulow 'Protokół ... (DD.MM.YYYY r.)'."""
    t = fetch(BASE + "/kategorie/31-protokoly-sesji-rady-miejskiej?lang=PL")
    arts = sorted(set(re.findall(r'href="(/kategorie/31-[^"?]+/artykuly/[^"?]+)', t)))
    sessions = []
    for a in arts:
        label = ""
        lm = re.search(r'href="' + re.escape(a) + r'(?:\?[^"]*)?"[^>]*>(.*?)</a>', t, re.S)
        if lm:
            label = re.sub(r"<[^>]+>", "", lm.group(1))
            label = re.sub(r"\s+", " ", label).strip()
        dates = re.findall(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", label)
        for d, mo, y in dates:
            num = ""
            nm = re.search(r"(\d{1,2})\.", label)
            sessions.append({"date": f"{y}-{int(mo):02d}-{int(d):02d}",
                             "number": label[:80], "label": f"Protokół sesji {d}.{mo}.{y}",
                             "vote_count": 0})
    # dedup by date, sort desc
    seen, out = set(), []
    for s in sorted(sessions, key=lambda x: x["date"], reverse=True):
        if s["date"] not in seen:
            seen.add(s["date"])
            out.append(s)
    return out


def build(city_dir: Path) -> int:
    cfg = json.loads((city_dir / "config.json").read_text(encoding="utf-8"))
    docs = city_dir / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    roster = get_roster()
    sessions = get_sessions()
    print(f"  slubice roster: {len(roster)}, sessions: {len(sessions)}")
    names = [r["name"] for r in roster]
    kadencja = {
        "id": KAD, "label": cfg["kadencje"][KAD]["label"], "clubs": {},
        "sessions": sessions,
        "total_sessions": len(sessions), "total_votes": 0, "total_councilors": len(names),
        "councilors": [{"name": n, "club": "", "district": None, "frekwencja": None,
                        "aktywnosc": None, "zgodnosc_z_klubem": None, "votes_total": 0,
                        "rebellion_count": 0, "has_activity_data": False} for n in names],
        "votes": [], "similarity_top": [], "similarity_bottom": [],
    }
    (docs / f"kadencja-{KAD}.json").write_text(json.dumps(kadencja, ensure_ascii=False, indent=1), encoding="utf-8")
    now = datetime.datetime.now().isoformat()
    profiles = {
        "profiles": [{"name": r["name"], "slug": _slug(r["name"]), "role": r.get("role", ""),
                      "kadencje": {KAD: {"club": "", "has_voting_data": False,
                                         "has_activity_data": False, "former": False, "mid_term": False,
                                         "role": r.get("role", "")}}}
                     for r in roster],
        "scraped_at": now,
        "total": len(roster),
    }
    (docs / "profiles.json").write_text(json.dumps(profiles, ensure_ascii=False, indent=1), encoding="utf-8")
    data = {"generated": now, "default_kadencja": KAD,
            "kadencje": [{"id": KAD, "label": cfg["kadencje"][KAD]["label"]}]}
    (docs / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    (docs / "config.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(build(Path(__file__).resolve().parents[1]))
