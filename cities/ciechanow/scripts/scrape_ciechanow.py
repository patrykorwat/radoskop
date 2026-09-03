#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop Ciechanów — Tier-2 (roster / "model berliński") scraper.

Brak głosowań imiennych: BIP bip.umciechanow.pl NIE publikuje kategorii
"głosowania imiennе" (mapa witryny przejrzana; per-sesja tylko "Zawiadomienie"
PDF). eSesja = wildcard korporacyjny, AlfaTV/bip.net.pl brak,
ciechanow.bip.gov.pl = SSDIP shell. Miasto dodawane jako Tier-2:
skład rady (roster) + kalendarz sesji IX kadencji.

Źródła (serwerowy HTML, bez JS):
  - Skład: unia składów 6 komisji Rady Miasta (/rada_miasta/komisja_*),
    sekcja "Skład Komisji …" — nazwiska w liniach "Imię Nazwisko[-opcja]".
  - Sesje: /rada_miasta/informacja_o_sesji?page=N — artykuły per sesja z
    polem "Data spotkania: DD miesiąca YYYY roku".

has_voting_data:false, voting_display:faction (roster-mode).
"""
import json
import re
import ssl
import time
import unicodedata
import urllib.request
from collections import defaultdict
from datetime import datetime
from pathlib import Path

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE
HEADERS = {"User-Agent": "Mozilla/5.0 (Radoskop cron)"}

BIP = "https://bip.umciechanow.pl"
KAD_START = "2024-05-07"
KAD = "2024-2029"

COMMISSIONS = {
    "Komisja Gospodarki Komunalnej i Ochrony Środowiska": "/rada_miasta/komisja_gospodarki_komunalnej",
    "Komisja Rozwoju Gospodarczego i Budżetu": "/rada_miasta/komisja_rozwoju_gospodarczego",
    "Komisja Spraw Społecznych": "/rada_miasta/komisja_spraw_spolecznych",
    "Komisja Oświaty, Kultury i Sportu": "/rada_miasta/komisja_kultury_sportu",
    "Komisja Rewizyjna": "/rada_miasta/komisja_rewizyjna",
    "Komisja Skarg, Wniosków i Petycji": "/rada_miasta/Komisja_Skarg_Wnioskow_i_Petycji",
}

MONTHS = {"stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5,
          "czerwca": 6, "lipca": 7, "sierpnia": 8, "września": 9,
          "października": 10, "pazdziernika": 10, "listopada": 11, "grudnia": 12,
          # nominative variants seen on some session pages ("26 luty 2026")
          "styczeń": 1, "luty": 2, "marzec": 3, "kwiecień": 4, "maj": 5,
          "czerwiec": 6, "lipiec": 7, "sierpień": 8, "wrzesień": 9,
          "październik": 10, "listopad": 11, "grudzień": 12}

_ROM = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6, "VII": 7, "VIII": 8,
        "IX": 9, "X": 10, "XI": 11, "XII": 12, "XIII": 13, "XIV": 14, "XV": 15,
        "XVI": 16, "XVII": 17, "XVIII": 18, "XIX": 19, "XX": 20, "XXI": 21,
        "XXII": 22, "XXIII": 23, "XXIV": 24, "XXV": 25, "XXVI": 26, "XXVII": 27,
        "XXVIII": 28, "XXIX": 29, "XXX": 30, "XXXI": 31, "XXXII": 32,
        "XXXIII": 33, "XXXIV": 34, "XXXV": 35, "XXXVI": 36}

# BIP typos / spellings normalized to canonical forms
NAME_FIX = {
    "Grażyn Derbin": "Grażyna Derbin",
    "Magdalena Grelik – Grodecka": "Magdalena Grelik-Grodecka",
    "Magdalena Grelik - Grodecka": "Magdalena Grelik-Grodecka",
    "Magdalena Grelik Grodecka": "Magdalena Grelik-Grodecka",
    "Arkadiusz Chełmiński": "Arkadiusz Chełmiński",
}


def _http(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=45, context=_CTX) as r:
        return r.read().decode("utf-8", "replace")


def _slug(name):
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace("ł", "l")
    s = re.sub(r"[^a-z0-9]+", "", s)
    return s or "radny"


def fetch_roster():
    roster = defaultdict(list)
    for cname, path in COMMISSIONS.items():
        try:
            t = _http(BIP + path)
        except Exception as e:
            print(f"  [warn] {path}: {e}")
            continue
        body = t[t.find('id="tresc"'):]
        if "Redakcja podstrony" in body:
            body = body[:body.find("Redakcja podstrony")]
        txt = re.sub(r"<[^>]+>", "\n", body)
        txt = re.sub(r"[ \t\xa0]+", " ", txt)
        in_skład = False
        for l in txt.splitlines():
            l = l.strip(" .;:-")
            if not l:
                continue
            if l.startswith("Skład Komisji"):
                in_skład = True
                continue
            if not in_skład:
                continue
            if l.startswith("Roczny"):
                break
            # strip role suffixes: "Nazwisko Imię - przewodniczący"
            core = re.split(r"\s+-\s+", l)[0].strip()
            m = re.match(r"^([A-ZŚŁŻŹĆŃÓ][\wŚŁŻŹĆŃÓąęśłżźćńó]+)\s+((?:[A-ZŚŁŻŹĆŃÓ][\wŚŁŻŹĆŃÓąęśłżźćńó]+[- ]?)+)$", core)
            if not m:
                continue
            given = m.group(2).strip()
            # keep up to 2 given names (Agnieszka Maria Kuźma), drop stray words
            parts = [p for p in given.split() if len(p) > 1]
            nm = f"{m.group(1)} {' '.join(parts[:2])}".strip()
            nm = NAME_FIX.get(nm, nm)
            if m.group(1) in ("Roczny", "Komisja", "Podmiot", "Urząd", "Drukuj"):
                continue
            if cname not in roster[nm]:
                roster[nm].append(cname)
        time.sleep(0.2)
    return dict(roster)


def fetch_sessions():
    sessions = {}
    for pg in range(1, 16):
        url = BIP + "/rada_miasta/informacja_o_sesji" + (f"?page={pg}" if pg > 1 else "")
        try:
            t = _http(url)
        except Exception as e:
            print(f"  [warn] sessions page {pg}: {e}")
            break
        new = 0
        for href, title in re.findall(r'href="(https://bip\.umciechanow\.pl/rada_miasta/informacja_o_sesji/[^"]+)"\s+title="[^"]*"[^>]*>([^<]{6,90})</a>', t):
            title = title.strip()
            rm = re.match(r"^(X?[IVXL]+)\s+Sesja", title)
            if not rm or href in sessions:
                continue
            try:
                a = _http(href)
            except Exception as e:
                print(f"  [warn] {href}: {e}")
                continue
            dm = re.search(r"Data spotkania:\s*(\d{1,2})\s+(\w+)\s+(20\d\d)", a)
            if not dm or dm.group(2) not in MONTHS:
                print(f"  [skip] {title}: no date")
                continue
            date = f"{dm.group(3)}-{MONTHS[dm.group(2)]:02d}-{int(dm.group(1)):02d}"
            if date < KAD_START:
                continue
            sessions[href] = {"date": date, "num": str(_ROM.get(rm.group(1), "")), "title": title}
            new += 1
            time.sleep(0.15)
        if new == 0:
            break
        time.sleep(0.2)
    return sorted(sessions.values(), key=lambda s: s["date"])


def main():
    import sys
    city_dir = Path(sys.argv[sys.argv.index("--city-dir") + 1]) if "--city-dir" in sys.argv else Path(__file__).resolve().parent.parent
    cfg = json.loads((city_dir / "config.json").read_text(encoding="utf-8"))

    roster = fetch_roster()
    print(f"[ciechanow] roster: {len(roster)} radnych")
    sessions = fetch_sessions()
    print(f"[ciechanow] sessions IX kad: {len(sessions)}")

    clubs = cfg.get("clubs", {}) or {}
    club_assign = cfg.get("club_assignments", {}) or {}
    councilors = []
    for name in sorted(roster.keys()):
        councilors.append({"name": name, "club": club_assign.get(name, "NZ"), "district": None,
                           "frekwencja": 0.0, "aktywnosc": 0.0, "zgodnosc_z_klubem": 0.0,
                           "votes_za": 0, "votes_przeciw": 0, "votes_wstrzymal": 0,
                           "votes_brak": 0, "votes_nieobecny": 0, "votes_total": 0,
                           "rebellion_count": 0, "rebellions": [],
                           "has_activity_data": False, "activity": None,
                           "commissions": roster[name]})
    sess_data = [{"date": s["date"], "number": s["num"], "vote_count": 0,
                  "label": s["title"][:60]} for s in sessions]
    kad = {"id": KAD, "label": "IX kadencja (2024–2029)",
           "clubs": clubs,
           "sessions": sess_data, "total_sessions": len(sess_data),
           "total_votes": 0, "total_councilors": len(councilors),
           "councilors": councilors, "votes": [],
           "similarity_top": [], "similarity_bottom": []}
    data = {"generated": datetime.now().isoformat(), "default_kadencja": KAD,
            "kadencje": [{"id": KAD, "label": "IX kadencja (2024–2029)"}]}
    profiles = {"profiles": [{"name": c["name"], "slug": _slug(c["name"]),
                              "kadencje": {KAD: {
                                  "club": c["club"], "has_voting_data": False,
                                  "has_activity_data": False, "frekwencja": 0.0,
                                  "aktywnosc": 0.0, "zgodnosc_z_klubem": 0.0,
                                  "votes_za": 0, "votes_przeciw": 0, "votes_wstrzymal": 0,
                                  "votes_brak": 0, "votes_nieobecny": 0, "votes_total": 0,
                                  "rebellion_count": 0, "rebellions": [],
                                  "roles": c["commissions"], "notes": "",
                                  "former": False, "mid_term": False}}}
                             for c in councilors],
                "total": len(councilors)}

    docs = city_dir / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    (docs / f"kadencja-{KAD}.json").write_text(json.dumps(kad, ensure_ascii=False, indent=1), encoding="utf-8")
    (docs / "profiles.json").write_text(json.dumps(profiles, ensure_ascii=False, indent=1), encoding="utf-8")
    import shutil
    shutil.copy2(city_dir / "config.json", docs / "config.json")
    print(f"[ciechanow] DONE councilors={len(councilors)} sessions={len(sess_data)}")


if __name__ == "__main__":
    main()
