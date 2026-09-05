#!/usr/bin/env python3
"""Radoskop Ostrów Mazowiecka — scraper imiennych głosowań z BIP bip.ostrowmaz.pl.

Źródło: "Sesje Rady Miasta -> IX Kadencja 2024-2029" (id=203743) na custom BIP
bip.ostrowmaz.pl/public/. Każda sesja ma załącznik "Wyniki głosowania"
(/public/getFile?id=N) — SKANOWANY PDF (raport eSesja, format TEXT:
"Głosowano w sprawie: ... / Wyniki imienne: ZA (n) lista nazwisk ...").
OCR: pymupdf render dpi=150 + tesseract -l pol (serial!).

Atrybucja per radny: lista nazwisk po etykiecie ZA/PRZECIW/WSTRZYMUJE
SIĘ/BRAK GŁOSU/NIEOBECNI; walidacja per głosowanie — liczba nazwisk w każdej
kategorii MUSI równać się licznikowi z nagłówka, inaczej głosowanie odrzucone.

Wzorce: cities/zbaszyn (OCR serial + cache), reference esesja-imienne-text-format.
"""

import difflib
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime
from itertools import combinations
from pathlib import Path

import pymupdf

BIP = "https://www.bip.ostrowmaz.pl"
KAD = "2024-2029"
KAD_LABEL = "IX kadencja (2024–2029)"
KAD_START = "2024-05-01"  # I sesja IX kad. = 6 maja 2024
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36"}

_MON = {m: i for i, m in enumerate(
    ["stycznia", "lutego", "marca", "kwietnia", "maja", "czerwca", "lipca",
     "sierpnia", "września", "wrzesnia", "października", "pazdziernika",
     "listopada", "grudnia"], 1)}
_MON.update({m + "r": _MON[m] for m in list(_MON)})

_HDR = re.compile(
    r"ZA[:\s]*(\d+)\s*,?\s*PRZECIW[:\s]*(\d+)\s*,?\s*WSTRZYMUJ[EĘ]?\s*SI[EĘ]?[:\s]*(\d+)"
    r"\s*,?\s*BRAK\s*G[ŁL]?OSU?[:\s]*(\d+)\s*,?\s*NIEOBECNI[:\s]*(\d+)")
_LABEL = re.compile(
    r"\b(ZA|PRZECIW|WSTRZYMUJ[EĘ]?SI[EĘ]?|BRAK\s*G[ŁL]?OSU?|NIEOBECNI)\s*\((\d+)\)", re.I)
_SESSION_ANCHOR = re.compile(r'href="(/public/\?id=(\d+))"[^>]*>\s*(I{1,3}V?X?|IV?|IX|X{1,3}[IVX]*)\s*[Ss]esja\s+w\s+dniu\s+([^<]+?)\s*r\.\s*</a>')
_FOOTER = re.compile(r"g[łl]osowanie\s+z\s+dnia|zakończono|wygenerowano|za\s+pomocą|app\.esesja|strona\s+\d+\s+z\s+\d+|raport\s+z\s+g", re.I)
_NAME_RE = re.compile(r"^[A-ZŁŚŹŻĆŃÓĄĘ][\w\-ąćęłńóśźż']*(?:\s+[A-ZŁŚŹŻĆŃÓĄĘ][\w\-ąćęłńóśźż']*)+$")


def fetch(url, timeout=30, retries=4):
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"fetch failed {url}: {last}")


def fetch_text(url):
    raw = fetch(url)
    m = re.search(rb"charset=[\"']?([\w-]+)", raw[:4000], re.I)
    enc = m.group(1).decode("ascii", "ignore") if m else "utf-8"
    try:
        return raw.decode(enc, "replace")
    except LookupError:
        return raw.decode("utf-8", "replace")


def ocr_pdf(raw, cache=None, url=""):
    if cache and url:
        cf = cache / (hashlib.md5(url.encode()).hexdigest() + ".ocr.txt")
        if cf.is_file():
            return cf.read_text(encoding="utf-8")
    doc = pymupdf.open(stream=raw, filetype="pdf")
    pages = []
    with tempfile.TemporaryDirectory() as td:
        for i, pg in enumerate(doc):
            pix = pg.get_pixmap(dpi=150)
            p = Path(td) / f"p{i}.png"
            pix.save(str(p))
            out = subprocess.run(["tesseract", str(p), "-", "-l", "pol", "--psm", "6"],
                                 capture_output=True, text=True, timeout=180)
            pages.append(out.stdout)
    txt = "\n".join(pages)
    if cache and url:
        cache.mkdir(parents=True, exist_ok=True)
        cf = cache / (hashlib.md5(url.encode()).hexdigest() + ".ocr.txt")
        cf.write_text(txt, encoding="utf-8")
    return txt


