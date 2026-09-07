#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop Darłowo — Tier-2 (roster / "model berliński") scraper.

Brak publicznie maszynowo czytelnych głosowań imiennych (stan 2026-09):
  - AlfaTV rada.darlowo.pl zwraca 403 za każdym requestem (też 2022/2023/2024
    w Wayback — serwer blokowy od lat, brak archiwalnych 200),
  - BIP um.darlowo.ibip.pl nie odpowiada (timed out, 2026-09; Wayback trzyma
    tylko zasoby statyczne),
  - darlowo.esesja.pl = wildcard (korporacyjna strona eSesja / biblioteka).

Miasto jako Tier-2 ze źródeł portalu www.darlowo.pl (WordPress REST API):
  - skład rady: artykuł "Dyżury Radnych Rady Miejskiej w 2026 roku"
    (14 radnych aktywnych) + nota o zmarłym przewodniczącym (C. Woźniak,
    zm. 08.2026 → former),
  - kalendarz sesji: kategoria "Sesje Rady Miejskiej" (cat 2610) — daty sesji
    z treści podsumowań ("...sesji, która odbyła się D miesiąc RRRR...")
    z fallbackiem na datę publikacji (posty relacjonujące bieżącą sesję).

has_voting_data:false, voting_display:faction (roster-mode).
"""
import datetime
import html as html_module
import json
import re
import ssl
import sys
import time
import unicodedata
import urllib.request
from pathlib import Path

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126 Safari/537.36"}

WP = "https://www.darlowo.pl/wp-json/wp/v2"
DYZUR_SLUG = "dyzury-radnych-rady-miejskiej-w-darlowie-w-2026-roku"
SESSION_CAT_ID = 2610  # "Sesje Rady Miejskiej"
KAD = "2024-2029"
KAD_START = "2024-05-07"
FORMER = {"Czesław Woźniak"}  # przewodniczący RM, zm. 08.08.2026 (post portalu)

MONTHS = {"stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5,
          "czerwca": 6, "lipca": 7, "sierpnia": 8, "września": 9,
          "października": 10, "listopada": 11, "grudnia": 12}
MONTH = "|".join(MONTHS)


def _get_json(url, tries=3, delay=5):
    for a in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=30, context=_CTX) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except Exception:
            if a + 1 < tries:
                time.sleep(delay)
    return None


def _plain(html_frag: str) -> str:
    raw = re.sub(r"<(script|style).*?</\1>", "", html_frag, flags=re.S)
    txt = html_module.unescape(re.sub(r"<[^>]+>", " ", raw))
    return re.sub(r"\s+", " ", txt)


def fetch_roster():
    """Skład z tabeli dyżurów (artykuł coroczny) + korekty po nazwisku."""
    d = _get_json(f"{WP}/posts?slug={DYZUR_SLUG}&_fields=content")
    if not d:
        raise RuntimeError("Nie pobrano artykułu o dyżurach radnych")
    txt = _plain(d[0]["content"]["rendered"])
    pairs = re.findall(
        r"\d{2}\.\d{2}\.\d{4} Radn\w+ ([A-ZŁŚŻ][\wąęćłńóśźż-]+(?: [A-ZŁŚŻ][\wąęćłńóśźż'’ -]+?)) w ", txt)
    names = set(re.sub(r"\s*–\s*", "-", n).strip() for n in pairs)
    # tabela "Radna Ewa Madalińsk – Marciniak" (regex tnie po mylniku)
    if "Madalińsk" in txt and not any("Madalińsk" in n for n in names):
        m = re.search(r"Madalińsk[\w-]*(?: ?-? ?Marciniak)?", txt)
        if m:
            names.add(re.sub(r"\s+", "", m.group(0)))
    names = sorted(names)
    if len(names) < 12:
        raise RuntimeError(f"Podejrzanie mały roster: {len(names)}")
    return names


def _session_date(title: str, text: str, post_date: str) -> str:
    """Data sesji: 'D miesiąca RRRR' w zdaniu o sesji, potem 'D miesiąca' (rok z posta),
    ostatecznie data publikacji (relacje publikowane w dniu sesji)."""
    year = post_date[:4]
    srcs = [text, title]
    for src in srcs:
        for m in re.finditer(rf"(\d{{1,2}}) ({MONTH}) (\d{{4}})", src):
            i = m.start()
            ctx = src[max(0, i - 160):i + 40].lower()
            if "sesj" in ctx and "uchwał" not in ctx[-25:]:
                mon = MONTHS.get(m.group(2))
                if mon:
                    d = f"{int(m.group(3)):04d}-{mon:02d}-{int(m.group(1)):02d}"
                    if KAD_START <= d:
                        return d
    for src in srcs:
        m = re.search(rf"odbył[a]? się (\d{{1,2}}) ({MONTH})", src)
        if m:
            mon = MONTHS.get(m.group(2))
            if mon:
                d = f"{year}-{mon:02d}-{int(m.group(1)):02d}"
                if KAD_START <= d <= post_date:
                    return d
    return post_date


def fetch_sessions():
    posts = _get_json(f"{WP}/posts?categories={SESSION_CAT_ID}&per_page=50"
                      f"&orderby=date&order=desc&_fields=date,link,title,content")
    if posts is None:
        raise RuntimeError("Nie pobrano kategorii Sesje Rady Miejskiej")
    sessions = {}
    for po in posts:
        post_date = po["date"][:10]
        if post_date < KAD_START:
            continue
        title = html_module.unescape(po["title"]["rendered"])
        text = _plain(po["content"]["rendered"])
        # pominąć wpisy nie-o-sesji (nagrody itp.)
        if "sesj" not in (title + text[:400]).lower():
            continue
        num = ""
        m = re.search(r"(\d{1,3})\.?\s+Sesja", title, re.I)
        if m:
            num = m.group(1)
        d = _session_date(title, text, post_date)
        sessions[d] = num
    return [{"date": d, "number": sessions[d], "title": f"Sesja {sessions[d]}" if sessions[d] else "Sesja Rady Miejskiej"}
            for d in sorted(sessions)]


def _slug(name):
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
    print(f"  darlowo roster: {len(names)}  sessions IX: {len(sessions)}")
    if not sessions:
        raise RuntimeError("Brak sesji — nie zapisuję pustej strony")

    active = [n for n in names if n not in FORMER]
    councilors = [{"name": n, "club": "", "district": None, "frekwencja": None,
                   "aktywnosc": None, "zgodnosc_z_klubem": None, "votes_total": 0,
                   "rebellion_count": 0, "has_activity_data": True} for n in names]
    kadencja = {
        "id": KAD, "label": cfg["kadencje"][KAD]["label"], "clubs": {},
        "sessions": [{"date": s["date"], "number": s["number"], "vote_count": 0,
                      "attendee_count": None, "attendees": [], "speakers": []} for s in sessions],
        "total_sessions": len(sessions), "total_votes": 0, "total_councilors": len(active),
        "councilors": councilors,
        "votes": [], "similarity_top": [], "similarity_bottom": [],
    }
    (docs / f"kadencja-{KAD}.json").write_text(json.dumps(kadencja, ensure_ascii=False, indent=1), encoding="utf-8")

    profiles = {
        "scraped_at": datetime.datetime.now().isoformat(),
        "profiles": [{"name": n, "slug": _slug(n),
                      "kadencje": {KAD: {"club": "", "has_voting_data": False,
                                         "has_activity_data": True,
                                         "former": n in FORMER, "mid_term": False}}}
                     for n in names],
        "total": len(active),
    }
    (docs / "profiles.json").write_text(json.dumps(profiles, ensure_ascii=False, indent=1), encoding="utf-8")

    data = {"generated": datetime.datetime.now().isoformat(), "default_kadencja": KAD,
            "kadencje": [{"id": KAD, "label": cfg["kadencje"][KAD]["label"]}]}
    (docs / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    (docs / "config.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    city_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent
    raise SystemExit(build(city_dir))
