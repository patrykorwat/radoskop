#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop Zielonka — głosowania imienne (BIP Madkom bip.zielonka.pl, API JSON + OCR).

BIP = React-SPA Madkom z jawnym API JSON (jak Oborniki):
  /api/menu/{id}/submenu, /api/menu/{id}/articles?limit=.., /api/articles/{id}
  załączniki: e,pobierz,get.html?id=N
Kategoria 'Głosowania z Sesji Rady Miasta' (menu 1737) → IX Kadencja (4410) →
artykuł 37950 z linkami /m,<id> per sesja → kategoria sesji → 1 artykuł
'Protokoły z głosowań...' → załącznik PDF = eSesja-print, ALE SKANOWANY
(brak warstwy tekstu) → OCR: fitz render dpi=150 + tesseract -l pol.
Format per głosowanie: 'Głosowano w sprawie: ...' / 'ZA: n, PRZECIW: n,
WSTRZYMUJĘ SIĘ: n, BRAK GŁOSU: n, NIEOBECNI: n' / 'Wyniki imienne:' /
'ZA (n)' listy nazwisk po przecinkach / 'Głosowanie z dnia: D.M.RRRR, HH:MM'.
Walidacja: liczba nazwisk per kategoria == licznik nagłówka (OCR-tolerancja:
porównanie po znormalizowanych nazwiskach).

