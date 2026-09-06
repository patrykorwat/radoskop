#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop Kostrzyn nad Odrą — głosowania imienne (DSSS Vote, BIP kostrzyn.nowoczesnagmina.pl).

BIP = platforma Sputnik 'nowoczesnagmina' (?c= kategorie, ?a= artykuly,
?p=document&action=show&id=N = bezposredni PDF). Kategoria 'kadencja 2024 - 2029'
(c=1229) zawiera podkategorie 'N sesja Rady Miasta ... - DD miesiac RRRR roku'.
Kazda podkategoria ma artykul 'protokół głosowań N sesji' z linkiem dokumentu PDF
generowanym z DSSS Vote App (lista obecnosci + per-glosowanie strony
'jestem za / jestem przeciw / wstrzymuję się', dwie kolumny nazwisk).
Parser kolumnowy po wspolrzednych x (X_SPLIT=345) jak Lochow.
Walidacja: liczby nazwisk == agregaty; inaczej glos odrzucany.
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

BASE = "https://kostrzyn.nowoczesnagmina.pl"
KADENCJA_CAT = 1229
UA = {"User-Agent": "Mozilla/5.0 Radoskop/1.0 (info@radoskop.eu)"}
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE
IX_START = "2024-05-07"
LQ = "\u201c\u201e\u201f"
RQ = "\u201d\u2019"
X_SPLIT = 345

MONTHS = {"stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5,
          "czerwca": 6, "lipca": 7, "sierpnia": 8, "września": 9,
          "października": 10, "listopada": 11, "grudnia": 12}
ROMAN_RE = r'M{0,4}(?:CM|CD|D?C{0,3})(?:XC|XL|L?X{0,3})(?:IX|IV|V?I{0,3})'


def _http(url: str) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=60, context=CTX) as r:
        return r.read()


def _html(url: str) -> str:
    return _http(url).decode("utf-8", "replace")


def slugify(name: str) -> str:
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace("ł", "l")
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-") or "radny"


def session_list():
    h = _html(f"{BASE}/?c={KADENCJA_CAT}")
    out = []
    for cid, title in re.findall(r'<a href="\?c=(\d+)" class=""><span>([^<]+)</span></a>', h):
        m = re.search(rf"^({ROMAN_RE}) sesja .*?-\s*(\d{{1,2}})\s+(\w+)\s+(\d{{4}})", title.strip())
        if not m:
            continue
        mon = MONTHS.get(m.group(3).lower())
        if not mon:
            continue
        date = f"{m.group(4)}-{mon:02d}-{int(m.group(2)):02d}"
        if date < IX_START:
            continue
        out.append({"cid": int(cid), "roman": m.group(1).upper(), "date": date,
                    "title": " ".join(title.split())})
    return sorted(out, key=lambda s: s["date"])


def vote_pdf_for_session(cid: int):
    """Znajdz artykul 'protokół głosowań' w kategorii sesji -> URL dokumentu PDF."""
    h = _html(f"{BASE}/?c={cid}")
    aids = [a for a, t in re.findall(r'<a href="\?a=(\d+)" class="blue">([^<]+)</a>', h)
            if "głosow" in t.lower() or "glosow" in t.lower()]
    if not aids:
        return None
    for aid in aids:
        art = _html(f"{BASE}/?a={aid}")
        docs = re.findall(r'href="\?p=document&amp;action=show&amp;id=(\d+)&amp;bar_id=(\d+)"', art)
        if not docs:
            docs = re.findall(r'href="\?p=document&action=show&id=(\d+)&bar_id=(\d+)"', art)
        if docs:
            did, bar = docs[0]
            return f"{BASE}/?p=document&action=show&id={did}&bar_id={bar}"
    return None


def _rows(page):
    rows = {}
    for w in page.get_text("words"):
        rows.setdefault(round(w[1]), []).append((round(w[0]), w[4]))
    return {y: sorted(v) for y, v in rows.items()}


def _name(line_words):
    toks = [w for _, w in line_words if not re.fullmatch(r"\d+\.", w) and w != "BRAK"]
    return " ".join(toks).strip()


