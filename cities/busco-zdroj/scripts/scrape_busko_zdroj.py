#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop Busko-Zdrój — Tier-2 (roster / "model berliński") scraper.

Brak głosowań imiennych: protokoły sesji Rady Miejskiej (dl.umig.busko.pl
/protokoly/) zawierają wyłącznie AGREGATY ("za"–21, "przeciw"–0, "wstrzymuję
się"–0), brak tabel imiennych i kategorii "głosowania imienne".
busko-zdroj.esesja.pl = Portal Mieszkańca instance-B (sessions-list pusta,
0 listaglosowan); rada.busko-zdroj.pl / bip.net.pl — brak.

Skład: https://bip.um.busko.pl/rada-miejska-top-menu/sklad-rady-miejskiej.html
(role: Przewodniczący / Wiceprzewodniczący / Radny/Radna + nazwisko imię).
Kalendarz: https://bip.um.busko.pl/rada-miejska-top-menu/program-sesji.html
— linki porzadek_obrad_rady/RRRR/porzadek_sesji_RRRR-MM-DD.pdf.
"""
import json
import re
import ssl
import sys
import unicodedata
import urllib.request
from datetime import datetime
from pathlib import Path

BIP = "https://bip.um.busko.pl"
ROSTER_URL = f"{BIP}/rada-miejska-top-menu/sklad-rady-miejskiej.html"
SESSIONS_URL = f"{BIP}/rada-miejska-top-menu/program-sesji.html"
KAD_START = "2024-05-07"
KAD = "2024-2029"

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE
_UA = {"User-Agent": "Mozilla/5.0 (Radoskop; info@radoskop.eu)"}


def _get(url):
    import time
    for att in range(4):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=_UA),
                                        timeout=40, context=_CTX) as r:
                b = r.read(1500000)
            head = b[:2500].decode("latin-1", "replace").lower()
            enc = "cp1250" if "charset=cp1250" in head else "utf-8"
            return b.decode(enc, "replace")
        except Exception:
            if att == 3:
                raise
            time.sleep(2 + att * 3)


def _slug(name):
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace("ł", "l")
    s = re.sub(r"[^a-z0-9]+", "", s)
    return s or "radny"


NAME_RE = r"[A-ZŁŚŻŹĆĘÓĄ][\wŁŚŻŹĆĘÓĄ\-]+"


def fetch_roster():
    t = _get(ROSTER_URL)
    body = re.sub(r"<script.*?</script>", " ", t, flags=re.S)
    body = re.sub(r"<[^>]+>", " ", body)
    body = re.sub(r"\s+", " ", body)
    # page convention: after role/photo tokens come "Nazwisko Imię"
    role_pat = re.compile(
        r"(Przewodniczący|Przewodnicząca|Wiceprzewodniczący|Wiceprzewodnicząca|Radny|Radna)\s+("
        + NAME_RE + r")\s+(" + NAME_RE + r")")
    people = {}   # frozenset(surname, firstname) -> {"name": "Imię Nazwisko", "role": str}
    alt_pat = 'alt="(' + NAME_RE + ')\\s+(' + NAME_RE + ')"'
    # BIP captions/photos list "Nazwisko Imię" — swap to "Imię Nazwisko"
    for fam, first in re.findall(alt_pat, t):
        people.setdefault(frozenset((fam, first)),
                          {"name": f"{first} {fam}", "role": ""})
    # role captions also "Nazwisko Imię"; committee-section order may vary —
    # dedupe via frozenset, keep alt spelling
    for role, t1, t2 in role_pat.findall(body):
        if t1 in ("Komisji", "SKŁAD", "Komisja") or t2 in ("Komisji", "SKŁAD"):
            continue
        key = frozenset((t1, t2))
        if key not in people:
            people[key] = {"name": f"{t2} {t1}", "role": ""}
        if not people[key]["role"]:
            people[key]["role"] = role
    return {p["name"]: p["role"] for p in people.values()}


def fetch_sessions():
    t = _get(SESSIONS_URL)
    dates = sorted(set(re.findall(
        r"porzadek_sesji_(\d{4}-\d{2}-\d{2})\.pdf", t)))
    return [{"date": d, "number": i + 1}
            for i, d in enumerate(dates) if d >= KAD_START]


def build(city_dir):
    city_dir = Path(city_dir)
    cfg = json.loads((city_dir / "config.json").read_text(encoding="utf-8"))
    docs = city_dir / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    roster = fetch_roster()
    names = sorted(roster.keys(), key=lambda n: n.split()[-1])
    print(f"  roster: {len(names)}")
    if len(names) < 10:
        raise SystemExit("roster too small — abort")
    sessions = fetch_sessions()
    print(f"  sessions: {len(sessions)}")
    if len(sessions) < 5:
        raise SystemExit("too few sessions — abort")

    clubs = cfg.get("clubs", {}) or {}
    assignments = cfg.get("club_assignments", {}) or {}
    kadencja = {
        "id": KAD, "label": cfg["kadencje"][KAD]["label"],
        "clubs": {k: v.get("name", k) for k, v in clubs.items()},
        "sessions": [{"date": s["date"], "number": s["number"], "vote_count": 0,
                      "attendee_count": None, "attendees": [], "speakers": []}
                     for s in sessions],
        "total_sessions": len(sessions), "total_votes": 0,
        "total_councilors": len(names),
        "councilors": [{"name": n, "club": assignments.get(n, ""), "district": None,
                        "frekwencja": None, "aktywnosc": None,
                        "zgodnosc_z_klubem": None, "votes_total": 0,
                        "rebellion_count": 0, "has_activity_data": False}
                       for n in names],
        "votes": [], "similarity_top": [], "similarity_bottom": [],
    }
    (docs / f"kadencja-{KAD}.json").write_text(
        json.dumps(kadencja, ensure_ascii=False, indent=1), encoding="utf-8")
    profiles = {
        "scraped_at": datetime.now().isoformat(),
        "profiles": [{"name": n, "slug": _slug(n),
                      "kadencje": {KAD: {"club": assignments.get(n, ""),
                                         "role": roster.get(n, ""),
                                         "has_voting_data": False,
                                         "has_activity_data": False,
                                         "former": False, "mid_term": False}}}
                     for n in names],
        "total": len(names),
    }
    (docs / "profiles.json").write_text(
        json.dumps(profiles, ensure_ascii=False, indent=1), encoding="utf-8")
    (docs / "data.json").write_text(json.dumps({
        "generated": datetime.now().isoformat(),
        "default_kadencja": KAD,
        "kadencje": [{"id": KAD, "label": cfg["kadencje"][KAD]["label"]}]},
        ensure_ascii=False, indent=1), encoding="utf-8")
    (docs / "config.json").write_text(
        json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"  OK {len(names)} radnych, {len(sessions)} sesji")
    return 0


if __name__ == "__main__":
    city_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent
    raise SystemExit(build(city_dir))
