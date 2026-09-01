#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop Rudnik nad Sanem — Tier-2 ("model berlinski"): sklad rady + sesje, has_voting_data=false.

Rudnik nad Sanem (powiat nizanski, podkarpackie, TERYT 1812063, QID Q1028027, pop 6565).

Zrodla (live, IX kadencja 2024-2029):
- Roster: https://rudnik.pl/dla-mieszkanca/rada-miejska/ — "Radni Rady Miejskiej
  w Rudniku nad Sanem kadencji 2024-2029", 15 radnych w <h*> 'N. Imie Nazwisko
  [- Rola]' (format IMIE Nazwisko, bez swapu).
- Sesje: BIP bip.rudnik.pl (Joomla) kategoria 162 'Uchwaly - wyniki glosowania':
  artykuly '<Rzym> sesja Rady Miejskiej' + PDF /pdf/uchwalywyniki/Protokoly DD.MM.YYYY.pdf
  (data w nazwie PDF).
- Brak glosowan imiennych do odzyskania (zweryfikowano 2026-09-01): DSSS Vote
  'WYNIKI GLOSOWANIA' per-sesja = tabele 'Za/Przeciw/Wstrzymalo sie' z markami
  GLIFOWYMI BEZ ATRYBUTÓW (span pusty w warstwie tekstowej, wektor/obraz/piksel
  nie odrozniowy) -> atrybucja per-radny nie do odczytania. Tozsame jak wzorzec
  pulsk/gora (wzorzec DSSS Vote graficzny). eSesja = wildcard, AlfaTV/Nefeni brak.

