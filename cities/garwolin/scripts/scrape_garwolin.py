#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop Garwolin — głosowania imienne (BIP eBOI garwolin.bip.gov.pl).

Kategoria 'Imienne wykazy głosowań radnych' (?/imienne-wykazy-glosowan-radnych/,
paginacja /articles/index/imienne-wykazy-glosowan-radnych/page:N).
Kazdy artykul = jedna sesja = 'Protokół z przebiegu głosowania imiennego'
z zalacznikiem PDF (/fobjects/download/<id>/...html -> PDF) w formacie:
  LISTA RADNYCH OBECNYCH NA POSIEDZENIU (lp/nazwisko/imie/status)
  PORZĄDEK OBRAD: '<n>. <punkt>' + 'głosowanie' + temat + 'wynik' +
  'Głosowanie zakończone wynikiem: przyjęto|odrzucono' + data +
  'Podsumowanie ... ZA n 100 % ... PRZECIW n ... WSTRZYMAŁO SIĘ n' +
  'Wyniki imienne' + 'lp nazwisko imię głos' + wiersze '<lp> <Nazwisko> <Imie> <ZA|PRZECIW|WSTRZYMAŁ SIĘ|NIEOBECNY|...>'.
Parser wierszowy + walidacja licznikami z Podsumowania.
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

BASE = "https://garwolin.bip.gov.pl"
CAT = "imienne-wykazy-glosowan-radnych"
UA = {"User-Agent": "Mozilla/5.0 Radoskop/1.0 (info@radoskop.eu)"}
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE
IX_START = "2024-05-07"

MONTHS = {"stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5,
          "czerwca": 6, "lipca": 7, "sierpnia": 8, "września": 9,
          "października": 10, "listopada": 11, "grudnia": 12}
ROMAN_RE = r'M{0,4}(?:CM|CD|D?C{0,3})(?:XC|XL|L?X{0,3})(?:IX|IV|V?I{0,3})'
VOTE_MAP = {"ZA": "za", "PRZECIW": "przeciw", "WSTRZYMAŁ SIĘ": "wstrzymal_sie",
            "WSTRZYMAŁA SIĘ": "wstrzymal_sie", "WSTRZYMAŁO SIĘ": "wstrzymal_sie",
            "WSTRZYMAŁ": "wstrzymal_sie", "WSTRZYMAŁA": "wstrzymal_sie",
            "WSTRZYMAŁO": "wstrzymal_sie",
            "NIEOBECNY": "brak_glosu", "NIEOBECNA": "brak_glosu"}


def _http(url: str) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=60, context=CTX) as r:
        return r.read()


def _html(url: str) -> str:
    b = _http(url)
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return b.decode("windows-1250", "replace")


def slugify(name: str) -> str:
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace("ł", "l")
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-") or "radny"


def article_list():
    out, seen = {}, set()
    page = 0
    while page <= 10:
        url = f"{BASE}/{CAT}/" if page == 0 else f"{BASE}/articles/index/{CAT}/page:{page + 1}"
        try:
            h = _html(url)
        except Exception as e:
            print(f"  [warn] page {page}: {e}")
            break
        new = 0
        for href, title in re.findall(r'<a href="(/imienne-wykazy-glosowan-radnych/[^"]+)"[^>]*>([^<]{10,})</a>', h):
            if href in seen:
                continue
            seen.add(href)
            title = " ".join(title.split())
            m = re.search(r"(\d{1,2})\s+(\w+)\s+(\d{4})", title)
            rm = re.search(r"podczas\s+(%s)" % ROMAN_RE, title, re.I)
            mon = MONTHS.get(m.group(2).lower()) if m else None
            if not m or not mon:
                continue
            date = f"{m.group(3)}-{mon:02d}-{int(m.group(1)):02d}"
            out[href] = {"url": BASE + href, "title": title, "date": date,
                         "roman": rm.group(1).upper() if rm else ""}
            new += 1
        if new == 0 and page > 0:
            break
        page += 1
        time.sleep(0.2)
    return sorted(out.values(), key=lambda a: a["date"])


def attachment_pdf(article_url: str):
    h = _html(article_url)
    m = re.search(r'href="(/fobjects/download/\d+/[^"]+)"', h)
    if not m:
        return None, None
    return BASE + m.group(1), m.group(1)


