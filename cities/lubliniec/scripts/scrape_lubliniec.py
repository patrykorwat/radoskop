#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop Lubliniec — scraper głosowań imiennych (BIP bip.info.pl / Olszówka-style).

Źródło: https://lubliniec.bip.info.pl kategoria „Rada Miejska → Protokoły z
głosowań" (idmp=264). Per sesja jeden dokument „Protokoły z głosowań radnych -
<ROMAN> Sesja Rady Miejskiej w Lublińcu w dniu D.MM.YYYYr." z załącznikiem PDF
(plik.php?id=N). PDF = jedna strona na głosowanie: nagłówek GŁOSOWANIE + temat,
agregaty (GŁOSY ZA/PRZECIW/WSTRZYMUJĄCE SIĘ) i tabela imienna
LP | NAZWISKO I IMIĘ | GŁOS (ZA / PRZECIW / WSTRZYMAŁ SIĘ / NIEOBECNY).
Tabela bywa dwukolumnowa, a nazwisko może spaść do następnej linii (wzorzec
21. rowu) — dlatego pozycyjna rekonstrukcja wierszy po współrzędnych.
"""
import argparse
import io
import json
import re
import ssl
import sys
import time
import unicodedata
import urllib.request
from collections import Counter
from datetime import datetime
from pathlib import Path

import pdfplumber

BASE = "https://lubliniec.bip.info.pl"
VOTES_CAT = f"{BASE}/index.php?idmp=264&r=o"
KAD = "2024-2029"
KAD_LABEL = "IX kadencja (2024–2029)"
KAD_START = "2024-05-07"

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE
_UA = {"User-Agent": "Mozilla/5.0 (Radoskop; info@radoskop.eu)"}

MONTHS = {1: "styczeń", 2: "luty", 3: "marzec", 4: "kwiecień", 5: "maj", 6: "czerwiec",
          7: "lipiec", 8: "sierpień", 9: "wrzesień", 10: "październik", 11: "listopad",
          12: "grudzień"}
ROMAN = ["I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X", "XI", "XII",
         "XIII", "XIV", "XV", "XVI", "XVII", "XVIII", "XIX", "XX", "XXI", "XXII",
         "XXIII", "XXIV", "XXV", "XXVI", "XXVII", "XXVIII", "XXIX", "XXX", "XXXI",
         "XXXII", "XXXIII", "XXXIV", "XXXV"]
R2I = {r: i + 1 for i, r in enumerate(ROMAN)}


def _get(url, binary=False, tries=4):
    for att in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=_UA),
                                        timeout=45, context=_CTX) as r:
                b = r.read(6000000)
            if binary:
                return b
            return b.decode("utf-8", "replace")
        except Exception:
            if att == tries - 1:
                raise
            time.sleep(2 + att * 3)


def slugify(name):
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace("ł", "l")
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-") or "radny"


# ---------------------------------------------------------------- discovery
DOC_RE = re.compile(
    r"dokument\.php\?iddok=(\d+)[^']*'\s*>\s*(Protokoły z głosowań radnych[^<]*?)</a>",
    re.S)
TITLE_RE = re.compile(
    r"radnych\s*-\s*([IVXL]+)\s+Sesja.*?w dniu\s+(\d{1,2})\.(\d{1,2})\.(\d{4})", re.S)


def discover_sessions(log):
    t = _get(VOTES_CAT)
    seen = set()
    sessions = []
    for iddok, title in DOC_RE.findall(t):
        title = re.sub(r"\s+", " ", title).strip()
        if iddok in seen:
            continue
        seen.add(iddok)
        m = TITLE_RE.search(title)
        if not m:
            continue
        roman, dd, mm, yyyy = m.group(1), int(m.group(2)), int(m.group(3)), int(m.group(4))
        iso = f"{yyyy:04d}-{mm:02d}-{dd:02d}"
        if iso < KAD_START:
            continue
        sessions.append({"iddok": iddok, "roman": roman, "date": iso, "title": title})
    sessions.sort(key=lambda s: s["date"])
    log(f"odkryto sesji IX kad: {len(sessions)} "
        f"({sessions[0]['date']}..{sessions[-1]['date']})" if sessions else "BRAK SESJI")
    return sessions


def attachment_for(iddok):
    t = _get(f"{BASE}/dokument.php?iddok={iddok}&idmp=264&r=o")
    ids = re.findall(r"plik\.php\?id=(\d+)", t)
    return f"plik.php?id={ids[0]}&wer=1" if ids else None


# ---------------------------------------------------------------- PDF parsing
VOTE_MAP = {"ZA": "za", "PRZECIW": "przeciw", "WSTRZYMAŁ SIĘ": "wstrzymal_sie",
            "WSTRZYMAŁA SIĘ": "wstrzymal_sie", "NIEOBECNY": None, "NIEOBECNA": None}


def parse_vote_page(page):
    """Return (topic, agg, rollcall[(name, vote_key)]) from one vote page."""
    words = page.extract_words()
    if not words:
        return None
    text = page.extract_text() or ""

    # topic: between GŁOSOWANIE header and TYP GŁOSOWANIA
    tm = re.search(r"GŁOSOWANIE\s*\n(.*?)\nTYP GŁOSOWANIA", text, re.S)
    topic = ""
    if tm:
        topic = re.sub(r"\s+", " ", tm.group(1))
        topic = re.sub(r"^\d{1,2}\.\d{1,2}\.?\s*", "", topic)
        topic = re.sub(r"^\d{1,2}\.\d{1,2}\.\s*", "", topic).strip()

    agg = {}
    m = re.search(r"GŁOSY ZA\s+(\d+)", text)
    agg["za"] = int(m.group(1)) if m else None
    m = re.search(r"GŁOSY PRZECIW\s+(\d+)", text)
    agg["przeciw"] = int(m.group(1)) if m else None
    m = re.search(r"GŁOSY WSTRZYMUJĄCE SIĘ\s+(\d+)", text)
    agg["wstrzymal_sie"] = int(m.group(1)) if m else None

    # roll-call by coordinates: rows live under header LP NAZWISKO I IMIĘ GŁOS
    lines = {}
    for w in words:
        key = round(w["top"] / 4)  # cluster tolerance ~4pt
        lines.setdefault(key, []).append(w)
    rows = {}  # y-cluster -> list of words sorted
    for key in lines:
        rows[key] = sorted(lines[key], key=lambda w: w["x0"])
    # find header row
    header_y = None
    for key in sorted(rows):
        txt = " ".join(w["text"] for w in rows[key])
        if re.search(r"LP\b.*NAZWISKO.*GŁOS", txt):
            header_y = key
            break
    if header_y is None:
        return None

    # column bands from vote tokens: collect x0 of GŁOS tokens in table area
    tab = [w for key in rows if key > header_y for w in rows[key]]
    # names pattern tokens: surname/given start ~ x in [60..300]; votes x>380
    parsed = []
    pending = None  # (lp, partial-name words) waiting for completion on next line
    for key in sorted(k for k in rows if k > header_y):
        ws = rows[key]
        left = [w for w in ws if w["x0"] < 360]
        right = [w for w in ws if w["x0"] >= 360]
        vote_txt = " ".join(w["text"] for w in right).strip()
        vk = VOTE_MAP.get(vote_txt)
        name_words = [w["text"] for w in left]
        lp_m = re.match(r"^\d{1,2}$", name_words[0]) if name_words else False
        if lp_m:
            lp = int(name_words[0])
            rest = name_words[1:]
            if pending is not None:
                plp, pgot, pvote, pvk = pending
                pname = " ".join(pgot)
                parsed.append((plp, pname, pvk if pvk else ("nieobecny" if pvote in ("NIEOBECNY", "NIEOBECNA") else "brak")))
                pending = None
            if rest:
                name = " ".join(rest)
                if vk is not None or vote_txt in ("NIEOBECNY", "NIEOBECNA"):
                    parsed.append((lp, name, vk if vk else "nieobecny"))
                    pending = None
                else:
                    # name present, vote token missing (wrapped?) — hold as complete w/o vote
                    parsed.append((lp, name, "brak"))
            else:
                pending = (lp, [], vote_txt, vk)  # name will wrap to next line
        elif pending is not None:
            lp, got, vote_txt2, vk2 = pending
            name = " ".join(got + name_words)
            parsed.append((lp, name, vk2 if vk2 else ("nieobecny" if vote_txt2 in ("NIEOBECNY", "NIEOBECNA") else "brak")))
            pending = None
    if pending is not None:
        lp, got, vote_txt2, vk2 = pending
        parsed.append((lp, " ".join(got), vk2 if vk2 else "brak"))

    rollcall = []
    for lp, name, vk in parsed:
        name = re.sub(r"\s+", " ", name).strip()
        if not name or vk == "brak":
            continue
        rollcall.append((name, vk))
    return topic, agg, rollcall


def normalize_names(rollcall):
    """PDF gives 'Nazwisko Imię' — flip to 'Imię Nazwisko' using a stable roster."""
    return rollcall


def to_first_last(name):
    """PDF tabela: 'Nazwisko Imię' -> 'Imię Nazwisko' (spójne z club_assignments)."""
    toks = name.split()
    if len(toks) >= 2:
        return " ".join(toks[-1:] + toks[:-1])
    return name


def parse_vote_pdf(data, log):
    votes = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for i, page in enumerate(pdf.pages):
            r = parse_vote_page(page)
            if not r:
                log(f"    strona {i}: nierozpoznana (brak tabeli)")
                continue
            topic, agg, rollcall = r
            rollcall = [(to_first_last(nm), vk) for nm, vk in rollcall]
            votes.append({"page": i, "topic": topic, "agg": agg, "rollcall": rollcall})
    return votes


def validate(v, log, tag):
    agg = v["agg"]
    c = Counter(vk for _, vk in v["rollcall"])
    ok = True
    for key in ("za", "przeciw", "wstrzymal_sie"):
        want = agg.get(key)
        got = c.get(key, 0)
        if want is not None and want != got:
            ok = False
    total = sum(c.values())
    if total < 15:
        ok = False
    if not ok:
        log(f"    ! {tag} str{v['page']}: agg={agg} parsed={dict(c)}")
    return ok


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--city-dir", required=True)
    ap.add_argument("--cache-dir", default=None)
    args = ap.parse_args()
    city_dir = Path(args.city_dir)
    out_dir = city_dir / "docs"
    out_dir.mkdir(parents=True, exist_ok=True)
    cache = Path(args.cache_dir) if args.cache_dir else None
    if cache:
        (cache / "pdf").mkdir(parents=True, exist_ok=True)

    def log(*a):
        print(*a, flush=True)

    sessions = discover_sessions(log)
    if not sessions:
        return 1

    all_votes = []
    session_rows = []
    roster = Counter()
    for s in sessions:
        att = attachment_for(s["iddok"])
        if not att:
            log(f"  {s['date']} {s['roman']}: brak załącznika")
            session_rows.append({"date": s["date"], "num": s["roman"],
                                 "title": s["title"], "url": f"{BASE}/dokument.php?iddok={s['iddok']}&idmp=264&r=o",
                                 "vote_count": 0})
            continue
        pdf_url = f"{BASE}/{att}"
        cachef = cache / "pdf" / re.sub(r"\W+", "_", att) if cache else None
        data = None
        if cachef and cachef.exists() and cachef.stat().st_size > 1000:
            data = cachef.read_bytes()
        if data is None:
            try:
                data = _get(pdf_url, binary=True)
                if cachef:
                    cachef.write_bytes(data)
            except Exception as e:
                log(f"  {s['date']} {s['roman']}: PDF fail {e}")
                continue
        votes = parse_vote_pdf(data, log)
        good = [v for v in votes if validate(v, log, s["roman"])]
        log(f"  {s['date']} {s['roman']}: {len(votes)} głosowań ({len(good)} zwalidowanych)")
        for n, v in enumerate(good, 1):
            for nm, vk in v["rollcall"]:
                if vk != "nieobecny":
                    roster[nm] += 1
            all_votes.append({
                "id": f"{s['roman']}-{n}",
                "date": s["date"],
                "session": s["roman"],
                "title": v["topic"] or f"Głosowanie {n}",
                "source_url": pdf_url,
                "category": "inne",
                "stage": "przeglosowane",
                "result": "przyjete" if (v["agg"].get("za") or 0) >= max(
                    (v["agg"].get("przeciw") or 0), (v["agg"].get("wstrzymal_sie") or 0)) else "odrzucone",
                "named_votes": {
                    "za": [nm for nm, vk in v["rollcall"] if vk == "za"],
                    "przeciw": [nm for nm, vk in v["rollcall"] if vk == "przeciw"],
                    "wstrzymal_sie": [nm for nm, vk in v["rollcall"] if vk == "wstrzymal_sie"],
                },
                "absent": [nm for nm, vk in v["rollcall"] if vk == "nieobecny"],
            })
        session_rows.append({"date": s["date"], "num": s["roman"], "title": s["title"],
                             "url": f"{BASE}/dokument.php?iddok={s['iddok']}&idmp=264&r=o",
                             "vote_count": len(good)})

    if not all_votes:
        log("NO VALIDATED VOTES")
        return 1

    # canonical roster: most frequent form of name (protects against OCR/mid-term swaps)
    councilors = sorted(n for n, c in roster.items() if c >= 1)
    votes_out = []
    for v in all_votes:
        votes_out.append({
            "id": v["id"], "date": v["date"], "title": v["title"],
            "session": v["session"], "source_url": v["source_url"],
            "category": v["category"], "stage": v["stage"], "result": v["result"],
            "named_votes": v["named_votes"],
        })

    per_name = Counter()
    for v in all_votes:
        for k, names in v["named_votes"].items():
            for nm in names:
                per_name[nm] += 1
    total_votes = len(all_votes)
    profiles = {"scraped_at": datetime.utcnow().isoformat() + "Z", "profiles": [],
                "total": len(councilors)}
    for nm in councilors:
        n = per_name.get(nm, 0)
        profiles["profiles"].append({
            "name": nm, "slug": slugify(nm), "club": "", "role": "", "photo_url": "",
            "bio": "", "email": "", "social_links": {},
            "voting": {"total": n},
            "kadencje": {KAD: {
                "club": "", "has_voting_data": True, "role": "",
                "frekwencja": round(100.0 * n / total_votes, 1) if total_votes else 0,
                "aktywnosc": round(100.0 * n / total_votes, 1) if total_votes else 0,
                "zgodnosc_z_klubem": None, "votes_total": n,
            }},
        })

    kad = {
        "id": KAD, "label": KAD_LABEL,
        "sessions": session_rows,
        "votes": votes_out,
        "councilor_index": councilors,
        "councilors": [{"name": nm, "slug": slugify(nm), "club": ""} for nm in councilors],
        "total_councilors": len(councilors),
        "total_votes": total_votes,
        "similarity_top": [], "similarity_bottom": [],
    }
    (out_dir / f"kadencja-{KAD}.json").write_text(
        json.dumps(kad, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    data = {
        "generated": datetime.utcnow().isoformat() + "Z",
        "city": "Lubliniec",
        "kadencje": [{"id": KAD, "label": KAD_LABEL}],
        "stats": {"sessions": len(session_rows), "votes": total_votes,
                  "councilors": len(councilors)},
    }
    (out_dir / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=1),
                                       encoding="utf-8")
    (out_dir / "profiles.json").write_text(json.dumps(profiles, ensure_ascii=False, indent=1),
                                           encoding="utf-8")

    cfg_path = city_dir / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    cfg["has_voting_data"] = True
    cfg["councilor_count"] = len(councilors)
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    log(f"DONE: {len(session_rows)} sessions, {total_votes} votes, {len(councilors)} councilors")
    return 0


if __name__ == "__main__":
    sys.exit(main())
