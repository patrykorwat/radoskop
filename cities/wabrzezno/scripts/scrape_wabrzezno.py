#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop Wąbrzeźno — scraper głosowań imiennych (BIP mojregion, ZIP-y raportów).

Rada Miasta Wąbrzeźno publikuje na BIP mst-wabrzezno.rbip.mojregion.info
kategoria 'Transmisje z sesji i raporty z głosowań → IX Kadencja' (#507),
w niej ZIP-y 'Raporty z głosowań <data>'. W ZIP-ie per-glosowanie
'Raport Glosowania N.pdf' (TEKSTOWY): naglowek 'Numer: XX/186/2026
Data glosowania: 24 czerwiec 2026', 'Tekst pytania:', agregaty
'ZA n / PRZECIW n / WSTRZYMALO SIE n', 'Uprawnionych do glosowania: n',
'Glosowalo: n', a 'Lista glosujacych' = wiersze 'Imie Nazwisko [TAK|NIE]
[data] [czas] ZA|PRZECIW|WSTRZYMAL SIE' + tryb NIEOBECNY rozbity na
'NIEOBEC / Nazwisko / NY'. Walidacja: licznik zgadza sie z agregatami.

Dodane 2026-09-07 (cron ekspansja 500).
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
import zipfile
from pathlib import Path

try:
    import pdfplumber
except ImportError:  # pragma: no cover
    print("brak pdfplumber", file=sys.stderr)
    raise

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE
BIP = "https://mst-wabrzezno.rbip.mojregion.info"
KAD = "2024-2029"
KAD_START = "2024-05-07"

MONTHS = {m: i for i, m in enumerate(
    "styczeń luty marzec kwiecień maj czerwiec lipiec sierpień wrzesień "
    "październik listopad grudzień".split(), 1)}

ZIP_RE = re.compile(r'href="(https://mst-wabrzezno\.rbip\.mojregion\.info/'
                    r'download/attachment/\d+/[^"]+\.zip[^"]*)"')
DATE_IN_NAME_RE = re.compile(r'(\d{1,2})[-.]?(\d{2})[-.]?(\d{4})|(\d{1,2})[- ]([a-ząćęłńóśźż]+)[- ](\d{4})', re.I)

HDR_NUM_RE = re.compile(r'Numer:\s*([IVXLCDM]+)/(\d+)/(\d{4})')
HDR_DATE_RE = re.compile(r'Data głosowania:\s*(\d{1,2})\s+([a-ząćęłńóśźż]+)\s+(\d{4})', re.I)
ASK_RE = re.compile(r'Tekst pytania:(.*?)(?=\nWYNIKI|\Z)', re.S)
ZA_RE = re.compile(r'^ZA\s+(\d+)\s*$', re.M)
PRZ_RE = re.compile(r'^PRZECIW\s+(\d+)\s*$', re.M)
WSZ_RE = re.compile(r'^WSTRZYMAŁO SI[ĘE]\s+(\d+)\s*$', re.M)
UPR_RE = re.compile(r'Uprawnionych do głosowania:\s*(\d+)')
GLO_RE = re.compile(r'Głosowało:\s*(\d+)')
ADOPT_RE = re.compile(r'(Uchwała została uchwalona|nie została uchwalona|większość nie została osiągnięta)', re.I)
VOTE_LINE_RE = re.compile(
    r'^([A-ZŁŚŻ][\wŁŚŻćęłńóśźż-]+(?: [A-ZŁŚŻ][\wŁŚŻćęłńóśźż-]+)+?) '
    r'(TAK|NIE)((?: \d{2}\.\d{2}\.\d{4})?(?: \d{2}:\d{2}:\d{2})?) '
    r'(ZA|PRZECIW|WSTRZYMAL SI[ĘE]|WSTRZYMAŁ SI[ĘE])\s*$')
NIEOBEC_SPLIT_RE = re.compile(r'^NIEOBEC\s*$\s*^([A-ZŁŚŻ][\wŁŚŻćęłńóśźż-]+(?: [A-ZŁŚŻ][\wŁŚŻćęłńóśźż-]+)+?)\s*$\s*^NY\s*$', re.M)
NIEOBEC_INLINE_RE = re.compile(r'^([A-ZŁŚŻ][\wŁŚŻćęłńóśźż-]+(?: [A-ZŁŚŻ][\wŁŚŻćęłńóśźż-]+)+?) NIEOBECNY?\s*$', re.M)


def http_get(url: str, timeout: int = 45) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Radoskop cron)"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
                return r.read()
        except Exception:
            if attempt == 3:
                raise
            time.sleep(2 * (attempt + 1))


def http_get_text(url: str) -> str:
    raw = http_get(url)
    return raw.decode("utf-8", errors="replace")


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c)).lower().replace("ł", "l")
    return re.sub(r"[^a-z0-9]", "", s)


def _slug(name: str) -> str:
    return _norm(name.replace("-", "")) or "radny"