def slugify(name):
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


def _clean_token(tok):
    tok = re.sub(r"\s+", " ", tok).strip().strip(".,;:•-")
    if not tok or len(tok) < 3:
        return None
    if _FOOTER.search(tok):
        return None
    tok = re.sub(r"^\d{1,2}\.\s*", "", tok)
    if not _NAME_RE.match(tok):
        return None
    return tok


def parse_names(chunk, expect):
    # cut footer glue
    for marker in ("Głosowanie z dnia", "Głosowanie zakonczone", "Głosowanie zakończono",
                   "Wygenerowano", "głosowania z dnia"):
        idx = chunk.lower().find(marker.lower())
        if idx != -1:
            chunk = chunk[:idx]
    chunk = re.sub(r"\s+", " ", chunk)
    names = []
    for part in chunk.split(","):
        nm = _clean_token(part)
        if nm:
            names.append(nm)
    return names


def parse_votes(full, session_date, session_num):
    """Split OCR text into per-vote blocks; reconcile counts. Returns list of records."""
    text = full.replace("\u00ad", "")
    records = []
    # blocks start with "N. Głosowano w sprawie:"
    parts = re.split(r"(?m)^\s*(\d{1,2})\.\s*G[łl]osowano w sprawie[: ]", text)
    if len(parts) < 3:
        return records
    for i in range(1, len(parts) - 2, 2):
        topic_raw = parts[i + 1]
        body = parts[i + 2]
        hdr = _HDR.search(body)
        if not hdr:
            continue
        if "Wyniki imienne" not in body:
            continue
        za, przeciw, wstrz, brak, nieob = (int(x) for x in hdr.groups())
        # topic: cut at "- czas" / "czas głosowania"
        topic = re.split(r"-\s*cza[mś]|cza[mś]\s+g[łl]osowania", topic_raw)[0]
        topic = re.sub(r"\s+", " ", topic).strip(" .,-")
        after = body[body.index("Wyniki imienne"):]
        labels = list(_LABEL.finditer(after))
        if not labels:
            continue
        named = {"za": [], "przeciw": [], "wstrzymal_sie": [], "brak_glosu": [], "nieobecni": []}
        expect = {"za": za, "przeciw": przeciw, "wstrzymal_sie": wstrz, "brak_glosu": brak, "nieobecni": nieob}
        ok = True
        for j, m2 in enumerate(labels):
            key = m2.group(1).upper()
            key = ("za" if key.startswith("ZA") else "przeciw" if key.startswith("PRZECIW")
                   else "wstrzymal_sie" if key.startswith("WSTRZ")
                   else "brak_glosu" if key.startswith("BRAK") else "nieobecni")
            end = labels[j + 1].start() if j + 1 < len(labels) else len(after)
            chunk = after[m2.end():end]
            exp = int(m2.group(2))
            if exp != expect[key]:
                ok = False
                break
            names = parse_names(chunk, exp)
            if len(names) != exp:
                ok = False
                break
            named[key] = names
        if not ok:
            continue
        records.append({
            "session_date": session_date, "session_num": session_num, "topic": topic,
            "named": {"za": named["za"], "przeciw": named["przeciw"],
                      "wstrzymal_sie": named["wstrzymal_sie"], "brak_glosu": named["brak_glosu"],
                      "nieobecni": named["nieobecni"]},
        })
    return records


