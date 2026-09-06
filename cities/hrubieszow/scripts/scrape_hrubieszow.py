#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop Hrubieszów — głosowania imienne (BIP umhrubieszow.bip.lubelskie.pl).

BIP = platforma lubelskie.pl (serwer-renderowany + DataTables AJAX):
  POST /index.php?id=246&action=list-ajax   lista dokumentów 'Imienne wykazy głosowań'
      (aaData: id_dokumentu, data_utworzenia, tresc='XXXVIII sesja Rady Miejskiej...')
  GET  /index.php?id=246&id_dokumentu=N&akcja=szczegoly&p2=N
      strona dokumentu → link 'Plik źródłowy' /upload/pliki/*.pdf
PDF = TEKSTOWY (pdfplumber/pymupdf直接): per głosowanie
  '<n>. <temat>:' / 'Radny' 'Oddany głos' / pary wierszy: nazwisko / 'Jestem za|Jestem przeciw|Wstrzymuję się'
Brak nagłówków z licznikami — walidacja: parzystość par + nazwisko z wzorca + głos ze znanego słownika.

Użycie: python scrape_hrubieszow.py [city_dir]
"""
import json
import re
import ssl
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pymupdf

BASE = "https://umhrubieszow.bip.lubelskie.pl"
MENU_GLOSOWANIA = "246"
UA = {"User-Agent": "Mozilla/5.0 Radoskop/1.0 (info@radoskop.eu)"}
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE
IX_START = "2024-05-07"

VOTE_MAP = {
    "jestem za": "za",
    "za": "za",
    "jestem przeciw": "przeciw",
    "przeciw": "przeciw",
    "wstrzymuję się": "wstrzymal_sie",
    "wstrzymuje sie": "wstrzymal_sie",
    "wstrzymam się": "wstrzymal_sie",
}
NAME_RE = re.compile(r"^[A-ZŁŚŻŹĆĄŃÓ][\włżźćąóńś-]*(?:\s+[A-ZŁŚŻŹĆĄŃÓ][\włżźćąóńś-]*){1,4}$")


def _get(url: str, data: bytes = None) -> bytes:
    req = urllib.request.Request(url, data=data, headers=UA)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60, context=CTX) as r:
                return r.read()
        except Exception:
            if attempt == 2:
                raise
            time.sleep(2)


def _json(path: str, data: bytes = None):
    return json.loads(_get(BASE + path, data).decode("utf-8"))


def slugify(name: str) -> str:
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace("ł", "l")
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-") or "radny"


def parse_voting_pdf(pdf: bytes):
    """Zwraca listę {topic, votes:{name:cat}} z tekstowego PDF-a imiennego."""
    doc = pymupdf.open(stream=pdf, filetype="pdf")
    text = "\n".join(p.get_text() for p in doc)
    doc.close()
    text = re.sub(r"\n\s*\d+\s*\n", "\n", text)  # numery stron
    blocks = re.split(r"\n\s*(\d{1,2})\.\s+", "\n" + text)
    votes = []
    for i in range(1, len(blocks) - 1, 2):
        body = blocks[i + 1]
        tm = re.search(r"(.*?)\s*(?:Radny\s*\n\s*Oddany głos)", body, re.S)
        if not tm:
            continue
        topic = " ".join(tm.group(1).split()).rstrip(":")
        tail = body[tm.end():]
        lines = [" ".join(l.split()) for l in tail.split("\n") if l.strip()]
        per = {}
        j = 0
        while j < len(lines) - 1:
            nm, vote = lines[j], lines[j].lower()
            v2 = lines[j + 1]
            key = VOTE_MAP.get(" ".join(v2.lower().split()))
            if NAME_RE.match(nm) and key:
                per[nm] = key
                j += 2
                continue
            j += 1
        if per and topic:
            votes.append({"topic": topic[:250], "per": per})
    return votes


def main(city_dir: Path):
    docs = city_dir / "docs"
    docs.mkdir(exist_ok=True)
    d = _json(f"/index.php?id={MENU_GLOSOWANIA}&action=list-ajax",
              data=b"draw=1&start=0&length=200&search[value]=")
    recs = d.get("aaData", [])
    print(f"[hrubieszow] {len(recs)} dokumentów 'Imienne wykazy głosowań'")
    all_names, sessions, votes_out = [], [], []
    for rec in recs:
        title = " ".join(str(rec.get("tresc", "")).split())
        date = str(rec.get("data_utworzenia", ""))[:10]
        if not re.match(r"^[IVXLCDM]+\s+sesja", title, re.I) or not date or date < IX_START:
            continue
        did = rec["id_dokumentu"]
        try:
            page = _get(f"/index.php?id={MENU_GLOSOWANIA}&id_dokumentu={did}&akcja=szczegoly&p2={did}").decode("utf-8", "replace")
            m = re.search(r'href="([^"]*upload/pliki/[^"]+\.pdf)"', page, re.I)
            if not m:
                print(f"  [skip] {title} — brak pliku źródłowego")
                continue
            url = m.group(1)
            pdf = _get(url)
            vs = parse_voting_pdf(pdf)
        except Exception as e:
            print(f"  [warn] {title}: {e}")
            continue
        time.sleep(0.3)
        if not vs:
            print(f"  [skip] {title} — parser 0")
            continue
        rm = re.match(r"^([IVXLCDM]+)", title, re.I)
        roman = rm.group(1).upper() if rm else date
        idxs = {nm: n for n, nm in enumerate(all_names)}
        n_ok = 0
        for v in vs:
            nv = {"za": [], "przeciw": [], "wstrzymal_sie": [], "brak_glosu": [], "nieobecni": []}
            for nm, key in v["per"].items():
                if nm not in idxs:
                    idxs[nm] = len(all_names)
                    all_names.append(nm)
                nv[key].append(idxs[nm])
            c = {"uprawnieni": sum(len(x) for x in nv.values()),
                 **{k: len(nv[k]) for k in ("za", "przeciw", "wstrzymal_sie")}}
            votes_out.append({
                "id": f"{date}_{len(votes_out):03d}", "source_url": url,
                "session_date": date, "session_number": roman,
                "topic": v["topic"], "druk": "None",
                "resolution": "przyjete" if c["za"] > c["przeciw"] else "odrzucone",
                "counts": c, "named_votes": nv})
            n_ok += 1
        sessions.append({"date": date, "number": roman,
                         "label": f"Sesja {roman} ({date})", "vote_count": n_ok,
                         "attendee_count": None, "attendees": [], "speakers": []})
        print(f"  [ok] {title} -> {n_ok} głosów")
    sessions.sort(key=lambda s: s["date"], reverse=True)
    councilors = []
    for i, nm in enumerate(all_names):
        z = p_ = w = tot = 0
        for v in votes_out:
            nv = v["named_votes"]
            if i in nv["za"]:
                z += 1; tot += 1
            elif i in nv["przeciw"]:
                p_ += 1; tot += 1
            elif i in nv["wstrzymal_sie"]:
                w += 1; tot += 1
        councilors.append({"name": nm, "slug": slugify(nm), "club": "",
                           "za": z, "przeciw": p_, "wstrzymal_sie": w,
                           "brak_glosu": 0, "nieobecny": 0, "glosowal": tot,
                           "frekwencja": round(100 * tot / len(votes_out), 1) if votes_out else 0,
                           "aktywnosc": round(100 * tot / len(votes_out), 1) if votes_out else 0,
                           "zgodnosc_z_klubem": None, "rebellion_count": 0})
    now = datetime.now(timezone.utc).isoformat()
    kad = {"id": "2024-2029", "label": "IX kadencja (2024–2029)",
           "sessions": sessions, "votes": votes_out,
           "councilor_index": all_names, "councilors": councilors,
           "total_councilors": len(all_names), "total_votes": len(votes_out),
           "similarity_top": [], "similarity_bottom": []}
    (docs / "kadencja-2024-2029.json").write_text(
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
    data = {"scraped_at": now, "city": "Hrubieszów", "bip": BASE,
            "kadencje": [{"id": "2024-2029", "label": "IX kadencja (2024–2029)"}],
            "stats": {"sessions": len(sessions), "votes": len(votes_out),
                      "councilors": len(all_names)}}
    (docs / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[hrubieszow] DONE: {len(sessions)} sesji / {len(votes_out)} głosów / {len(all_names)} radnych")


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else Path("."))