def _date_from_zip_name(name: str):
    m = DATE_IN_NAME_RE.search(name)
    if not m:
        return None
    if m.group(1):
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    else:
        d, mo, y = int(m.group(4)), MONTHS.get(m.group(5).lower(), 0), int(m.group(6))
    if not mo:
        return None
    return f"{y}-{mo:02d}-{d:02d}"


def list_session_zips() -> list[dict]:
    txt = http_get_text(f"{BIP}/507/ix-kadencja-rady-miasta.html")
    out, seen = [], set()
    for href in ZIP_RE.findall(txt):
        clean = href.split("?")[0]
        if clean in seen:
            continue
        seen.add(clean)
        fn = clean.rsplit("/", 1)[-1]
        date = _date_from_zip_name(fn)
        if date and date >= KAD_START:
            out.append({"url": href, "date": date})
    out.sort(key=lambda r: r["date"])
    return out


def parse_raport(pdf_bytes: bytes) -> dict | None:
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        full = "\n".join((p.extract_text() or "") for p in pdf.pages)
    if "Lista głosujących" not in full:
        return None
    hd = HDR_NUM_RE.search(full)
    dd = HDR_DATE_RE.search(full)
    ask = ASK_RE.search(full)
    za = int(ZA_RE.search(full).group(1)) if ZA_RE.search(full) else None
    prze = int(PRZ_RE.search(full).group(1)) if PRZ_RE.search(full) else None
    wsz = int(WSZ_RE.search(full).group(1)) if WSZ_RE.search(full) else None
    upr = int(UPR_RE.search(full).group(1)) if UPR_RE.search(full) else None
    glo = int(GLO_RE.search(full).group(1)) if GLO_RE.search(full) else None
    title = re.sub(r"\s+", " ", ask.group(1)).strip() if ask else ""
    vote_num = f"{hd.group(1)}/{hd.group(2)}" if hd else ""
    # session number from header Roman numeral (XX/186/2026 -> sesja XX)
    sesja = hd.group(1) if hd else ""

    body = full.split("Lista głosujących", 1)[1]
    body = NIEOBEC_SPLIT_RE.sub(r"\1 NIEOBECNY", body)
    votes = {"za": [], "przeciw": [], "wstrzymal_sie": [], "nieobecny": []}
    roster = []
    for ln in body.splitlines():
        ln = ln.strip()
        m = VOTE_LINE_RE.match(ln)
        if m:
            name = re.sub(r"\s+", " ", m.group(1))
            how = m.group(4)
            key = ("za" if how == "ZA" else
                   "przeciw" if how == "PRZECIW" else "wstrzymal_sie")
            votes[key].append(name)
            if name not in roster:
                roster.append(name)
            continue
        m2 = NIEOBEC_INLINE_RE.match(ln)
        if m2:
            name = re.sub(r"\s+", " ", m2.group(1))
            votes["nieobecny"].append(name)
            if name not in roster:
                roster.append(name)
    # walidacja agregatów
    if za is not None and (len(votes["za"]) != za or len(votes["przeciw"]) != prze
                           or len(votes["wstrzymal_sie"]) != wsz):
        return {"_bad": True, "_title": title, "_got": {k: len(v) for k, v in votes.items()},
                "_agg": [za, prze, wsz], "_vote_num": vote_num}
    return {"title": title, "vote_num": vote_num, "sesja": sesja,
            "date": (f"{dd.group(3)}-{MONTHS[dd.group(2).lower()]:02d}-{int(dd.group(1)):02d}" if dd else ""),
            "za": za or 0, "przeciw": prze or 0, "wstrzymal_sie": wsz or 0,
            "uprawnieni": upr, "glosowalo": glo,
            "status": "przyjete" if ADOPT_RE.search(full) and "nie została" not in (ADOPT_RE.search(full).group(0) if ADOPT_RE.search(full) else "") else None,
            "votes": votes, "roster": roster}


