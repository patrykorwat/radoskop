#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Radoskop Piła — imienne głosowania Rady Miasta Piły (IX kadencja 2024-2029).

Źródło: BIP bip.pila.pl (platforma eBOI/Madkom-look, server-rendered HTML).
Struktura:
  /5417-2026.html?  /2025.html?  /3202-2024.html?   -> listy sesji wg roku
    link "…Sesja Rady Miasta Piły, <data>…" -> strona sesji
      link "Głosowania z sesji" -> strona z Załącznikiem
        /files/file_add/download/<id>_protokol-glosowania.pdf  (PDF tekstowy,
        format eSesja standard: "Głosowano w sprawie / Wyniki głosowania /
        Wyniki imienne: ZA (N) nazwiska…")
Parser: lib_voting_pdf_table.parse_voting_pdf (esesja_standard), walidacja
per-głos: liczby nazwisk == agregaty ZA/PRZECIW/WSTRZYM.

Kluby (kadencja 2024-2029) z /kluby-radnych.html — club_assignments w
config.json (kuratorowane).

Wyjście: docs/data.json, docs/kadencja-2024-2029.json, docs/profiles.json
Użycie: python scrape_pila.py --city-dir <dir> [--cache-dir d] [--skip-download]
"""
import argparse
import json
import re
import sys
import time
import unicodedata
from datetime import datetime
from pathlib import Path

import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

HERE = Path(__file__).resolve()
REPO = HERE.parent.parent.parent.parent     # scripts -> pila -> cities -> radoskop
sys.path.insert(0, str(REPO / "scripts"))
from lib_voting_pdf_table import parse_voting_pdf  # noqa: E402

BIP = "https://bip.pila.pl"
YEAR_PAGES = ["/5417-2026.html?", "/2025.html?", "/3202-2024.html?"]
KAD_START = "2024-05-07"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"
REQ_DELAY = 0.5
_LAST = 0.0

MONTHS = {"stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5, "czerwca": 6,
          "lipca": 7, "sierpnia": 8, "września": 9, "października": 10, "listopada": 11, "grudnia": 12}


def _get(url, cache_dir=None, binary=False):
    global _LAST
    import hashlib
    key = hashlib.md5(url.encode()).hexdigest()
    cf = None
    if cache_dir:
        cache_dir = Path(cache_dir); cache_dir.mkdir(parents=True, exist_ok=True)
        cf = cache_dir / (key + (".bin" if binary else ".dat"))
        if cf.is_file() and cf.stat().st_size > 0:
            return cf.read_bytes() if binary else cf.read_text(encoding="utf-8")
    d = time.time() - _LAST
    if d < REQ_DELAY:
        time.sleep(REQ_DELAY - d)
    _LAST = time.time()
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0 (Radoskop)"}, timeout=90, verify=False)
    r.raise_for_status()
    data = r.content
    if cf is not None:
        cf.write_bytes(data)
    return data if binary else data.decode("utf-8", "ignore")


_ROMAN = re.compile(r"([IVXLCDM]+)\s+Sesja", re.I)
_DATE = re.compile(r"(\d{1,2})\s+(\w+)\s+(\d{4})", re.I)


def discover_sessions(cache_dir=None):
    out = {}
    for yp in YEAR_PAGES:
        html = _get(BIP + yp, cache_dir)
        for m in re.finditer(r'<a[^>]+href="([^"]+)"[^>]*>([^<]*Sesja[^<]*)</a>', html):
            href, txt = m.group(1), m.group(2).strip()
            rm = _ROMAN.search(txt)
            dm = _DATE.search(txt)
            if not rm or not dm:
                continue
            mon = MONTHS.get(dm.group(2).lower())
            if not mon:
                continue
            date = f"{dm.group(3)}-{mon:02d}-{int(dm.group(1)):02d}"
            if date < KAD_START:
                continue
            url = href if href.startswith("http") else BIP + "/" + href.lstrip("/")
            out[url] = {"num": rm.group(1).upper(), "date": date, "title": txt, "url": url}
    return sorted(out.values(), key=lambda s: s["date"])


def find_votes_pdf(session_url, cache_dir=None):
    html = _get(session_url, cache_dir)
    gm = re.search(r'href="([^"]*glosowania-z-sesji[^"]*)"', html, re.I)
    if not gm:
        return None
    gu = gm.group(1)
    gu = gu if gu.startswith("http") else session_url.rsplit("/", 1)[0] + "/" + gu.lstrip("/")
    h2 = _get(gu, cache_dir)
    pm = re.search(r'href="([^"]*file_add/download/[^"]+\.pdf)"', h2, re.I)
    if not pm:
        return None
    pu = pm.group(1)
    return pu if pu.startswith("http") else BIP + "/" + pu.lstrip("/")


def normalize_name(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z]", "", s.lower())
    return s


def parse_pdf(data):
    """PDF -> records w formacie build_output (named dict + ok).

    Piła ma DWA warianty wydruku w jednym załączniku 'Protokół głosowania':
      A) starszy (2024/2025): bloki per głos 'Wyniki głosowania: ZA (20), …'
         + 'Lista imienna' + 'ZA: nazwiska…' … + 'ID głosowania: N, czas…'
      B) nowszy (2026, eSesja standard): 'Głosowano w sprawie / Wyniki
         głosowania (Radni) / ZA: 19, … / Wyniki imienne: ZA (19) nazwiska'
    Parser blokowy obsługuje oba: podział po 'ID głosowania:' albo po
    nagłówku kolejnego głosowania; liczy nazwiska i waliduje z agregatami.
    """
    import io
    import pdfplumber
    text_parts = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for p in pdf.pages:
            text_parts.append(p.extract_text() or "")
    full = "\n".join(text_parts)
    full = full.replace("\u00a0", " ")

    # wariant B: znormalizuj do szkieletu wariantu A ('Wyniki imienne: ZA (19)' -> 'ZA: ')
    # prościej: parsuj każdy wariant osobną ścieżką.
    recs = []
    if "Lista imienna" in full:
        recs.extend(_parse_blocks(full))
    if "Wyniki imienne" in full:
        f = HERE.parent / ".run_pdf.tmp"
        f.write_bytes(data)
        try:
            res = parse_voting_pdf(str(f))
        finally:
            f.unlink(missing_ok=True)
        for v in res["votes"]:
            c = v.get("counts") or {}
            nv = v["named_votes"]
            za, prze, wstr = nv.get("za", []), nv.get("przeciw", []), nv.get("wstrzymal_sie", [])
            ok = (c.get("za") is not None
                  and len(za) == c.get("za", -1)
                  and len(prze) == c.get("przeciw", -1)
                  and len(wstr) == c.get("wstrzymal_sie", -1))
            recs.append({"topic": v["topic"],
                         "named": {"za": za, "przeciw": prze, "wstrzymal_sie": wstr,
                                   "nieobecni": nv.get("nieobecni", [])},
                         "agg": {"za": c.get("za"), "przeciw": c.get("przeciw"),
                                 "wstrzym": c.get("wstrzymal_sie")},
                         "ok": ok, "bad": []})
    # dedup NIE jest potrzebny: 'Lista imienna' (wariant A) i 'Wyniki imienne'
    # (wariant B) wykluczają się w PDFach Piły; identyczne wyniki różnych
    # głosowań kandydatów nie mogą być scalone po kluczu agg.
    return recs


_CATS = [
    ("za", r"\bZA:"),
    ("przeciw", r"\bPRZECIW:"),
    ("wstrzymal_sie", r"WSTRZ(Y|U)MUJ[EĘŚ] SI[EĘ]:"),
    ("brak_glosu", r"BRAK G[EŁ]OSU:"),
    ("nieobecni", r"NIEOBECN(Y|NYCH|NI|NA):"),
    ("obecny", r"OBECN(A/Y|NA/Y|ENI|NY|A/Y\s*\()"),
]
_AGG = re.compile(r"(ZA|PRZECIW|WSTRZ(?:Y|U)MUJ[EĘ] SI[EĘ]|BRAK G[EŁ]OSU|NIEOBECN(?:Y|YCH|NI)|OBECN(?:A/Y|ENI|NY))\s*\((\d+)\)")


def _split_names(block):
    return [x.strip(" .,") for x in block.split(",") if x.strip(" .,")]


def _looks_like_name(s):
    return bool(re.match(r"^[A-ZŁŚŹŻĆŃÓ][\wŁłŚśŹźŻżĆćŃńÓó\-\'\. ]{2,}$", s))


def _parse_blocks(full):
    """Wariant A: bloki per głos = 'Wyniki głosowania: <agg>' .. 'ID głosowania: N'.
    Kategorie po 'Lista imienna': 'ZA: nazwiska, …' do kolejnej etykiety."""
    recs = []
    heads = [m for m in re.finditer(r"Wyniki głosowania[:\s]", full)]
    prev_end = 0
    for i, m in enumerate(heads):
        # koniec bloku: cała linia 'ID głosowania: …' po head
        idm = re.search(r"ID głosowania:", full[m.start():])
        if idm:
            be = m.start() + idm.end()
            nl_id = full.find("\n", be)
            block_end = nl_id + 1 if 0 <= nl_id < be + 120 else be
        else:
            block_end = heads[i + 1].start() if i + 1 < len(heads) else len(full)
        # head = linia 'Wyniki głosowania: ZA (20), …' (do końca linii)
        nl = full.find("\n", m.start())
        head = full[m.start(): nl if 0 < nl < block_end else block_end]
        aggs = {}
        for g, n in _AGG.findall(head):
            gu = g.upper()
            if gu == "ZA":
                aggs["za"] = int(n)
            elif gu == "PRZECIW":
                aggs["przeciw"] = int(n)
            elif gu.startswith("WSTRZ"):
                aggs["wstrzym"] = int(n)
            elif gu.startswith("NIEOBECN"):
                aggs["nieobecnych"] = int(n)
        # kategorie
        li = full.find("Lista imienna", m.end(), block_end)
        start = li + len("Lista imienna") if li >= 0 else nl + 1
        catseg = full[start:block_end]
        catseg = re.sub(r"suma kontrolna.*", "", catseg, flags=re.S)
        marks = []
        for cat, pat in _CATS:
            for mm in re.finditer(pat, catseg, re.I):
                marks.append((mm.start(), mm.end(), cat))
        marks.sort()
        named = {"za": [], "przeciw": [], "wstrzymal_sie": [], "nieobecni": []}
        for j, (s, e, cat) in enumerate(marks):
            stop = marks[j + 1][0] if j + 1 < len(marks) else len(catseg)
            block = re.sub(r"\s+", " ", catseg[e:stop])
            names = [x for x in _split_names(block) if _looks_like_name(x)]
            if cat in named:
                named[cat].extend(names)
        # topic = tekst między poprzednim blokiem a head (bez stopki ID/suma)
        pre = full[prev_end:m.start()]
        pre = re.sub(r"ID głosowania:[^\n]*", " ", pre)
        pre = re.sub(r"suma kontrolna:[^\n]*", " ", pre)
        tail = [l.strip() for l in pre.splitlines() if l.strip()][-3:]
        topic = " ".join(tail)[-220:]
        prev_end = block_end
        ok = (aggs.get("za") is not None
              and len(named["za"]) == aggs.get("za", -1)
              and len(named["przeciw"]) == aggs.get("przeciw", -1)
              and len(named["wstrzymal_sie"]) == aggs.get("wstrzym", -1))
        recs.append({"topic": topic, "named": named,
                     "agg": {"za": aggs.get("za"), "przeciw": aggs.get("przeciw"),
                             "wstrzym": aggs.get("wstrzym")},
                     "ok": ok, "bad": []})
    return recs


def build_output(records, club_assign=None):
    club_assign = club_assign or {}
    names = set()
    for r in records:
        for cat in ("za", "przeciw", "wstrzymal_sie", "nieobecni"):
            names.update(r["named"].get(cat, []))
    names = sorted(names)
    idx = {n: i for i, n in enumerate(names)}
    sessions = {}
    votes_out = []
    for r in records:
        key = (r["date"], r["num"])
        sessions.setdefault(key, 0)
        sessions[key] += 1
        named_idx = {cat: [idx[n] for n in r["named"].get(cat, [])]
                     for cat in ("za", "przeciw", "wstrzymal_sie")}
        votes_out.append({"topic": r["topic"], "date": r["date"], "session": r["num"],
                          "named_votes": named_idx})
    sess_list = []
    for i, ((date, num), cnt) in enumerate(sorted(sessions.items()), 1):
        sess_list.append({"date": date, "number": num,
                          "label": f"{num} Sesja Rady Miasta Piły ({date})",
                          "vote_count": cnt})
    councilors = [{"id": i, "name": n, "club": club_assign.get(n, "NZ")} for i, n in enumerate(names)]
    total_votes = len(votes_out)
    kad = {"id": KADENCJA_ID, "label": KADENCJA_LABEL,
           "sessions": sess_list, "votes": votes_out,
           "councilor_index": names, "councilors": councilors,
           "total_councilors": len(names), "total_votes": total_votes,
           "similarity_top": [], "similarity_bottom": []}
    out = {"generated": datetime.now().isoformat(), "default_kadencja": KADENCJA_ID,
           "kadencje": [kad]}
    return out, total_votes, len(sess_list)


def build_profiles(records, club_assign=None):
    club_assign = club_assign or {}
    stats = {}
    for r in records:
        for cat in ("za", "przeciw", "wstrzymal_sie"):
            for n in r["named"].get(cat, []):
                s = stats.setdefault(n, {"za": 0, "przeciw": 0, "wstrzymal_sie": 0, "nieobecni": 0, "total": 0})
                s[cat] += 1; s["total"] += 1
        for n in r["named"].get("nieobecni", []):
            s = stats.setdefault(n, {"za": 0, "przeciw": 0, "wstrzymal_sie": 0, "nieobecni": 0, "total": 0})
            s["nieobecni"] += 1
    profiles = []
    for n in sorted(stats):
        s = stats[n]
        def slug(x):
            x = unicodedata.normalize("NFKD", x).encode("ascii", "ignore").decode().lower()
            return re.sub(r"[^a-z0-9]+", "-", x).strip("-")
        total_all = s["total"] + s["nieobecni"]
        profiles.append({
            "name": n, "slug": slug(n), "club": club_assign.get(n, "NZ"),
            "role": "", "photo_url": "", "bio": "", "email": "", "social_links": {},
            "voting": None,
            "kadencje": {KADENCJA_ID: {
                "club": club_assign.get(n, "NZ"), "has_voting_data": True, "role": "",
                "votes_for": s["za"], "votes_against": s["przeciw"], "abstentions": s["wstrzymal_sie"],
                "absent": s["nieobecni"], "votes_total": s["total"],
                "frekwencja": round(100 * s["total"] / total_all, 1) if total_all else 0,
                "aktywnosc": round(100 * s["total"] / total_all, 1) if total_all else 0,
                "zgodnosc_z_klubem": None, "rebellion_count": 0,
            }},
        })
    return {"scraped_at": datetime.now().isoformat(), "profiles": profiles, "total": len(profiles)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--city-dir", required=True)
    ap.add_argument("--cache-dir", default=None)
    ap.add_argument("--skip-download", action="store_true")
    args = ap.parse_args()
    city_dir = Path(args.city_dir)
    cache = Path(args.cache_dir) if args.cache_dir else None
    cfg = {}
    if (city_dir / "config.json").is_file():
        cfg = json.loads((city_dir / "config.json").read_text(encoding="utf-8"))
    club_assign = cfg.get("club_assignments", {}) or {}
    # club_assignments keys -> znormalizowane
    ca_norm = {normalize_name(k): v for k, v in club_assign.items()}

    sessions = discover_sessions(cache)
    if not sessions:
        print("[pila] BRAK SESJI!"); sys.exit(2)
    print(f"[pila] {len(sessions)} sesji IX kad. ({sessions[0]['date']} .. {sessions[-1]['date']})")

    pdf_dir = city_dir / "pdfs"; pdf_dir.mkdir(exist_ok=True)
    records = []
    vstat = {"v": 0, "ok": 0}
    for se in sessions:
        pf = pdf_dir / f"{se['num']}_{se['date']}.pdf"
        if not (pf.is_file() and pf.stat().st_size > 1000):
            if args.skip_download:
                print(f"  [skip {se['date']} no pdf cached]"); continue
            pu = find_votes_pdf(se["url"], cache)
            if not pu:
                print(f"  [skip {se['date']} no votes pdf]"); continue
            data = _get(pu, cache, binary=True)
            if data[:4] != b"%PDF":
                print(f"  [skip {se['date']} not pdf]"); continue
            pf.write_bytes(data)
        recs = parse_pdf(pf.read_bytes())
        nok = sum(1 for r in recs if r["ok"])
        vstat["v"] += len(recs); vstat["ok"] += nok
        for r in recs:
            r["date"] = se["date"]; r["num"] = se["num"]
            # nazwiska -> forma z club_assignments (spójność pisowni)
            for cat in r["named"]:
                r["named"][cat] = [next((k for k in club_assign if normalize_name(k) == normalize_name(n)), n)
                                   for n in r["named"][cat]]
        records.extend([r for r in recs if r["ok"]])
        print(f"  [{se['num']:>4} {se['date']}] votes={len(recs)} ok={nok}")

    print(f"[pila] zwalidowane {vstat['ok']}/{vstat['v']}")
    if not records:
        sys.exit(3)

    out, tv, ts = build_output(records, club_assign)
    profiles = build_profiles(records, club_assign)
    docs = city_dir / "docs"; docs.mkdir(exist_ok=True)
    kad_obj = out["kadencje"][0]
    (docs / "kadencja-2024-2029.json").write_text(json.dumps(kad_obj, ensure_ascii=False, indent=1), encoding="utf-8")
    (docs / "profiles.json").write_text(json.dumps(profiles, ensure_ascii=False, indent=1), encoding="utf-8")
    data_json = {
        "city_name": cfg.get("city_name", "Piła"),
        "rada_name": cfg.get("rada_name", "Rada Miasta Piły"),
        "generated": out["generated"],
        "kadencje": [{"id": KADENCJA_ID, "label": KADENCJA_LABEL}],
        "stats": {"total_votes": tv, "total_sessions": ts,
                  "total_councilors": kad_obj["total_councilors"]},
    }
    (docs / "data.json").write_text(json.dumps(data_json, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[pila] OK votes={tv} sessions={ts} councilors={kad_obj['total_councilors']}")


if __name__ == "__main__":
    main()