Użycie: python scrape_zielonka.py [city_dir]
"""
import json
import re
import ssl
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pymupdf

BASE = "https://bip.zielonka.pl"
MENU_VOTES_KAD_IX = "4410"     # Głosowania z Sesji → IX Kadencja
UA = {"User-Agent": "Mozilla/5.0 Radoskop/1.0 (info@radoskop.eu)"}
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE
IX_START = "2024-05-07"


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60, context=CTX) as r:
                return r.read()
        except Exception:
            if attempt == 2:
                raise
            time.sleep(2)


def _json(path: str):
    return json.loads(_get(BASE + path).decode("utf-8"))


def slugify(name: str) -> str:
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace("ł", "l")
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-") or "radny"


def norm_name(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace("ł", "l")
    return re.sub(r"[^a-z-]", "", s)


CAT_MAP = {"ZA": "za", "PRZECIW": "przeciw", "WSTRZYMUJE SIE": "wstrzymal_sie",
           "WSTRZYMUJĘ SIĘ": "wstrzymal_sie", "BRAK GLOSU": "brak_glosu",
           "BRAK GŁOSU": "brak_glosu", "NIEOBECNI": "nieobecni"}
OCR_FIX = {"WSTRZYMUJE SLIE": "wstrzymal_sie", "WSTRZYMUJE SLĘ": "wstrzymal_sie",
           "WSTRZYMUJE SM": "wstrzymal_sie", "PRZECIW": "przeciw",
           "NIEOECNI": "nieobecni", "BRAK GLOSU": "brak_glosu"}


def cat_key(raw: str):
    raw = raw.upper()
    if raw.startswith("WSTRZYMUJ"):
        return "wstrzymal_sie"
    if raw.startswith("PRZECIW") or raw.startswith("PRZECN"):
        return "przeciw"
    if raw.startswith("BRAK"):
        return "brak_glosu"
    if raw.startswith("NIEOBECNI") or raw.startswith("NIEOECNI"):
        return "nieobecni"
    if raw == "ZA":
        return "za"
    return None


def ocr_pdf(pdf: bytes) -> str:
    doc = pymupdf.open(stream=pdf, filetype="pdf")
    pages = []
    with tempfile.TemporaryDirectory() as td:
        for i, pg in enumerate(doc):
            png = f"{td}/p{i}.png"
            pg.get_pixmap(dpi=150).save(png)
            r = subprocess.run(["tesseract", png, "-", "-l", "pol"],
                               capture_output=True, text=True)
            pages.append(r.stdout)
    doc.close()
    return "\n".join(pages)


def parse_votes_text(text: str):
    """OCR-tekst eSesja-print: per głosowanie blok Wyniki głosowania."""
    text = text.replace("\u00a0", " ")
    votes = []
    blocks = re.split(r"Wyniki głosowania", text)
    for b in blocks[1:]:
        gm = re.search(r"G.{0,3}sowano w sprawie:?\s*(.+?)\s*\n\s*(ZA:?\s*\d+.+?)\n", b, re.S)
        if not gm:
            continue
        topic = " ".join(gm.group(1).split())
        agg_line = " ".join(gm.group(2).split())
        counts = dict(re.findall(r"(ZA|PRZECIW|WSTRZYMUJ[^:]{0,12}|BRAK\s*G.OSU|NIEOBECNI):\s*(\d+)", agg_line))
        dm = re.search(r"G.{0,3}sowanie z dnia:\s*(\d{1,2})\.(\d{2})\.(\d{4})", b)
        if not dm:
            continue
        date = f"{dm.group(3)}-{dm.group(2)}-{dm.group(1).zfill(2)}"
        hdr = re.compile(r"\n\s*(WSTRZYMUJ[^(:\n]{0,12}|ZA|PRZECIW|BRAK\s*G.OSU|NIEOBECNI)\s*\((\d+)\)")
        heads = list(hdr.finditer("\n" + b))
        full = "\n" + b
        per = {}
        ok = True
        for j, h in enumerate(heads):
            key_raw = " ".join(h.group(1).upper().split())
            key = cat_key(key_raw)
            if not key:
                continue
            n_decl = int(h.group(2))
            end = heads[j + 1].start() if j + 1 < len(heads) else len(full)
            names_txt = " ".join(full[h.end():end].split())
            names_txt = re.split(r"G.{0,3}sowanie z dnia|Wygenerowano|Strona \d", names_txt)[0]
            names = [x.strip(" .") for x in names_txt.split(",") if x.strip(" .")]
            names = [re.sub(r"\s+", " ", x) for x in names
                     if re.match(r"^[A-ZŁŚŻŹĆĄŃÓ][\włżźćąóńś-]+", x)]
            if len(names) != n_decl:
                ok = False
                break
            per.setdefault(key, []).extend(names)
        if not ok or not per:
            continue
        cv = {k: int(v) for k, v in counts.items()}
        votes.append({"date": date, "topic": topic[:250], "per": per, "agg": cv})
    return votes


def validate(v):
    """per-listy muszą zgadzać się z licznikami zagregowanymi."""
    for cat, n in v["agg"].items():
        key = cat_key(" ".join(cat.upper().split()))
        if key is None:
            continue
        if len(v["per"].get(key, [])) != int(n):
            return False
    return True


def main(city_dir: Path):
    docs = city_dir / "docs"
    docs.mkdir(exist_ok=True)
    # 1) lista kategorii sesji z artykułu spisu treści
    d = _json(f"/api/menu/{MENU_VOTES_KAD_IX}/articles?limit=20")
    idx_art = d["articles"][0]["id"]
    idx = _json(f"/api/articles/{idx_art}")
    cats = re.findall(r'href="/m,(\d+),', idx.get("content", ""))
    print(f"[zielonka] {len(cats)} kategorii sesji")
    all_names, sessions, votes_out = [], [], []
    for cat in cats:
        try:
            arts = _json(f"/api/menu/{cat}/articles?limit=20")["articles"]
            if not arts:
                print(f"  [skip] cat {cat} — brak artykułów")
                continue
            art = _json(f"/api/articles/{arts[0]['id']}")
            title = art.get("title", "")
            atts = [x for x in art.get("attachments", [])
                    if (x.get("extension") or "").lower() == "pdf"]
            if not atts:
                print(f"  [skip] {title[:60]} — brak PDF")
                continue
            url = BASE + "/" + atts[0]["link"].lstrip("/")
            pdf = _get(url)
            text = ocr_pdf(pdf)
            vs = [v for v in parse_votes_text(text) if validate(v)]
        except Exception as e:
            print(f"  [warn] cat {cat}: {e}")
            continue
        time.sleep(0.3)
        if not vs:
            print(f"  [skip] {title[:60]} — parser/OCR 0 zwalidowanych")
            continue
        rm = re.search(r"([IVXLCDM]+)\s+Sesji", title)
        roman = rm.group(1) if rm else ""
        sdate = vs[0]["date"]
        idxs = {nm: n for n, nm in enumerate(all_names)}
        n_ok = 0
        for v in vs:
            nv = {"za": [], "przeciw": [], "wstrzymal_sie": [], "brak_glosu": [], "nieobecni": []}
            for k, names in v["per"].items():
                for nm in names:
                    if nm not in idxs:
                        known = {norm_name(x): x for x in all_names}
                        if norm_name(nm) in known:
                            nm = known[norm_name(nm)]
                        else:
                            all_names.append(nm)
                        idxs[nm] = len(all_names) - 1
                    nv[k].append(idxs[nm])
            c = {"uprawnieni": sum(len(x) for x in nv.values()),
                 **{k: len(v["per"].get(k, [])) for k in ("za", "przeciw", "wstrzymal_sie")}}
            votes_out.append({
                "id": f"{v['date']}_{len(votes_out):03d}", "source_url": url,
                "session_date": v["date"], "session_number": roman,
                "topic": v["topic"], "druk": "None",
                "resolution": "przyjete" if c["za"] > c["przeciw"] else "odrzucone",
                "counts": c, "named_votes": nv})
            n_ok += 1
        sessions.append({"date": sdate, "number": roman,
                         "label": f"Sesja {roman} ({sdate})", "vote_count": n_ok,
                         "attendee_count": None, "attendees": [], "speakers": []})
        print(f"  [ok] {title[:60]} -> {n_ok} głosów")
    sessions.sort(key=lambda s: s["date"], reverse=True)
    councilors = []
    for i, nm in enumerate(all_names):
        z = p_ = w = b_ = nb = tot = 0
        for v in votes_out:
            nv = v["named_votes"]
            if i in nv["za"]:
                z += 1; tot += 1
            elif i in nv["przeciw"]:
                p_ += 1; tot += 1
            elif i in nv["wstrzymal_sie"]:
                w += 1; tot += 1
            elif i in nv["brak_glosu"]:
                b_ += 1; tot += 1
            elif i in nv["nieobecni"]:
                nb += 1
        uprawn = tot + nb
        councilors.append({"name": nm, "slug": slugify(nm), "club": "",
                           "za": z, "przeciw": p_, "wstrzymal_sie": w,
                           "brak_glosu": b_, "nieobecny": nb, "glosowal": tot,
                           "frekwencja": round(100 * tot / uprawn, 1) if uprawn else 0,
                           "aktywnosc": round(100 * tot / uprawn, 1) if uprawn else 0,
                           "zgodnosc_z_klubem": None, "rebellion_count": 0})
    now = datetime.now(timezone.utc).isoformat()
    councilor_index = all_names
    kad = {"id": "2024-2029", "label": "IX kadencja (2024–2029)",
           "sessions": sessions,
           "votes": votes_out, "councilor_index": councilor_index,
           "councilors": councilors,
           "total_councilors": len(all_names), "total_votes": len(votes_out),
           "similarity_top": [], "similarity_bottom": []}
    (docs / f"kadencja-2024-2029.json").write_text(
        json.dumps(kad, ensure_ascii=False, indent=1), encoding="utf-8")
    profiles = {"scraped_at": now, "total": len(councilors), "profiles": [
        {"name": c["name"], "slug": c["slug"], "club": "", "role": "",
         "photo_url": "", "bio": "", "email": "", "social_links": {},
         "voting": {"za": c["za"], "przeciw": c["przeciw"], "wstrzymal_sie": c["wstrzymal_sie"]},
         "kadencje": {"2024-2029": {
             "club": "", "has_voting_data": True, "role": "",
             "frekwencja": c["frekwencja"], "aktywnosc": c["aktywnosc"],
             "zgodnosc_z_klubem": None, "rebellion_count": 0,
             "votes_total": c["glosowal"]}}} for c in councilors]}
    (docs / "profiles.json").write_text(json.dumps(profiles, ensure_ascii=False, indent=1), encoding="utf-8")
    data = {"scraped_at": now, "city": "Zielonka", "bip": BASE,
            "kadencje": [{"id": "2024-2029", "label": "IX kadencja (2024–2029)"}],
            "stats": {"sessions": len(sessions), "votes": len(votes_out),
                      "councilors": len(all_names)}}
    (docs / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[zielonka] DONE: {len(sessions)} sesji / {len(votes_out)} głosów / {len(all_names)} radnych")


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else Path("."))