def parse_pdf(pdf: bytes):
    d = pymupdf.open(stream=pdf, filetype="pdf")
    text = "\n".join(p.get_text() for p in d)
    d.close()
    lines = [l.strip() for l in text.split("\n")]
    lines = [l for l in lines if l]
    # roster z listy obecnosci
    roster = []  # (nazwisko, imie)
    i = 0
    votes = []
    # 1) obecny status listy —第一段 do 'Kworum'
    try:
        kq = lines.index("Kworum zostało osiągnięte")
    except ValueError:
        kq = len(lines)
    num_re = re.compile(r"^\d{1,2}$")
    for j in range(kq):
        if num_re.fullmatch(lines[j]) and j + 2 < kq and lines[j + 1] and lines[j + 2]:
            naz, ime, st = lines[j + 1], lines[j + 2], (lines[j + 3] if j + 3 < kq else "")
            if (re.fullmatch(r"[A-ZŁŚŻŹĆĄŃÓ][\włżźćąóńś-]+", naz)
                    and re.fullmatch(r"[A-ZŁŚŻŹĆĄŃÓ][\włżźćąóńś]+", ime)
                    and st in ("obecny", "obecna", "nieobecny", "nieobecna")):
                roster.append((naz, ime))
    # 2) bloki glosowan: zaczynaja sie od linii 'głosowanie'
    idxs = [j for j, l in enumerate(lines) if l == "głosowanie"]
    for n, j in enumerate(idxs):
        end = idxs[n + 1] - 1 if n + 1 < len(idxs) else len(lines)
        # temat = linie przed 'głosowanie' po numerze punktu; tu: tematy wewnatrz bloku
        blk = lines[j:end + 1]
        try:
            t_idx = blk.index("wynik")
        except ValueError:
            continue
        topic = " ".join(blk[1:t_idx]).strip()
        topic = re.sub(r"^(jednostka|Rada Miasta Garwolina)\s*", "", topic)
        res_line = blk[t_idx + 1] if t_idx + 1 < len(blk) else ""
        status = "przyjete" if "przyjęto" in res_line else ("odrzucone" if "odrzucono" in res_line else "przyjete")
        sm = re.search(r"ZA\s+(\d+)", " ".join(blk))
        pm = re.search(r"PRZECIW\s+(\d+)", " ".join(blk))
        wm = re.search(r"WSTRZYMAŁO\s+SIĘ\s+(\d+)", " ".join(blk))
        dm = re.search(r"data\s+(\d{1,2})\s+(\w+)\s+(\d{4})", " ".join(blk))
        vdate = ""
        if dm and dm.group(2).lower() in MONTHS:
            vdate = f"{dm.group(3)}-{MONTHS[dm.group(2).lower()]:02d}-{int(dm.group(1)):02d}"
        za_n, pp_n, ws_n = (int(sm.group(1)) if sm else -1, int(pm.group(1)) if pm else -1,
                            int(wm.group(1)) if wm else -1)
        # wyniki imienne
        try:
            wi = blk.index("Wyniki imienne")
        except ValueError:
            continue
        per = {"za": [], "przeciw": [], "wstrzymal_sie": [], "brak_glosu": []}
        j2 = wi
        while j2 < len(blk) and blk[j2] != "głos":
            j2 += 1
        k = j2 + 1
        ok_rows = 0
        while k < len(blk):
            l = blk[k]
            if l in ("PORZĄDEK OBRAD",) or re.match(r"^\d+\.\s", l) or l == "głosowanie":
                break
            if num_re.fullmatch(l) and k + 3 < len(blk):
                naz, ime, gl = blk[k + 1], blk[k + 2], blk[k + 3]
                consumed = 4
                if gl.upper() in ("WSTRZYMAŁ", "WSTRZYMAŁA", "WSTRZYMAŁO") and k + 4 < len(blk) and blk[k + 4] == "SIĘ":
                    gl = "WSTRZYMAŁO SIĘ"
                    consumed = 5
                key = VOTE_MAP.get(gl.upper())
                if key and re.fullmatch(r"[A-ZŁŚŻŹĆĄŃÓ][\włżźćąóńś-]+", naz):
                    per[key].append((naz, ime))
                    ok_rows += 1
                    k += consumed
                    continue
            k += 1
        parsed = (len(per["za"]), len(per["przeciw"]), len(per["wstrzymal_sie"]))
        if (za_n >= 0 and parsed != (za_n, pp_n, ws_n)) or not any(per.values()):
            print(f"    [skip-unverified] {topic[:40]} counts=({za_n},{pp_n},{ws_n}) parsed={parsed}")
            continue
        votes.append({"topic": topic[:250], "status": status, "date": vdate,
                      "za": per["za"], "przeciw": per["przeciw"],
                      "wstrz": per["wstrzymal_sie"], "abs": per["brak_glosu"],
                      "counts": {"za": za_n, "przeciw": pp_n, "wstrzymal_sie": ws_n}})
    return roster, votes


