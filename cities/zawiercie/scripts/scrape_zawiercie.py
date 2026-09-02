#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop zawiercie — Tier-2 (model berliński) scraper.

Zrodla (zawiercie.bip.net.pl, platforma Nefeni Next.js — tresci w strumieniu RSC):
  * sklad rady: artykul 2243 kategoria 631 "Skład Rady Miejskiej w Zawierciu - kadencja 2024-2029"
    (HTML artykutu w self.__next_f.push payloadach; nazwiska szyku "Nazwisko Imię")
  * kalendarz sesji: kategorie miesieczne pod 675-kadencja-20242029,
    artykuly "Sesja <data>" z datami w tytulach
Brak imiennych glosow — kategoria 24 'Protokoły z głosowania na sesjach' ma tylko
archiwum 2022 r. has_voting_data:false.
"""
import datetime
import json
import re
import sys
import time
import unicodedata
from pathlib import Path
from urllib.request import Request, urlopen
import ssl

BASE = "https://zawiercie.bip.net.pl"
KAD = "2024-2029"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"}
_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE

MONTH_CATS = {
    "2024": {"677-styczen": "01"},
    "2025": {},
    "2026": {"1177-styczen": "01", "1178-luty": "02", "1179-marzec": "03",
             "1180-kwiecien": "04", "1181-maj": "05", "1182-czerwiec": "06",
             "1297-sierpien": "08"},
}


def fetch(url: str, tries: int = 3) -> str:
    err = ""
    for _ in range(tries):
        try:
            req = Request(url, headers=UA)
            return urlopen(req, timeout=30, context=_CTX).read().decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            err = str(e)
            time.sleep(1.5)
    raise RuntimeError(f"fetch failed {url}: {err}")


def _slug(name: str) -> str:
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c)).lower().replace("ł", "l")
    s = re.sub(r"[^a-z0-9]+", "", s)
    return s or "radny"


def _fix_mojibake(s: str) -> str:
    try:
        return s.encode("latin-1", "ignore").decode("utf-8", "ignore")
    except Exception:  # noqa: BLE001
        return s


def _article_paras(url: str) -> list[str]:
    """Paragrafy <p> z RSC-strumienia artykulu Next.js."""
    h = fetch(url)
    pushes = re.findall(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)</script>', h, re.S)
    blob = "".join(p.encode().decode("unicode_escape", "ignore") for p in pushes)
    out = []
    for p in re.findall(r"<p>(.*?)</p>", blob, re.S):
        t = re.sub(r"<[^>]+>", " ", p).replace("&nbsp;", " ")
        t = re.sub(r"\s+", " ", t).strip()
        if t:
            out.append(_fix_mojibake(t))
    return out


def get_roster() -> list[dict]:
    url = (BASE + "/kategorie/631-sklad-rady-miejskiej-w-zawierciu-kadencja-20242029/"
                  "artykuly/2243-sklad-rady-miejskiej-w-zawierciu-kadencja-20242029?lang=PL")
    rows = []
    for t in _article_paras(url):
        if "Rady Miejskiej" in t and ":" in t:
            lab, val = t.split(":", 1)
            val = val.strip()
            if "Przewodnicząca" in lab:
                rows.append((val, "Przewodnicząca Rady"))
            elif "Wiceprzewodnicz" in lab:
                rows.append((val, "Wiceprzewodniczący Rady"))
            elif "Członkowie" in lab:
                rows.append((val, "Radny"))
            else:
                continue
        elif re.match(r"^[A-ZŁŚŻÓŃĆĘĄŹ]", t) and "Rady" not in t and len(t.split()) <= 6:
            mid = "wygaśnięcie" in t or "obsadzenie" in t
            nm = re.split(r"\s+-\s+", t)[0].strip()
            if mid and "obsadzenie" not in t:
                continue  # wygasłe mandaty poza aktualnym składem
            rows.append((nm, "Radny"))
    # normalize 'Nazwisko Imię' -> 'Imię Nazwisko', dedupe
    seen, out = set(), []
    for nm, role in rows:
        toks = nm.split()
        if len(toks) >= 2:
            name = " ".join(toks[1:] + [toks[0]])
        else:
            name = nm
        if name in seen or not (3 < len(name) < 60):
            continue
        seen.add(name)
        out.append({"name": name, "role": role})
    if len(out) < 10:
        raise RuntimeError(f"zawiercie: roster za mały ({len(out)})")
    return out


def get_sessions() -> list[dict]:
    """Sesje z kategorii miesiecznych kadencji 2024-2029 (roki + miesiace dynamicznie)."""
    sessions = set()
    year_cats = {"676-2024-rok": "2024", "888-2025-r": "2025", "1176-2026-r": "2026"}
    monnum = {"styczen": "01", "luty": "02", "marzec": "03", "kwiecien": "04", "maj": "05",
              "czerwiec": "06", "lipiec": "07", "sierpien": "08", "wrzesien": "09",
              "pazdziernik": "10", "listopad": "11", "grudzien": "12"}
    for ycat, year in year_cats.items():
        h = fetch(BASE + "/kategorie/" + ycat + "?lang=PL")
        mcats = sorted(set(re.findall(r"/kategorie/(\d+-[a-zżźćńółęąś-]+)", h)))
        for mc in mcats:
            slugpart = re.sub(r"^\d+-", "", mc)
            mm = monnum.get(slugpart)
            if not mm:
                continue
            hm = fetch(BASE + "/kategorie/" + mc + "?lang=PL")
            for m in re.finditer(r'href="(/kategorie/\d+-[\w-]+/artykuly/\d+-[\w-]+)[^"]*"[^>]*>(.*?)</a>', hm, re.S):
                t = re.sub(r"<[^>]+>", "", m.group(2)).strip()
                if not re.match(r"^Sesja\b", t, re.I):
                    continue
                d = re.search(r"(\d{1,2})\s+\w+\s+(\d{4})", t)
                if not d:
                    continue
                iso = f"{d.group(2)}-{mm}-{int(d.group(1)):02d}"
                sessions.add((iso, t[:80]))
            time.sleep(0.25)
    out = []
    for iso, label in sorted(sessions, reverse=True):
        out.append({"date": iso, "number": label, "label": f"Sesja {iso}", "vote_count": 0})
    if len(out) < 5:
        raise RuntimeError(f"zawiercie: za malo sesji ({len(out)})")
    return out


def build(city_dir: Path) -> int:
    cfg = json.loads((city_dir / "config.json").read_text(encoding="utf-8"))
    docs = city_dir / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    roster = get_roster()
    sessions = get_sessions()
    print(f"  zawiercie roster: {len(roster)}, sessions: {len(sessions)}")
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