Uzycie: python scrape_rudnik-nad-sanem.py [--city-dir cities/rudnik-nad-sanem]
"""
import json
import re
import sys
import time
import unicodedata
from datetime import datetime
from pathlib import Path
import urllib.request
import html as ihtml
import ssl

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Radoskop/1.0 (info@radoskop.eu)"}

ROSTER_URL = "https://rudnik.pl/dla-mieszkanca/rada-miejska/"
BIP_CAT = "https://bip.rudnik.pl/index.php?option=com_content&view=category&layout=blog&id=162&Itemid=178&limit=200&limitstart=0"

KAD = "2024-2029"
KAD_LABEL = "IX kadencja (2024\u20132029)"
KAD_START = "2024-05-07"

# Zweryfikowany 2026-09-01 z rudnik.pl + PDF XXVII sesji (listy obecnosci 15)
FALLBACK_ROSTER = [
    ("Beata Schiffer", ""),
    ("Bogdan Kupiec", "Przewodniczący Rady Miejskiej"),
    ("Małgorzata Gancarz", ""),
    ("Bernarda Podstawek-Przybysz", ""),
    ("Tomasz Kołcz", ""),
    ("Marian Pędlowski", ""),
    ("Edward Wołoszyn", ""),
    ("Józef Długosiewicz", ""),
    ("Dawid Konior", ""),
    ("Stanisław Loryś", ""),
    ("Krzysztof Szarek", ""),
    ("Łukasz Ludian", "Wiceprzewodniczący Rady Miejskiej"),
    ("Bernadetta Bis", ""),
    ("Aneta Kasprzyk", ""),
    ("Elżbieta Maczuga", ""),
]

_LAST = 0.0


def _rate(delay=0.7):
    global _LAST
    d = time.time() - _LAST
    if d < delay:
        time.sleep(delay - d)
    _LAST = time.time()


def fetch(url, timeout=30):
    _rate()
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
        raw = r.read()
        m = re.search(rb"charset=([\w-]+)", raw[:3000], re.I)
        enc = m.group(1).decode() if m else "utf-8"
        try:
            return raw.decode(enc, "replace")
        except Exception:
            return raw.decode("utf-8", "replace")


def parse_roster(t: str):
    """'<h*> N. Imie Nazwisko - Rola</h*>' z naglowkiem 'Radni Rady Miejskiej ... kadencji 2024'."""
    idxs = [m.start() for m in re.finditer("Radni Rady Miejskiej", t)]
    i = idxs[-1] if idxs else -1
    seg = t[i:] if i >= 0 else t
    roster, roles = [], {}
    for h in re.findall(r"<h[1-6][^>]*>(.*?)</h[1-6]>", seg, re.S):
        x = re.sub(r"<[^>]+>", " ", h)
        x = ihtml.unescape(x)
        x = re.sub(r"\s+", " ", x).strip()
        m = re.match(r"^\d+\.\s+(.+)$", x)
        if not m:
            continue
        val = m.group(1).strip()
        role = ""
        if "–" in val or " - " in val:
            parts = re.split(r"\s+[–-]\s+", val, 1)
            name = parts[0].strip()
            role = parts[1].strip() if len(parts) > 1 else ""
        else:
            name = val
        if not re.match(r"^[A-ZŁŚŻŹĆŃÓĄĘ][\wŁŚŻŹĆŃÓĄĘ\-]+(\s+[A-ZŁŚŻŹĆŃÓĄĘ][\wŁŚŻŹĆŃÓĄĘ\-]+)+$", name):
            continue
        if name not in roster:
            roster.append(name)
            roles[name] = role
    return roster, roles, len(roster) >= 14


def parse_sessions(t: str):
    """Zrosla dat: (a) PDF-e 'Protokoly DD.MM.YYYY' wieszone bezposrednio w liscie
    kategorii; (b) artykuly '<Rzym> sesja...' — dla kazdego wejdz i znajdz PDF.
    Data sesji = data w nazwie PDF; cyfra rzymska z tytulu/leadu ('z XXVII sesji')."""
    sessions, seen = {}, set()
    # (a) bezposrednie PDF w liscie
    for m in re.finditer(r'href="(/pdf/uchwalywyniki/[^"]+)"', t):
        href = ihtml.unescape(m.group(1))
        dm = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", href)
        if not dm:
            continue
        iso = f"{dm.group(3)}-{dm.group(2)}-{dm.group(1)}"
        if iso < KAD_START or iso in seen:
            continue
        seen.add(iso)
        before = re.sub(r"<[^>]+>", " ", t[max(0, m.start() - 2500):m.start()])
        before = ihtml.unescape(before)
        rms = re.findall(r"([IVXLCDM]{1,7})\s+(?:nadzwyczajn\w+\s+)?sesj", before)
        rom = rms[-1] if rms else ""
        sessions[iso] = {"date": iso, "number": rom,
                         "label": f"Sesja {rom} ({iso})" if rom else f"Sesja ({iso})",
                         "url": "https://bip.rudnik.pl" + href, "vote_count": 0}
    # (b) artykuly sesji
    for m in re.finditer(r'href="(/index\.php\?option=com_content[^"]*view=article[^"]*)"[^>]*>([^<]{4,120})</a>', t):
        href, title = ihtml.unescape(m.group(1)), ihtml.unescape(m.group(2)).strip()
        if "sesj" not in title.lower():
            continue
        try:
            art = fetch("https://bip.rudnik.pl" + href)
        except Exception:
            continue
        dm = re.search(r"/pdf/uchwalywyniki/[^\"']*(\d{2})\.(\d{2})\.(\d{4})", art) or \
             re.search(r"(\d{2})\.(\d{2})\.(\d{4})", title)
        if not dm:
            continue
        iso = f"{dm.group(3)}-{dm.group(2)}-{dm.group(1)}"
        if iso < KAD_START or iso in seen:
            continue
        seen.add(iso)
        rm = re.match(r"^([IVXLCDM]+)", title)
        rom = rm.group(1) if rm else ""
        if not rom:
            r2 = re.search(r"z\s+([IVXLCDM]{1,7})\s+sesj", ihtml.unescape(art))
            rom = r2.group(1) if r2 else ""
        sessions[iso] = {"date": iso, "number": rom,
                         "label": f"Sesja {rom} ({iso})" if rom else f"Sesja ({iso})",
                         "url": "https://bip.rudnik.pl" + href, "vote_count": 0}
    out = sorted(sessions.values(), key=lambda s: s["date"], reverse=True)
    return out


def slugify(name: str) -> str:
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c)).replace("ł", "l").replace("Ł", "L")
    s = re.sub(r"[^a-z0-9]+", "", s.lower())
    return s or "radny"


def build(city_dir: Path) -> int:
    cfg = json.loads((city_dir / "config.json").read_text(encoding="utf-8"))
    docs = city_dir / "docs"
    docs.mkdir(parents=True, exist_ok=True)

    roster, roles, ok = [], {}, False
    try:
        roster, roles, ok = parse_roster(fetch(ROSTER_URL))
    except Exception as e:
        print(f"  [warn] roster live failed: {e}")
    if not ok:
        print("  [info] roster live niepelny — fallback (zweryfikowany 2026-09-01)")
        roster = [n for n, _ in FALLBACK_ROSTER]
        roles = {n: r for n, r in FALLBACK_ROSTER if r}

    councilors = []
    for n in roster:
        councilors.append({
            "name": n, "club": "",
            "role": roles.get(n) or "Radny/Radna",
            "district": None, "frekwencja": None, "aktywnosc": None,
            "zgodnosc_z_klubem": None, "votes_total": 0, "rebellion_count": 0,
            "has_activity_data": False,
        })

    sessions = []
    try:
        sessions = parse_sessions(fetch(BIP_CAT))
    except Exception as e:
        print(f"  [warn] sesje failed: {e}")
    if not sessions:
        print("  [error] 0 sessions parsed — abort (nie fabrykuje)")
        return 2

    kadencja = {
        "id": KAD, "label": KAD_LABEL, "clubs": {},
        "sessions": sessions,
        "total_sessions": len(sessions), "total_votes": 0,
        "total_councilors": len(councilors),
        "councilors": councilors,
        "votes": [], "similarity_top": [], "similarity_bottom": [],
    }
    (docs / f"kadencja-{KAD}.json").write_text(
        json.dumps(kadencja, ensure_ascii=False, indent=1), encoding="utf-8")

    profiles = {"scraped_at": datetime.now().isoformat(), "profiles": [], "total": len(councilors)}
    for c in councilors:
        profiles["profiles"].append({
            "name": c["name"], "slug": slugify(c["name"]),
            "kadencje": {
                KAD: {
                    "club": "", "role": c["role"], "has_voting_data": False,
                    "has_activity_data": False, "former": False, "mid_term": False,
                    "frekwencja": None, "aktywnosc": None, "zgodnosc_z_klubem": None,
                    "votes_total": 0, "rebellion_count": 0,
                }
            },
        })
    (docs / "profiles.json").write_text(
        json.dumps(profiles, ensure_ascii=False, indent=1), encoding="utf-8")

    data = {"generated": datetime.now().isoformat(), "default_kadencja": KAD,
            "kadencje": [{"id": KAD, "label": KAD_LABEL}]}
    (docs / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    (docs / "config.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"  rudnik-nad-sanem: roster={len(councilors)} sessions={len(sessions)} (club_assignments PENDING)")
    return 0


if __name__ == "__main__":
    if "--city-dir" in sys.argv:
        city_dir = Path(sys.argv[sys.argv.index("--city-dir") + 1])
    else:
        city_dir = Path(__file__).resolve().parent.parent
    raise SystemExit(build(city_dir))