def parse_dsss(pdf_bytes: bytes):
    d = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    roster, votes, session = [], [], {}
    for p in d:
        t = " ".join(p.get_text().split())
        m = re.search(rf"Na sesji [{LQ}](.*?)[{RQ}] stawiło się (\d+) radnych", t)
        if m:
            session = {"title": m.group(1), "present": int(m.group(2))}
            for y, line in _rows(p).items():
                if line and re.fullmatch(r"\d+\.", line[0][1]):
                    nm = _name([wx for wx in line if wx[0] < 280])
                    if nm and nm not in roster:
                        roster.append(nm)
            continue
        m = re.search(
            rf'(Uchwała numer (\S+) [{LQ}](.*?)[{RQ}]|Wniosek [^{LQ}]*?[{LQ}](.*?)[{RQ}]|'
            rf'Przeprowadzono głosowanie w ?sprawy [{LQ}](.*?)[{RQ}])'
            r'.*?proporcją głosów: jestem za (\d+), jestem przeciw (\d+), wstrzymuję się (\d+)', t)
        if not m:
            continue
        topic = m.group(3) or m.group(4) or m.group(5) or ""
        num = m.group(2) or ""
        za_n, pp_n, ws_n = int(m.group(6)), int(m.group(7)), int(m.group(8))
        head = t[:t.find("proporcją")] if "proporcją" in t else ""
        status = "przyjete"
        if za_n <= pp_n:
            status = "odrzucone" if re.search(r"nie (został|została|zostali)", head) else "przyjete"
        dm = re.search(r"Data i godzina głosowania: (\d{2})\.(\d{2})\.(\d{4})", t)
        vdate = f"{dm.group(3)}-{dm.group(2)}-{dm.group(1)}" if dm else ""
        rows = _rows(p)
        ys = sorted(rows)
        y_za = y_wsz = y_abs = y_foot = None
        for y in ys:
            txt = " ".join(w for _, w in rows[y])
            if "Jestem za" in txt and "Jestem przeciw" in txt and y_za is None:
                y_za = y
            if any(x < 300 and "Wstrzymuję" in w for x, w in rows[y]) and y_za and y > y_za and y_wsz is None:
                y_wsz = y
            if any(x >= 300 and ("Obecni" in w or "udziału" in w) for x, w in rows[y]) and y_za and y > y_za and y_abs is None:
                y_abs = y
            if any("Operatorem" in w or "Wygenerowano" in w for _, w in rows[y]):
                y_foot = y
                break
        za_l, pp_l, ws_l, abs_l = [], [], [], []
        if y_za:
            y_wsz_lim = y_wsz or y_foot or 99999
            y_abs_lim = y_abs or y_foot or 99999
            y_foot_lim = y_foot or 99999
            for y in ys:
                if y <= y_za + 4:
                    continue
                line = rows[y]
                n1 = _name([wx for wx in line if wx[0] < X_SPLIT])
                n2 = _name([wx for wx in line if wx[0] >= X_SPLIT])
                if y_za < y < min(y_wsz_lim, y_foot_lim) and n1 and "Jestem" not in n1 and "Wstrzymuję" not in n1:
                    za_l.append(n1)
                if y_za < y < min(y_abs_lim, y_foot_lim) and n2 and "Obecni" not in n2 and "udziału" not in n2 and "głosowaniu" not in n2:
                    pp_l.append(n2)
                if y_wsz and y_wsz_lim < y < y_foot_lim and n1:
                    ws_l.append(n1)
                if y_abs and y_abs_lim < y < y_foot_lim and n2:
                    abs_l.append(n2)
        ok = (len(za_l) == za_n and len(pp_l) == pp_n and len(ws_l) == ws_n)
        if not ok:
            print(f"    [skip-unverified] {num or topic[:40]} counts=({za_n},{pp_n},{ws_n}) parsed=({len(za_l)},{len(pp_l)},{len(ws_l)})")
            continue
        votes.append({"topic": topic, "num": num, "status": status, "date": vdate,
                      "za": za_l, "przeciw": pp_l, "wstrz": ws_l, "abs": abs_l,
                      "counts": {"za": za_n, "przeciw": pp_n, "wstrzymal_sie": ws_n}})
    return session, roster, votes


def main(city_dir: Path):
    docs = city_dir / "docs"
    docs.mkdir(exist_ok=True)
    sess_list = session_list()
    print(f"[kostrzyn] {len(sess_list)} sesji IX kadencji")
    all_names, sessions, votes_out = [], [], []
    for s in sess_list:
        try:
            pdf_url = vote_pdf_for_session(s["cid"])
        except Exception as e:
            print(f"  [warn] list {s['cid']}: {e}")
            continue
        if not pdf_url:
            print(f"  [skip] {s['title'][:60]} — brak protokołu głosowań")
            continue
        try:
            pdf = _http(pdf_url)
        except Exception as e:
            print(f"  [warn] pdf {s['cid']}: {e}")
            continue
        time.sleep(0.3)
        if pdf[:4] != b"%PDF":
            print(f"  [warn] nie-PDF {s['cid']}")
            continue
        sess, roster, pvotes = parse_dsss(pdf)
        for nm in roster:
            if nm not in all_names:
                all_names.append(nm)
        if not pvotes:
            print(f"  [warn] 0 glosow: {s['title'][:60]}")
            continue
        present = sess.get("present") or 0
        sessions.append({"date": s["date"], "number": s["roman"],
                         "label": f"Sesja {s['roman']} ({s['date']})",
                         "vote_count": len(pvotes),
                         "attendee_count": present or None,
                         "attendees": [], "speakers": []})
        for i, v in enumerate(pvotes):
            idx = {nm: n for n, nm in enumerate(all_names)}
            votes_out.append({
                "id": f"{s['date']}_{i:03d}",
                "source_url": pdf_url,
                "session_date": v["date"] or s["date"],
                "session_number": s["roman"],
                "topic": v["topic"][:250],
                "druk": v["num"] or "None",
                "resolution": v["status"],
                "counts": {**v["counts"], "brak_glosu": len(v["abs"]), "nieobecni": 0,
                           "uprawnieni": len(v["za"]) + len(v["przeciw"]) + len(v["wstrz"]) + len(v["abs"])},
                "named_votes": {
                    "za": [idx[n] for n in v["za"] if n in idx],
                    "przeciw": [idx[n] for n in v["przeciw"] if n in idx],
                    "wstrzymal_sie": [idx[n] for n in v["wstrz"] if n in idx],
                    "brak_glosu": [idx[n] for n in v["abs"] if n in idx],
                    "nieobecni": [],
                },
            })
        print(f"  [ok] {s['title'][:60]} -> {len(pvotes)} glosow")
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
    print(f"[kostrzyn] DONE: {len(sessions)} sesji, {len(votes_out)} glosow, {len(all_names)} radnych")
    if not votes_out:
        sys.exit(2)


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent)