def main():
    city_dir = Path(sys.argv[sys.argv.index("--city-dir") + 1]) if "--city-dir" in sys.argv else Path(".")
    cache = Path(sys.argv[sys.argv.index("--cache-dir") + 1]) if "--cache-dir" in sys.argv else Path("/tmp/ostrow-cache")
    cache.mkdir(parents=True, exist_ok=True)

    # 1) session list from IX kadencja page
    html = fetch_text(f"{BIP}/public/?id=203743")
    sessions = {}
    for m in _SESSION_ANCHOR.finditer(html):
        sid = m.group(2)
        title = re.sub(r"<[^>]+>", "", m.group(0))
        roman = re.search(r">\s*([IVX]+)\s*[Ss]esja", m.group(0))
        dm = re.search(r"(\d{1,2})\s+([a-ząęłńóśźż]+)\s+(\d{4})", m.group(3))
        if not (roman and dm and dm.group(2).lower() in _MON):
            continue
        date_iso = f"{dm.group(3)}-{_MON[dm.group(2).lower()]:02d}-{int(dm.group(1)):02d}"
        if date_iso >= KAD_START:
            sessions[sid] = {"id": sid, "roman": roman.group(1), "date": date_iso}
    print(f"[ostrow] IX sessions: {len(sessions)}")

    # 2) per session -> "Wyniki głosowania" attachment
    records = []
    n_bad = 0
    for sid, meta in sorted(sessions.items(), key=lambda kv: kv[1]["date"]):
        url = f"{BIP}/public/?id={sid}"
        try:
            sh = fetch_text(url)
        except Exception as e:  # noqa: BLE001
            print(f"  [warn] session page {sid}: {e}")
            continue
        gf = None
        for m2 in re.finditer(r'href="(/public/getFile\?id=(\d+))"[^>]*>(.*?)</a>', sh, re.S):
            txt = re.sub(r"<[^>]+>", " ", m2.group(3))
            if re.search(r"Wyniki\s+g[łl]osowania", txt, re.I):
                gf = BIP + m2.group(1)
                break
        if not gf:
            print(f"  [skip] {meta['roman']} {meta['date']}: brak 'Wyniki głosowania'")
            continue
        pdf_url_cache = cache / (meta["date"] + ".pdfurl")
        pdf_url_cache.write_text(gf)
        try:
            raw = fetch(gf)
        except Exception as e:  # noqa: BLE001
            print(f"  [warn] pdf {gf}: {e}")
            continue
        full = ocr_pdf(raw, cache=cache, url=gf)
        recs = parse_votes(full, meta["date"], meta["roman"])
        n_bad += max(0, len(re.findall(r"Wyniki imienne", full)) - len(recs))
        records.extend(recs)
        print(f"  [{meta['roman']}] {meta['date']}: votes_ok={len(recs)} (OCR blocks={len(re.findall('Wyniki imienne', full))})")
        time.sleep(0.5)

    print(f"[ostrow] sessions_with_votes={len({r['session_date'] for r in records})} votes_ok={len(records)} votes_bad={n_bad}")
    if not records:
        print("[ostrow] NO RECONCILED VOTES — aborting")
        return 1

    # 3) build Radoskop output (zbaszyn-style)
    all_votes = []
    sessions_by_date = {}
    vid = 0
    for rec in records:
        d = rec["session_date"]
        if d not in sessions_by_date:
            sessions_by_date[d] = {"date": d, "number": rec["session_num"], "vote_count": 0, "attendees": set()}
        named = {k: list(v) for k, v in rec["named"].items() if k != "nieobecni"}
        named["nieobecni"] = rec["named"].get("nieobecni", [])
        vid += 1
        sessions_by_date[d]["vote_count"] += 1
        for cat in ("za", "przeciw", "wstrzymal_sie", "brak_glosu"):
            sessions_by_date[d]["attendees"].update(rec["named"].get(cat, []))
        all_votes.append({"id": str(vid), "session_date": d, "session_number": rec["session_num"],
                          "topic": rec["topic"], "named_votes": named,
                          "counts": {k: len(rec["named"].get(k, [])) for k in ("za", "przeciw", "wstrzymal_sie")}})
    sessions_data = []
    for d in sorted(sessions_by_date):
        s = sessions_by_date[d]
        sessions_data.append({"date": d, "number": s["number"], "vote_count": s["vote_count"],
                              "attendee_count": len(s["attendees"]), "attendees": sorted(s["attendees"]),
                              "speakers": []})
    all_names = set()
    for v in all_votes:
        for cat, names in v["named_votes"].items():
            if cat != "nieobecni":
                all_names.update(names)
    councilors_data = {n: {"name": n, "votes_za": 0, "votes_przeciw": 0, "votes_wstrzymal": 0,
                           "votes_brak": 0, "votes_nieobecny": 0} for n in all_names}
    for v in all_votes:
        for cat, names in v["named_votes"].items():
            key = {"za": "votes_za", "przeciw": "votes_przeciw", "wstrzymal_sie": "votes_wstrzymal",
                   "brak_glosu": "votes_brak", "nieobecni": "votes_nieobecny"}[cat]
            for nm in names:
                if nm in councilors_data:
                    councilors_data[nm][key] += 1
    total_votes = len(all_votes)
    total_sessions = len(sessions_data)
    csel = defaultdict(set)
    for v in all_votes:
        for cat, names in v["named_votes"].items():
            for nm in names:
                if nm in councilors_data:
                    csel[nm].add(v["session_date"])
    councilors_list = []
    for c in sorted(councilors_data.values(), key=lambda x: x["name"]):
        present = c["votes_za"] + c["votes_przeciw"] + c["votes_wstrzymal"]
        aktywnosc = present / total_votes * 100 if total_votes else 0
        frekwencja = len(csel.get(c["name"], set())) / total_sessions * 100 if total_sessions else 0
        councilors_list.append({"name": c["name"], "club": "", "district": None,
                                "frekwencja": round(frekwencja, 1), "aktywnosc": round(aktywnosc, 1),
                                "zgodnosc_z_klubem": 0.0,
                                "votes_za": c["votes_za"], "votes_przeciw": c["votes_przeciw"],
                                "votes_wstrzymal": c["votes_wstrzymal"], "votes_brak": c["votes_brak"],
                                "votes_nieobecny": c["votes_nieobecny"], "votes_total": total_votes,
                                "rebellion_count": 0, "rebellions": [],
                                "has_activity_data": False, "activity": None})
    vectors = defaultdict(dict)
    for v in all_votes:
        for cat in ("za", "przeciw", "wstrzymal_sie"):
            for nm in v["named_votes"].get(cat, []):
                vectors[nm][v["id"]] = cat
    pairs = []
    ns = sorted(vectors.keys())
    for a, b in combinations(ns, 2):
        common = set(vectors[a]) & set(vectors[b])
        if len(common) < 10:
            continue
        same = sum(1 for v2 in common if vectors[a][v2] == vectors[b][v2])
        pairs.append({"a": a, "b": b, "club_a": "", "club_b": "",
                      "score": round(same / len(common) * 100, 1), "common_votes": len(common)})
    pairs.sort(key=lambda x: x["score"], reverse=True)
    kad = {"id": KAD, "label": KAD_LABEL, "clubs": {}, "sessions": sessions_data,
           "total_sessions": total_sessions, "total_votes": total_votes,
           "total_councilors": len(councilors_list), "councilors": councilors_list,
           "votes": all_votes, "similarity_top": pairs[:20], "similarity_bottom": pairs[-20:][::-1]}

    cv = defaultdict(lambda: {"za": 0, "przeciw": 0, "wstrzymal_sie": 0, "votes": []})
    for rec in records:
        d = rec["session_date"]
        for cat in ("za", "przeciw", "wstrzymal_sie"):
            for nm in rec["named"].get(cat, []):
                cv[nm][cat] += 1
                cv[nm]["votes"].append({"session": d, "vote": cat})
    profiles = []
    for nm in sorted(all_names):
        vd = cv.get(nm, {"za": 0, "przeciw": 0, "wstrzymal_sie": 0, "votes": []})
        total = sum(vd[k] for k in ("za", "przeciw", "wstrzymal_sie")) or 1
        sess = len({v["session"] for v in vd["votes"]})
        profiles.append({"name": nm, "slug": slugify(nm),
                         "kadencje": {KAD: {
                             "club": "", "has_voting_data": True, "has_activity_data": False,
                             "frekwencja": round(sess / max(1, total_sessions) * 100, 1),
                             "aktywnosc": round((vd["za"] + vd["przeciw"] + vd["wstrzymal_sie"]) / max(1, len(records)) * 100, 1),
                             "zgodnosc_z_klubem": 0.0,
                             "votes_za": vd["za"], "votes_przeciw": vd["przeciw"],
                             "votes_wstrzymal": vd["wstrzymal_sie"],
                             "votes_brak": councilors_data[nm]["votes_brak"],
                             "votes_nieobecny": councilors_data[nm]["votes_nieobecny"],
                             "votes_total": total,
                             "rebellion_count": 0, "rebellions": [],
                             "roles": [], "notes": "",
                             "former": False, "mid_term": False}}})
    profiles = {"profiles": profiles, "total": len(profiles)}

    out_path = city_dir / "docs" / "data.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    stubs = []
    kid = kad["id"]
    stubs.append({"id": kid, "label": kad.get("label", f"Kadencja {kid}")})
    with open(out_path.parent / f"kadencja-{kid}.json", "w", encoding="utf-8") as f:
        json.dump(kad, f, ensure_ascii=False, separators=(",", ":"))
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"generated": datetime.now().isoformat(), "default_kadencja": KAD, "kadencje": stubs},
                  f, ensure_ascii=False, separators=(",", ":"))
    with open(out_path.parent / "profiles.json", "w", encoding="utf-8") as f:
        json.dump(profiles, f, ensure_ascii=False, separators=(",", ":"))
    print(f"[ostrow] FINAL: votes={total_votes} sessions={total_sessions} councilors={len(councilors_list)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
