#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop Rydułtowy — Tier-2 (roster / "model berliński") scraper.

Brak publicznie maszynowo czytelnych głosowań imiennych:
  - BIP bip.rydultowy.finn.pl zwraca 503 dla ruchu automatycznego (2026-09),
  - eSesja rydultowy.esesja.pl = PM-instance B (pusta .sessions-list, brak sesji),
  - portal rydultowy.pl publikuje OBWIESZCZENIA O SESJI (porządek obrad,
    termin, numer sesji) + stronę "Skład Rady Miasta 2024-2029".

Miasto jako Tier-2: skład rady (15 radnych, strona portalu 4213) + kalendarz
sesji IX kadencji z obwieszczeń (na żywo + archiwum Wayback, bo portal bywa
503). Daty sesji zbierane z nagłówków "N SESJA RADY MIASTA ... w dniu D
miesiąc RRRR" ORAZ z cross-referencji "protokołu nr X.2025 z X sesji ...
w dniu ..." w treści obwieszczeń.

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

PORTAL = "https://rydultowy.pl"
ROSTER_URL = PORTAL + "/strona-4213-sklad_rady_miasta_rydultowy_2024_2029.html"
KAD = "2024-2029"
KAD_START = "2024-05-07"
MONTHS = {"stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5,
          "czerwca": 6, "lipca": 7, "sierpnia": 8, "września": 9, "wrzesnia": 9,
          "października": 10, "pazdziernika": 10, "listopada": 11, "grudnia": 12}


def _http(url, timeout=40):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
        return r.read(6000000)


def _get(url, tries=3, delay=8):
    for a in range(tries):
        try:
            return _http(url).decode("utf-8", "replace")
        except Exception:
            if a + 1 < tries:
                time.sleep(delay)
    return None


def _iso(d, m, y):
    if m not in MONTHS:
        return None
    return f"{int(y):04d}-{MONTHS[m]:02d}-{int(d):02d}"


def fetch_roster():
    """15 radnych IX kadencji: portal na żywo, fallback = najnowszy snapshot Wayback."""
    html = _get(ROSTER_URL, tries=3)
    if not html or "Skład Rady Miasta Rydułtowy" not in html:
        # fallback: wayback
        idx = _get("http://archive.org/wayback/available?url=rydultowy.pl/"
                   "strona-4213-sklad_rady_miasta_rydultowy_2024_2029.html")
        snap = ""
        if idx:
            try:
                snap = json.loads(idx)["archived_snapshots"]["closest"]["timestamp"]
            except Exception:
                snap = ""
        if snap:
            html = _get(f"http://web.archive.org/web/{snap}id_/{ROSTER_URL}")
    if not html:
        raise RuntimeError("Nie pobrano strony Skład Rady (portal i wayback)")
    names = []
    raw = re.sub(r"<(script|style).*?</\1>", "", html, flags=re.S)
    raw = raw.replace("&nbsp;", " ")
    raw = html_module.unescape(raw)
    lines = [re.sub(r"\s+", " ", l).strip() for l in re.sub(r"<[^>]+>", "\n", raw).split("\n")]
    lines = [l for l in lines if l]
    for n, l in enumerate(lines):
        if re.match(r"^[a-złóąęńśżźć]+@rada\.rydultowy\.pl$", l.strip()):
            for back in range(1, 4):
                if n - back < 0:
                    break
                cand = lines[n - back]
                if re.match(r"^[A-ZŁÓĄĘŃŚŻŹĆ][\wŁłÓóĄąĘęŃńŚśŻżŹźĆć-]+ [A-ZŁÓĄĘŃŚŻŹĆ][\wŁłÓóĄąĘęŃńŚśŻżŹźĆć-]+$", cand):
                    if cand not in names:
                        names.append(cand)
                    break
    if len(names) < 10:
        raise RuntimeError(f"Podejrzanie mały roster: {len(names)}")
    return names