def main(city_dir: Path):
    docs = city_dir / "docs"
    docs.mkdir(exist_ok=True)
    arts = article_list()
    arts = [a for a in arts if a["date"] >= IX_START]
    print(f"[garwolin] {len(arts)} artykulow IX kadencji")
    all_names, sessions, votes_out = [], [], []

    def name_idx(pairs):
        idxs = []
        for naz, ime in pairs:
            full = f"{ime} {naz}"
            if full not in all_names:
                all_names.append(full)
            idxs.append(all_names.index(full))
        return idxs

    for a in arts:
        try:
            pdf_url, _ = attachment_pdf(a["url"])
        except Exception as e:
            print(f"  [warn] {a['url']}: {e}")
            continue
        if not pdf_url:
            print(f"  [skip] {a['title'][:60]} — brak zalacznika")
            continue
        try:
            pdf = _http(pdf_url)
        except Exception as e:
            print(f"  [warn] pdf: {e}")
            continue
        time.sleep(0.3)
        if pdf[:4] != b"%PDF":
            print(f"  [warn] nie-PDF {a['title'][:50]}")
            continue
        roster, pvotes = parse_pdf(pdf)
        name_idx(roster)
        if not pvotes:
            print(f"  [warn] 0 glosow: {a['title'][:60]}")
            continue
        present = sum(1 for naz, ime in roster)
        sessions.append({"date": a["date"], "number": a["roman"] or a["date"],
                         "label": f"Sesja {a['roman']} ({a['date']})",
                         "vote_count": len(pvotes),
                         "attendee_count": None, "attendees": [], "speakers": []})
        for i, v in enumerate(pvotes):
            votes_out.append({
                "id": f"{a['date']}_{i:03d}",
                "source_url": a["url"],
                "session_date": v["date"] or a["date"],
                "session_number": a["roman"],
                "topic": v["topic"],
                "druk": "None",
                "resolution": v["status"],
                "counts": {**v["counts"], "brak_glosu": len(v["abs"]), "nieobecni": 0,
                           "uprawnieni": sum(len(v[k]) for k in ("za", "przeciw", "wstrz", "abs"))},
                "named_votes": {
                    "za": name_idx(v["za"]),
                    "przeciw": name_idx(v["przeciw"]),
                    "wstrzymal_sie": name_idx(v["wstrz"]),
                    "brak_glosu": name_idx(v["abs"]),
                    "nieobecni": [],
                },
            })
        print(f"  [ok] {a['title'][:60]} -> {len(pvotes)} glosow")
    councilors = []
    for nm in all_names:
        i = all_names.index(nm)
        z = p_ = w = b = 0
        for v in votes_out:
            nv = v["named_votes"]
            if i in nv["za"]: z += 1
            elif i in nv["przeciw"]: p_ += 1
            elif i in nv["wstrzymal_sie"]: w += 1
            elif i in nv["brak_glosu"]: b += 1
        tot = z + p_ + w + b
        councilors.append({
            "name": nm, "club": "", "district": None,
            "votes_za": z, "votes_przeciw": p_, "votes_wstrzymal": w,
            "votes_brak": b, "votes_nieobecny": 0, "votes_total": tot,
            "frekwencja": round(100.0 * (z + p_ + w) / tot, 1) if tot else None,
            "aktywnosc": None, "zgodnosc_z_klubem": None,
            "rebellion_count": 0, "has_activity_data": False,
        })
    councilors.sort(key=lambda c: -c["votes_total"])
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
                "votes_nieobecny": 0, "votes_total": c["votes_total"],
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
    print(f"[garwolin] DONE: {len(sessions)} sesji, {len(votes_out)} glosow, {len(all_names)} radnych")
    if not votes_out:
        sys.exit(2)


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent)