def build(output: Path, profiles_path: Path, cache_dir: Path):
    zips = list_session_zips()
    print(f"ZIP-ów IX: {len(zips)}")
    sessions = []
    all_votes = []
    roster_all: list[str] = []
    councilor_clubs: dict[str, str] = {}
    for zv in zips:
        cache = cache_dir / re.sub(r"[^A-Za-z0-9.-]", "_", zv["url"].split("?")[0].rsplit("/", 1)[-1] + ".zip")
        if cache.exists():
            raw = cache.read_bytes()
        else:
            raw = http_get(zv["url"])
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_bytes(raw)
            time.sleep(0.4)
        z = zipfile.ZipFile(io.BytesIO(raw))
        pdfs = sorted((n for n in z.namelist() if n.lower().endswith(".pdf")),
                      key=lambda n: (len(n), n))
        reps = []
        bad = 0
        for n in pdfs:
            try:
                rep = parse_raport(z.read(n))
            except Exception as e:
                print(f"  ERR {n}: {e}")
                rep = None
            if rep is None:
                continue
            if rep.get("_bad"):
                bad += 1
                print(f"  AGREGAT-MISMATCH {zv['date']} {n}: {_got(rep['_got'])} vs {rep['_agg']}")
                continue
            reps.append(rep)
        if not reps:
            print(f"  {zv['date']}: 0 raportów, pomijam")
            continue
        sesja_no = max((r["sesja"] for r in reps if r["sesja"]), key=lambda s: len(s)) if any(r["sesja"] for r in reps) else ""
        sess = {"date": zv["date"], "number": sesja_no or zv["date"],
                "label": f"Sesja {('Rady Miasta ' + sesja_no) if sesja_no else ''} ({zv['date']})".replace("  ", " "),
                "vote_count": len(reps)}
        sessions.append(sess)
        for i, rep in enumerate(reps, 1):
            vid = f"{zv['date'].replace('-', '')}-{i}"
            all_votes.append({
                "id": vid, "session": sess["number"], "date": zv["date"],
                "title": rep["title"][:300] or f"Głosowanie {rep['vote_num']}",
                "za": rep["za"], "przeciw": rep["przeciw"], "wstrzymal_sie": rep["wstrzymal_sie"],
                "status": rep["status"] or "—" if rep["status"] else "—",
                "named_votes": {"za": rep["votes"]["za"], "przeciw": rep["votes"]["przeciw"],
                                 "wstrzymal_sie": rep["votes"]["wstrzymal_sie"]},
            })
            for nm in rep["roster"]:
                if nm not in roster_all:
                    roster_all.append(nm)
        print(f"  {zv['date']}: {len(reps)} glosowan (zle: {bad})")

    councilor_index = roster_all
    councilors = [{"name": n, "slug": _slug(n), "club": "", "id": idx}
                  for idx, n in enumerate(roster_all)]
    kad = {"id": KAD, "label": "IX kadencja (2024–2029)",
           "sessions": sessions, "votes": all_votes,
           "councilor_index": councilor_index, "councilors": councilors,
           "total_councilors": len(roster_all), "total_votes": len(all_votes),
           "similarity_top": [], "similarity_bottom": []}
    # profiles
    prof = []
    per = {n: {"za": 0, "przeciw": 0, "wsz": 0, "obecnosc": 0} for n in roster_all}
    for v in all_votes:
        for k, key in (("za", "za"), ("przeciw", "przeciw"), ("wstrzymal_sie", "wsz")):
            for n in v["named_votes"][k]:
                if n in per:
                    per[n][key] += 1
        voted = sum(len(v["named_votes"][x]) for x in ("za", "przeciw", "wstrzymal_sie"))
        for n in roster_all:
            if n in per and per[n]["za"] + per[n]["przeciw"] + per[n]["wsz"] > 0:
                pass
    total_v = len(all_votes)
    for n in roster_all:
        glos = per[n]["za"] + per[n]["przeciw"] + per[n]["wsz"]
        frekw = round(100 * glos / total_v, 1) if total_v else 0
        prof.append({"name": n, "slug": _slug(n), "club": "", "role": "",
                     "photo_url": "", "bio": "", "email": "", "social_links": {},
                     "voting": None,
                     "kadencje": {KAD: {"club": "", "has_voting_data": True, "role": "",
                                         "frekwencja": frekw, "aktywnosc": 0,
                                         "zgodnosc_z_klubem": 0,
                                         "glosow_za": per[n]["za"],
                                         "glosow_przeciw": per[n]["przeciw"],
                                         "glosow_wstrzymane": per[n]["wsz"]}}})
    data = {"scraped_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "city": "Wąbrzeźno", "kadencja_active": KAD,
            "kadencje": [{"id": KAD, "label": "IX kadencja (2024–2029)"}],
            "stats": {"sessions": len(sessions), "votes": len(all_votes),
                      "councilors": len(roster_all)}}
    output.parent.mkdir(parents=True, exist_ok=True)
    (output.parent / f"kadencja-{KAD}.json").write_text(
        json.dumps(kad, ensure_ascii=False, indent=1), encoding="utf-8")
    profiles_path.write_text(json.dumps({"scraped_at": data["scraped_at"],
                                          "profiles": prof, "total": len(prof)},
                                         ensure_ascii=False, indent=1), encoding="utf-8")
    output.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"ZAPIS: {len(sessions)} sesji / {len(all_votes)} glosowan / {len(roster_all)} radnych")


def _got(d):
    return f"za={d.get('za')} przeciw={d.get('przeciw')} wsz={d.get('wstrzymal_sie')}"


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", required=True)
    ap.add_argument("--profiles", required=True)
    ap.add_argument("--cache-dir", default="/cache/wabrzezno/html")
    a = ap.parse_args()
    build(Path(a.output), Path(a.profiles), Path(a.cache_dir))