def _cdx_candidates():
    """Zrzuty wayback aktualności rydultowy.pl od startu IX kadencji."""
    txt = _get("http://web.archive.org/cdx/search/cdx?url=rydultowy.pl/aktualnosc*"
               "&from=20240507&output=json&limit=5000&collapse=urlkey&fl=original,timestamp")
    if not txt:
        return []
    try:
        rows = json.loads(txt)[1:]
    except Exception:
        return []
    return [(ts, url) for url, ts in rows
            if re.search(r"(o_b_w|obwieszczen|sesj)", url, re.I)]


def fetch_sessions():
    """Sesje IX kad: numer+data z obwieszczeń (żywo + wayback), + cross-referencje."""
    sessions = {}  # date -> number

    def harvest(html):
        body = re.sub(r"<(script|style).*?</\1>", "", html, flags=re.S)
        body = " ".join(re.sub(r"<[^>]+>", " ", body).split())
        body = body.replace("&#x20;", " ")
        # nagłówek: "w dniu 16 września 2024 r. o godz. 15:00 ... 6 SESJA RADY MIASTA"
        for m in re.finditer(
                r"w dniu (\d{1,2}) (\w+) (\d{4}) r\.? o godz\..{0,120}?(\d{1,2})\s+SESJA RADY MIASTA",
                body, re.I):
            d = _iso(m.group(1), m.group(2).lower(), m.group(3))
            if d and d >= KAD_START:
                sessions.setdefault(d, m.group(4))
        # cross-ref: "protokołu nr 22.2025 z 22 sesji ... w dniu 18 grudnia 2025 roku"
        for m in re.finditer(
                r"z (\d{1,2}) sesji[^.]{0,60}?w dniu (\d{1,2}) (\w+) (\d{4})", body, re.I):
            d = _iso(m.group(2), m.group(3).lower(), m.group(4))
            if d and d >= KAD_START:
                sessions.setdefault(d, m.group(1))

    # 1) strona-lista aktualności na żywo (jeśli portal nie zwraca 503)
    live = _get(PORTAL + "/aktualnosci-lista-strona-1.html", tries=2, delay=5)
    if live:
        for m in re.finditer(r'href="([^"]*?/aktualnosc-\d+-[\w-]*o_b_w[\w-]*\.html)"', live):
            u = re.sub(r"^.*?(/aktualnosc-)", PORTAL + r"\1", m.group(1))
            html = _get(u, tries=2, delay=4)
            if html:
                harvest(html)
    # 2) wayback: wszystkie zrzuty kandydujących aktualności
    for ts, url in _cdx_candidates():
        html = _get(f"http://web.archive.org/web/{ts}id_/{url}", tries=2, delay=4)
        if html and "SESJA RADY MIASTA" in html.upper():
            harvest(html)
        time.sleep(0.4)
    out = [{"date": d, "number": sessions[d] or "", "title": f"Sesja {sessions[d]}" if sessions.get(d) else "Sesja Rady Miasta"}
           for d in sorted(sessions)]
    return out


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
    print(f"  rydultowy roster: {len(names)}  sessions IX: {len(sessions)}")
    if not sessions:
        raise RuntimeError("Brak sesji — nie zapisuję pustej strony")

    kadencja = {
        "id": KAD, "label": cfg["kadencje"][KAD]["label"], "clubs": {},
        "sessions": [{"date": s["date"], "number": s["number"], "vote_count": 0,
                      "attendee_count": None, "attendees": [], "speakers": []} for s in sessions],
        "total_sessions": len(sessions), "total_votes": 0, "total_councilors": len(names),
        "councilors": [{"name": n, "club": "", "district": None, "frekwencja": None,
                        "aktywnosc": None, "zgodnosc_z_klubem": None, "votes_total": 0,
                        "rebellion_count": 0, "has_activity_data": True} for n in names],
        "votes": [], "similarity_top": [], "similarity_bottom": [],
    }
    (docs / f"kadencja-{KAD}.json").write_text(json.dumps(kadencja, ensure_ascii=False, indent=1), encoding="utf-8")

    profiles = {
        "scraped_at": datetime.datetime.now().isoformat(),
        "profiles": [{"name": n, "slug": _slug(n),
                      "kadencje": {KAD: {"club": "", "has_voting_data": False,
                                         "has_activity_data": True, "former": False, "mid_term": False}}}
                     for n in names],
        "total": len(names),
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
