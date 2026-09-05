#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop Gostynin — scraper głosowań imiennych (BIP umgostynin.bip.org.pl).

Źródło: BIP Urzędu Miasta Gostynina (stary szablon bip.org.pl), kategorie
"Sesje Rady Miejskiej - ROK 2024/2025/2026" (/id/5490, /id/5853, /id/6215).
Każdy <p> to "Protokół Nr X ... z dnia D miesiąc RRRR" + link do PDF
/pliki/umgostynin/protokol*.pdf. W treści protokołu bloki:
  "... <opis> głosowano następująco: Za - 14: Nazwisko Imię, ... . Przeciw - 0
   Wstrzymało się – 0|1: Nazwisko Imię ..."
Walidacja per głosowanie: liczba nazwisk == deklarowany licznik każdej kategorii.
Roster: skład Rady Miejskiej IX kadencji ze strony miasta (gostynin.pl/579),
nazwiska w PDF w szyku "Nazwisko Imię" → mapowane na "Imię Nazwisko";
nieznane nazwiska (zmiany w trakcie kadencji) dopisywane danych-źródłowo.

Użycie: python scrape_gostynin.py [city_dir]
Zapisuje: docs/data.json, docs/kadencja-2024-2029.json, docs/profiles.json
"""
import json
import re
import ssl
import sys
import time
import unicodedata
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pymupdf

BASE = "http://umgostynin.bip.org.pl"
CATS = {"2024": "5490", "2025": "5853", "2026": "6215"}
UA = {"User-Agent": "Mozilla/5.0 Radoskop/1.0 (info@radoskop.eu)"}
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

IX_START = "2024-05-07"
MONTHS = {"stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5,
          "czerwca": 6, "lipca": 7, "sierpnia": 8, "września": 9,
          "października": 10, "listopada": 11, "grudnia": 12}

# Skład Rady Miejskiej w Gostyninie IX kadencji — BIP-panel strony miasta:
# https://www.gostynin.pl/579,sklad-rady-miejskiej (pobrano 2026-09-06)
ROSTER_IX = [
    "Krzysztof Klejna", "Konrad Krysiak", "Artur Szulwach", "Katarzyna Bryłka",
    "Łukasz Flejszer", "Anna Florczak", "Czesław Jaśkiewicz", "Iwona Markus",
    "Kamil Nowogórski", "Urszula Pieniążek", "Waldemar Pilichowicz",
    "Patryk Radzikowski", "Arkadiusz Szulczewski", "Stanisław Wróblewski",
    "Bartosz Walczak",
]
SURNAMES = {}
for _n in ROSTER_IX:
    _p = _n.split()
    SURNAMES[_p[-1].upper()] = _n


def _http(url: str) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=60, context=CTX) as r:
        return r.read()


def _html(url: str) -> str:
    raw = _http(url)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("windows-1250", "replace")


def slugify(name: str) -> str:
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace("ł", "l")
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or "radny"


def list_sessions() -> list[dict]:
    """[{date, roman, pdf_url}] z kategorii sesji 2024/2025/2026."""
    out, seen = [], set()
    for year, cid in CATS.items():
        try:
            h = _html(f"{BASE}/id/{cid}")
        except Exception as e:
            print(f"  [warn] kat {year}: {e}")
            continue
        from html import unescape
        h = unescape(h)
        pat = re.compile(
            r'<a href="(/pliki/umgostynin/[^"]+\.pdf)[^"]*"[^>]*>\s*(Protok[óo][łl][^<]{0,40})\s*</a>'
            r'[^<]*?(?:sesji|Rady[^<]*?sesji)[^<]*?z dnia (\d{1,2})\s+(\w+)\s+(\d{4})',
            re.I)
        for m in pat.finditer(h):
            pdf, _lab, d, mon, y = m.groups()
            mon = mon.lower()
            if mon not in MONTHS:
                continue
            date = f"{y}-{MONTHS[mon]:02d}-{int(d):02d}"
            url = BASE + pdf.split("?")[0]
            if url in seen:
                continue
            seen.add(url)
            rm = re.search(r"Nr\s+([IVXLCDM]+)", _lab, re.I)
            out.append({"date": date,
                        "roman": rm.group(1).upper() if rm else date,
                        "pdf_url": url})
        time.sleep(0.3)
    out = [s for s in out if s["date"] >= IX_START]
    out.sort(key=lambda s: s["date"])
    return out


NAME_END_WORDS = ("Uchwała", "Uchwal", "Oświadczenie", "Porządek", "Do punktu",
                  "Przewodniczący", "Protokół", "Wotum", "Sprawozdanie",
                  "Nagranie", "Głosowano", "Za przyjęciem", "Za podjęciem",
                  "Absolutorium", "Skarga", "Za udzieleniem", "wniosku")


def _split_names(seg: str) -> list[str]:
    """'Nazwisko Imię, Nazwisko Imię.' -> ['Nazwisko Imię', ...] (tnąc na keywordach)."""
    seg = seg.strip()
    # cut at any sentence-starting keyword
    cut = len(seg)
    for kw in NAME_END_WORDS:
        i = seg.find(kw)
        if 0 <= i < cut:
            cut = i
    seg = seg[:cut]
    seg = seg.replace(";", ",")
    names = []
    for part in seg.split(","):
        part = re.sub(r"^\s*(?:oraz|i)\s+", "", part).strip(" .:")
        part = re.sub(r"\s+", " ", part)
        if not part:
            continue
        toks = part.split()
        if len(toks) < 2 or len(toks) > 4:
            return []
        if not re.match(r"^[A-ZŁŚŻŹĆĄŃÓĘ][\włłżźćąóńś]*$", toks[0]):
            return []
        names.append(part)
    return names


def _map_name(raw: str, roster: dict) -> str:
    toks = raw.split()
    surname = toks[0].upper()
    if surname in roster:
        return roster[surname]
    # może szyk "Imię Nazwisko"?
    if len(toks) >= 2 and toks[-1].upper() in roster:
        return roster[toks[-1].upper()]
    # literówki w źródle: dopuszczamy 1 edycję
    if len(toks) >= 2:
        import difflib
        cand = difflib.get_close_matches(surname, list(roster.keys()), n=1, cutoff=0.88)
        if cand:
            roster[surname] = roster[cand[0]]
            return roster[cand[0]]
        cand = difflib.get_close_matches(toks[-1].upper(), list(roster.keys()), n=1, cutoff=0.88)
        if cand:
            roster[surname] = roster[cand[0]]
            return roster[cand[0]]
    disp = " ".join(toks[1:] + [toks[0]]) if surname[0].isupper() else raw
    roster[surname] = disp
    return disp


BLOCK_RE = re.compile(
    r"g[łl]osowano nast[ęe]puj(?:ąco|ce):(.{0,1500}?)"
    r"Wstrzy(?:ma[łl]o|muj(?:ą|e))\s+si[ęe]\s*[–-]\s*(\d+)(?::\s*(.*?))?"
    r"(?=\s*(?:Uchwała|Oświadczenie|Porządek|Do punktu|Przewodniczący|Protok[óo][łl]|"
    r"G[łl]osowanie|Przyjęcie|Nagranie|Kolejny|Kolejne|Wolne|Wolne wniosk|\n\s*\n|$))",
    re.S)
ZA_RE = re.compile(r"Za\s*[–-]\s*(\d+)(?::\s*(.*?))?\s*Przeciw\s*[–-]\s*(\d+)(?::\s*(.*?))?\s*$", re.S)


def parse_protocol(pdf_bytes: bytes, date: str, roman: str, url: str):
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    text = "".join(p.get_text() for p in doc)
    doc.close()
    text = re.sub(r"[ \t]+", " ", text)
    roster = dict(SURNAMES)
    votes = []
    for m in BLOCK_RE.finditer(text):
        head, ws_n, ws_seg = m.group(1), int(m.group(2)), m.group(3) or ""
        zm = ZA_RE.search(head.strip())
        if not zm:
            continue
        za_n, za_seg, pr_n, pr_seg = int(zm.group(1)), zm.group(2) or "", int(zm.group(3)), zm.group(4) or ""
        za_l, pr_l, ws_l = _split_names(za_seg), _split_names(pr_seg), _split_names(ws_seg)
        if len(za_l) != za_n or len(pr_l) != pr_n or len(ws_l) != ws_n:
            continue
        # topic: sentence fragment before the block
        pre = text[max(0, m.start() - 160):m.start()]
        pre = re.sub(r"\s+", " ", pre).strip()
        sent = re.split(r"[.:]", pre)[-1].strip()
        topic = sent[:200] if len(sent) > 4 else "głosowanie"
        topic = re.sub(r"^Za\s+", "za: ", topic, flags=re.I)
        votes.append({
            "topic": topic, "date": date, "roman": roman, "url": url,
            "za": [_map_name(x, roster) for x in za_l],
            "przeciw": [_map_name(x, roster) for x in pr_l],
            "wstrzymal_sie": [_map_name(x, roster) for x in ws_l],
            "counts": {"za": za_n, "przeciw": pr_n, "wstrzymal_sie": ws_n},
        })
    return votes


def main(city_dir: Path):
    docs = city_dir / "docs"
    docs.mkdir(exist_ok=True)
    sess = list_sessions()
    print(f"[gostynin] {len(sess)} sesji IX kad.")
    all_names = list(ROSTER_IX)
    sessions, votes_out = [], []
    for s in sess:
        try:
            pdf = _http(s["pdf_url"])
        except Exception as e:
            print(f"  [warn] {s['date']}: {e}")
            continue
        if pdf[:4] != b"%PDF":
            continue
        try:
            vs = parse_protocol(pdf, s["date"], s["roman"], s["pdf_url"])
        except Exception as e:
            print(f"  [warn] parse {s['date']}: {e}")
            continue
        time.sleep(0.25)
        if not vs:
            print(f"  [skip] {s['date']} sesja {s['roman']} -> 0 bloków imiennych")
            continue
        idx = {nm: n for n, nm in enumerate(all_names)}
        for v in vs:
            nv = {"za": [], "przeciw": [], "wstrzymal_sie": [], "brak_glosu": [], "nieobecni": []}
            for key in ("za", "przeciw", "wstrzymal_sie"):
                for nm in v[key]:
                    if nm not in idx:
                        idx[nm] = len(all_names)
                        all_names.append(nm)
                    nv[key].append(idx[nm])
            c = v["counts"]
            votes_out.append({
                "id": f"{v['date']}_{len(votes_out):03d}",
                "source_url": v["url"], "session_date": v["date"],
                "session_number": v["roman"], "topic": v["topic"], "druk": "None",
                "resolution": "przyjete" if c["za"] > c["przeciw"] else "odrzucone",
                "counts": {"uprawnieni": 15, **c}, "named_votes": nv,
            })
        sessions.append({"date": s["date"], "number": s["roman"],
                         "label": f"Sesja {s['roman']} ({s['date']})",
                         "vote_count": len(vs), "attendee_count": None,
                         "attendees": [], "speakers": []})
        print(f"  [ok] {s['date']} sesja {s['roman']} -> {len(vs)} głosów")
    councilors = []
    for nm in all_names:
        i = all_names.index(nm)
        z = p_ = w = 0
        for v in votes_out:
            nv = v["named_votes"]
            if i in nv["za"]:
                z += 1
            elif i in nv["przeciw"]:
                p_ += 1
            elif i in nv["wstrzymal_sie"]:
                w += 1
        tot = z + p_ + w
        councilors.append({
            "name": nm, "club": "", "district": None,
            "votes_za": z, "votes_przeciw": p_, "votes_wstrzymal": w,
            "votes_brak": 0, "votes_nieobecny": 0, "votes_total": tot,
            "frekwencja": round(100.0 * tot / len(votes_out), 1) if votes_out else None,
            "aktywnosc": None, "zgodnosc_z_klubem": None,
            "rebellion_count": 0, "has_activity_data": False,
        })
    councilors.sort(key=lambda c: -c["votes_total"])
    sessions.sort(key=lambda x: x["date"], reverse=True)
    kad = {"id": "2024-2029", "label": "IX kadencja (2024–2029)",
           "names_normalized": True, "clubs": {},
           "sessions": sessions, "total_sessions": len(sessions),
           "total_votes": len(votes_out), "total_councilors": len(all_names),
           "councilors": councilors, "votes": votes_out,
           "similarity_top": [], "similarity_bottom": [],
           "councilor_index": list(all_names)}
    (docs / "kadencja-2024-2029.json").write_text(
        json.dumps(kad, ensure_ascii=False, indent=1), encoding="utf-8")
    profiles = {"scraped_at": datetime.now(timezone.utc).isoformat(),
                "profiles": [], "total": len(councilors)}
    for c in councilors:
        profiles["profiles"].append({
            "name": c["name"], "slug": slugify(c["name"]), "club": "",
            "role": "", "photo_url": "", "bio": "", "email": "",
            "social_links": {},
            "kadencje": {"2024-2029": {
                "club": "", "frekwencja": c["frekwencja"], "aktywnosc": 0,
                "zgodnosc_z_klubem": None, "votes_za": c["votes_za"],
                "votes_przeciw": c["votes_przeciw"],
                "votes_wstrzymal": c["votes_wstrzymal"],
                "votes_brak": c["votes_brak"],
                "votes_nieobecny": c["votes_nieobecny"],
                "votes_total": c["votes_total"], "rebellion_count": 0,
                "rebellions": [], "has_voting_data": True,
                "has_activity_data": False, "former": False, "mid_term": False}},
        })
    (docs / "profiles.json").write_text(
        json.dumps(profiles, ensure_ascii=False, indent=1), encoding="utf-8")
    (docs / "data.json").write_text(json.dumps({
        "generated": datetime.now(timezone.utc).isoformat(),
        "default_kadencja": "2024-2029",
        "kadencje": [{"id": "2024-2029", "label": "IX kadencja (2024–2029)"}],
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[gostynin] DONE: {len(sessions)} sesji, {len(votes_out)} głosów, {len(all_names)} radnych")
    if not votes_out:
        sys.exit(2)


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent)
