#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop Działdowo — scraper głosowań imiennych (BIP bip.dzialdowo.eu, CMS Logonet).

Źródło: BIP Urzędu Miasta Działdowo, kategoria "Protokoły głosowań" →
artykuł "Protokoły głosowań - IX kadencja - lata 2024-2029" (artykul/954/5230).
Załączniki = po jednym PDF-ie na sesję; w każdym PDF-ie jedna strona =
jedno głosowanie imienne: tabela "Lp | Nazwisko i imię | Za | Przeciw |
Wstrzymujące się | Nie głosowano | Nieobecność" ze znakami X w kolumnach
(lub napisem "Nieobecność"), nagłówek z tematem + stopka "Wyniki głosowania:
ZA n PRZECIW - WSTRZYMUJĄCE - NIE GŁOSOWANO -".
Parser kolumnowy per-strona: granice kolumn z pozycji x nagłówków (układ
różni się między sesjami — część bez kolumny Nieobecność). Walidacja:
liczby w kolumnach == agregaty ze stopki; głos niespójny odrzucany.
Roster = unia nazwisk z PDF-ów (dynamiczny, wymiany w trakcie kadencji).
"""
import json
import re
import ssl
import sys
import unicodedata
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pymupdf

BASE = "https://bip.dzialdowo.eu"
IX_URL = BASE + "/artykul/954/5230/protokoly-glosowan-ix-kadencja-lata-2024-2029"
UA = {"User-Agent": "Mozilla/5.0 Radoskop/1.0 (info@radoskop.eu)"}
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

IX_START = "2024-05-07"
MONTHS = {"stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5,
          "czerwca": 6, "lipca": 7, "sierpnia": 8, "września": 9,
          "października": 10, "listopada": 11, "grudnia": 12}
ROMAN_RE = r'M{0,4}(?:CM|CD|D?C{0,3})(?:XC|XL|L?X{0,3})(?:IX|IV|V?I{0,3})'


def _http(url: str) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=60, context=CTX) as r:
        return r.read()


def _html(url: str) -> str:
    raw = _http(url)
    m = re.search(rb'charset=["\']?([\w-]+)', raw[:3000], re.I)
    enc = m.group(1).decode('ascii', 'ignore').lower() if m else None
    for e in [enc, 'utf-8', 'cp1250']:
        if not e:
            continue
        try:
            return raw.decode(e)
        except Exception:
            pass
    return raw.decode('utf-8', 'replace')


def slugify(name: str) -> str:
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace("ł", "l")
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or "radny"


def _norm(t: str) -> str:
    return t.strip('„”"\u201e\u201f\u201d\u2019 ').lower()


def _col_cols(page):
    """Granice kolumn głosowań z pozycji x nagłówków w pasie nagłówka tabeli."""
    words = page.get_text("words")
    lp_y = None
    for w in words:
        if w[4].rstrip('.') == 'Lp':
            lp_y = w[1]
            break
    if lp_y is None:
        return None
    band = [w for w in words if lp_y - 40 <= w[1] <= lp_y + 30]
    pos = {}
    for w in band:
        t = _norm(w[4])
        cx = (w[0] + w[2]) / 2.0
        if t == 'za' and 'za' not in pos:
            pos['za'] = cx
        elif t == 'przeciw' and 'przeciw' not in pos:
            pos['przeciw'] = cx
        elif t.startswith('wstrzy') and 'wstrzymal_sie' not in pos:
            pos['wstrzymal_sie'] = cx
        elif (t.startswith('głosowa') or t == 'głosowano') and 'brak_glosu' not in pos:
            pos['brak_glosu'] = cx
        elif t.startswith('nieobecno') and 'nieobecni' not in pos:
            pos['nieobecni'] = cx
    if not ('za' in pos and 'przeciw' in pos and 'wstrzymal_sie' in pos):
        return None
    order = [k for k in ('za', 'przeciw', 'wstrzymal_sie', 'brak_glosu', 'nieobecni') if k in pos]
    xs = [pos[k] for k in order]
    bounds = []
    for i, k in enumerate(order):
        lo = xs[i] - 30 if i == 0 else (xs[i - 1] + xs[i]) / 2.0
        hi = 9999 if i == len(order) - 1 else (xs[i] + xs[i + 1]) / 2.0
        bounds.append((k, lo, hi))
    return min(w[0] for w in band if _norm(w[4]) == 'za') - 30, bounds


def _rows(page):
    r = {}
    for w in page.get_text("words"):
        r.setdefault(round(w[1] / 3) * 3, []).append((round(w[0]), w[4]))
    return {y: sorted(v) for y, v in r.items()}


class _OcrPage:
    """Shim: strona zeskanowana (bez warstwy tekstu) po OCR — API jak strona PDF."""
    def __init__(self, text, words):
        self._t, self._w = text, words

    def get_text(self, mode="text"):
        return self._w if mode == "words" else self._t

    def get_images(self):
        return []


def _ocr_page(pg):
    """Render + tesseract -l pol (TSV z koordynatami) -> _OcrPage w punktach PDF."""
    import subprocess
    import tempfile
    import os
    try:
        pix = pg.get_pixmap(dpi=150)
    except Exception:
        return None
    sx = pg.rect.width / pix.width
    sy = pg.rect.height / pix.height
    with tempfile.TemporaryDirectory() as td:
        png = os.path.join(td, "p.png")
        pix.save(png)
        try:
            out = subprocess.run(["tesseract", png, "stdout", "-l", "pol", "tsv"],
                                 capture_output=True, text=True, timeout=120).stdout
        except Exception:
            return None
    words, texts = [], []
    for line in out.splitlines()[1:]:
        c = line.split("\t")
        if len(c) < 12 or c[11].strip() == "":
            continue
        try:
            l, t_, w_, h_ = float(c[6]), float(c[7]), float(c[8]), float(c[9])
        except ValueError:
            continue
        txt = c[11].strip()
        texts.append(txt)
        words.append((l * sx, t_ * sy, (l + w_) * sx, (t_ + h_) * sy, txt, 0, 0, len(words)))
    if not words:
        return None
    return _OcrPage(" ".join(texts), words)


def _norm_name(n: str) -> str:
    # PDF układa "NAZWISKO Imię" (nazwisko wersalikami) -> "Imię Nazwisko"
    parts = n.split()
    if len(parts) == 2 and parts[0].upper() == parts[0] and parts[1].upper() != parts[1]:
        surname = parts[0].capitalize()
        if "-" in surname:
            surname = "-".join(p.capitalize() for p in surname.split("-"))
        return f"{parts[1]} {surname}"
    return " ".join(parts)


def parse_session_pdf(pdf_bytes: bytes):
    """-> list głosów z jednego PDF-a sesji (1 strona = 1 głosowanie). None = format nieznany."""
    d = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    votes = []
    for pg in d:
        t = " ".join(pg.get_text().split())
        if not t and pg.get_images():
            ocr = _ocr_page(pg)
            if ocr is None:
                return None
            pg = ocr
            t = pg.get_text()
        mh = re.search(r'w dniu (\d{1,2}) (\w+) (\d{4}) r\s*\.?\s*[–-]?\s*(.+?)\s*Lp\.', t)
        ma = re.search(r'Wyniki głosowania:\s*[„"]ZA[”"]\s*(\d+|-)\s*[„"]PRZECIW[”"]\s*(\d+|-)'
                       r'\s*[„"]WSTRZYMUJĄCE[”"]?\s*(\d+|-)?\s*NIE GŁOSOWANO\s*(\d+|-)?', t)
        cb = _col_cols(pg)
        if not (mh and ma and cb):
            return None
        day, mon, yr, topic = mh.groups()
        if mon not in MONTHS:
            return None
        agg = {}
        for k, g in zip(("za", "przeciw", "wstrzymal_sie", "brak_glosu"), ma.groups()):
            agg[k] = 0 if (g is None or g == "-") else int(g)
        name_xmax, bounds = cb
        rr = _rows(pg)
        parsed = {k: [] for k in ("za", "przeciw", "wstrzymal_sie", "brak_glosu", "nieobecni")}
        for y in sorted(rr):
            line = rr[y]
            toks = " ".join(w for _, w in line)
            if not re.match(r'^\d+\.\s+\S', toks):
                continue
            nm = [w for x, w in line if x < name_xmax and _norm(w) != 'się'
                  and not re.fullmatch(r'\d+\.', w)]
            if not nm:
                continue
            name = _norm_name(" ".join(nm))
            cat = None
            for x, w in line:
                if x < name_xmax:
                    continue
                if _norm(w).startswith('nieobecno'):
                    cat = 'nieobecni'
                    break
                if w.strip() == 'X':
                    for k, lo, hi in bounds:
                        if lo <= x < hi:
                            cat = k
                            break
            if cat is None:
                # wiersz bez żadnego znaku (myślniki = brak głosu, nie liczony)
                continue
            parsed[cat].append(name)
        ok = all(len(parsed[k]) == agg.get(k, 0) for k in ("za", "przeciw", "wstrzymal_sie", "brak_glosu"))
        votes.append({
            "topic": topic.strip(" –"),
            "date": f"{yr}-{MONTHS[mon]:02d}-{int(day):02d}",
            "counts": {**agg, "nieobecni": len(parsed["nieobecni"])},
            "za": parsed["za"], "przeciw": parsed["przeciw"],
            "wstrz": parsed["wstrzymal_sie"], "brak": parsed["brak_glosu"],
            "nieob": parsed["nieobecni"], "ok": ok,
        })
    return votes


def session_attachments():
    """(attachment_url, tytuł) all session PDFs of IX kadencja."""
    h = _html(IX_URL)
    out, seen = [], set()
    for u, title in re.findall(r'href="(https://bip\.dzialdowo\.eu/attachments/download/\d+)"[^>]*>([^<]{5,140})<', h):
        title = " ".join(title.split())
        if u in seen:
            continue
        seen.add(u)
        out.append((u, title))
    return out


def main(city_dir: Path):
    docs = city_dir / "docs"
    docs.mkdir(exist_ok=True)
    atts = session_attachments()
    print(f"[dzialdowo] {len(atts)} zalacznikow sesji")
    all_names, sessions, votes_out = [], [], []
    for url, title in atts:
        ms = re.search(rf"({ROMAN_RE})\s+(?:nadzwyczajnej\s+)?sesja\s*(\d{{2}}-\d{{2}}-\s*\d{{4}})", title)
        if not ms:
            ms2 = re.search(r"sesja\s+(\d{2})-(\d{2})-\s*(\d{4})", title)
            roman, date = "", (f"{ms2.group(3)}-{ms2.group(2)}-{ms2.group(1)}" if ms2 else "")
        else:
            roman = ms.group(1)
            d, m_, y = re.sub(r"\s", "", ms.group(2)).split("-")
            date = f"{y}-{m_}-{d}"
        if not date or date < IX_START:
            print(f"  [skip] {title[:60]} (data {date})")
            continue
        try:
            pdf = _http(url)
        except Exception as e:
            print(f"  [warn] dl {url}: {e}")
            continue
        if pdf[:4] != b"%PDF":
            print(f"  [warn] nie-PDF: {title[:50]}")
            continue
        try:
            pvotes = parse_session_pdf(pdf)
        except Exception as e:
            print(f"  [warn] parse {title[:50]}: {e}")
            continue
        if pvotes is None:
            print(f"  [warn] format nieznany: {title[:60]}")
            continue
        good = [v for v in pvotes if v["ok"]]
        if len(good) < len(pvotes):
            print(f"  [warn] {title[:50]}: odrzucono {len(pvotes)-len(good)} niespójnych głosów")
        if not good:
            continue
        for v in good:
            for nm in (v["za"] + v["przeciw"] + v["wstrz"] + v["brak"] + v["nieob"]):
                if nm not in all_names:
                    all_names.append(nm)
        sessions.append({"date": date, "number": roman or date,
                         "label": f"Sesja {roman} ({date})" if roman else f"Sesja ({date})",
                         "vote_count": len(good),
                         "attendee_count": None, "attendees": [], "speakers": []})
        idx = {nm: n for n, nm in enumerate(all_names)}
        for i, v in enumerate(good):
            votes_out.append({
                "id": f"{date}_{i:03d}",
                "source_url": IX_URL,
                "session_date": date,
                "session_number": roman,
                "topic": v["topic"],
                "druk": "None",
                "resolution": "przyjete" if v["counts"]["za"] > v["counts"]["przeciw"] else "odrzucone",
                "counts": v["counts"],
                "named_votes": {
                    "za": [idx[n] for n in v["za"] if n in idx],
                    "przeciw": [idx[n] for n in v["przeciw"] if n in idx],
                    "wstrzymal_sie": [idx[n] for n in v["wstrz"] if n in idx],
                    "brak_glosu": [idx[n] for n in v["brak"] if n in idx],
                    "nieobecni": [idx[n] for n in v["nieob"] if n in idx],
                },
            })
        print(f"  [ok] {title[:70]} -> {len(good)} głosów")
    councilors = []
    for nm in all_names:
        i = all_names.index(nm)
        z = p_ = w = b = nb = 0
        for v in votes_out:
            nv = v["named_votes"]
            if i in nv["za"]:
                z += 1
            elif i in nv["przeciw"]:
                p_ += 1
            elif i in nv["wstrzymal_sie"]:
                w += 1
            elif i in nv["brak_glosu"]:
                b += 1
            elif i in nv["nieobecni"]:
                nb += 1
        tot = z + p_ + w + b
        councilors.append({
            "name": nm, "club": "", "district": None,
            "votes_za": z, "votes_przeciw": p_, "votes_wstrzymal": w,
            "votes_brak": b, "votes_nieobecny": nb, "votes_total": tot,
            "frekwencja": round(100.0 * (z + p_ + w) / tot, 1) if tot else None,
            "aktywnosc": None, "zgodnosc_z_klubem": None,
            "rebellion_count": 0, "has_activity_data": False,
        })
    councilors.sort(key=lambda c: -c["votes_total"])
    sessions.sort(key=lambda s: s["date"], reverse=True)
    kad = {
        "id": "2024-2029", "label": "IX kadencja (2024–2029)",
        "names_normalized": True, "clubs": {},
        "sessions": sessions, "total_sessions": len(sessions),
        "total_votes": len(votes_out), "total_councilors": len(all_names),
        "councilors": councilors, "votes": votes_out,
        "similarity_top": [], "similarity_bottom": [],
        "councilor_index": list(all_names),
    }
    (docs / "kadencja-2024-2029.json").write_text(
        json.dumps(kad, ensure_ascii=False, indent=1), encoding="utf-8")
    profiles = {"scraped_at": datetime.now(timezone.utc).isoformat(), "profiles": [],
                "total": len(councilors)}
    for c in councilors:
        profiles["profiles"].append({
            "name": c["name"], "slug": slugify(c["name"]), "club": "",
            "role": "", "photo_url": "", "bio": "", "email": "",
            "social_links": {},
            "kadencje": {"2024-2029": {
                "club": "", "frekwencja": c["frekwencja"],
                "aktywnosc": 0, "zgodnosc_z_klubem": None,
                "votes_za": c["votes_za"], "votes_przeciw": c["votes_przeciw"],
                "votes_wstrzymal": c["votes_wstrzymal"], "votes_brak": c["votes_brak"],
                "votes_nieobecny": c["votes_nieobecny"], "votes_total": c["votes_total"],
                "rebellion_count": 0, "rebellions": [],
                "has_voting_data": True, "has_activity_data": False,
                "former": False, "mid_term": False}},
        })
    (docs / "profiles.json").write_text(
        json.dumps(profiles, ensure_ascii=False, indent=1), encoding="utf-8")
    (docs / "data.json").write_text(json.dumps({
        "generated": datetime.now(timezone.utc).isoformat(),
        "default_kadencja": "2024-2029",
        "kadencje": [{"id": "2024-2029", "label": "IX kadencja (2024–2029)"}],
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[dzialdowo] DONE: {len(sessions)} sesji, {len(votes_out)} głosów, {len(all_names)} radnych")
    if not votes_out:
        sys.exit(2)


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent)
