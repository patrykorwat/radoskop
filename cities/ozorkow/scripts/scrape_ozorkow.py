#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop Ozorków — scraper głosowań imiennych (BIP ozorkow.bip.net.pl, Nefeni Next.js).

Źródło: kategorie "Uchwały Rady Miejskiej w Ozorkowie z roku {2024,2025,2026}"
(/kategorie/{133|126|99}-...). Każdy artykuł-sesja ma załącznik
"raport z głosowań - {N} sesja RM - {data}" (https://ozorkow-api.bip.net.pl/
api/attachments/{id}) — raport SED/SDL: ponumerowane głosowania
"N. Głosowanie w sprawie ... - wyniki: ZA: n, PRZECIW: n, WSTRZYMUJĘ SIĘ: n,
BRAK GŁOSU: n, NIEOBECNI: n" + "Wyniki imienne:" z listami nazwisk w nawiasach
"Imię Nazwisko (ZA), ...". Walidacja: liczba nazwisk z nawiasów == agregaty.
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

BASE = "https://ozorkow.bip.net.pl"
API = "https://ozorkow-api.bip.net.pl/api/attachments/"
CATS = [
    "133-uchwaly-rady-miejskiej-w-ozorkowie-z-roku-2026",
    "126-uchwaly-rady-miejskiej-w-ozorkowie-z-roku-2025",
    "99-uchwaly-rady-miejskiej-w-ozorkowie-z-roku-2024",
    "51-uchwaly-rady-miejskiej-w-ozorkowie-z-roku-2023",
]
UA = {"User-Agent": "Mozilla/5.0 Radoskop/1.0 (info@radoskop.eu)"}
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

IX_START = "2024-05-07"
MONTHS = {"stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5,
          "czerwca": 6, "lipca": 7, "sierpnia": 8, "września": 9,
          "października": 10, "listopada": 11, "grudnia": 12}
ROMAN_RE = r'M{0,4}(?:CM|CD|D?C{0,3})(?:XC|XL|L?X{0,3})(?:IX|IV|V?I{0,3})'
KEYMAP = {"ZA": "za", "PRZECIW": "przeciw", "WSTRZYMUJĘ SIĘ": "wstrzymal_sie",
          "BRAK GŁOSU": "brak_glosu", "NIEOBECNI": "nieobecni"}
VOTE_TOK_RE = re.compile(r'\((ZA|PRZECIW|WSTRZYMUJĘ SIĘ|BRAK GŁOSU|NIEOBECNI)\)')


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
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or "radny"


def list_sessions():
    """[(roman, iso_date, art_path)] from category article slugs."""
    out = []
    for cat in CATS:
        h = _html(f"{BASE}/kategorie/{cat}?lang=PL")
        seen = set()
        for m in re.finditer(rf'/kategorie/{cat}/artykuly/(\d+)-sesja-nr-({ROMAN_RE})-z-dnia-(.+?)-r[^"]*\?lang=PL', h, re.I):
            aid, roman, tail = m.group(1), m.group(2).upper(), m.group(3)
            if aid in seen:
                continue
            seen.add(aid)
            dm = re.match(r'(\d+)-([a-ząęłńóśźż]+)-(\d{4})(?:-i-(?:\d+-)?([a-z]+)-(\d{4}))?', tail)
            iso = ""
            if dm and dm.group(2) in MONTHS:
                iso = f"{dm.group(3)}-{MONTHS[dm.group(2)]:02d}-{int(dm.group(1)):02d}"
            elif dm:
                # '29-pazdziernika-i-6-listopada-2024' -> second date
                dm2 = re.match(r'\d+-[a-z]+-i-(\d+)-([a-z]+)-(\d{4})', tail)
                if dm2 and dm2.group(2) in MONTHS:
                    iso = f"{dm2.group(3)}-{MONTHS[dm2.group(2)]:02d}-{int(dm2.group(1)):02d}"
            path = f"/kategorie/{cat}/artykuly/{aid}-sesja-nr-{m.group(2)}-z-dnia-{tail}-r"
            # rebuild exact href instead: re-extract raw
            out.append((roman, iso, m.group(0)))
        time.sleep(0.3)
    # normalize hrefs to full url
    fixed = []
    for roman, iso, raw in out:
        p = raw.split("?")[0]
        fixed.append((roman, iso, BASE + p + "?lang=PL"))
    return fixed


def article_report_attachment(html: str) -> tuple[str, str]:
    """(url, title) of the 'raport z głosowań' attachment."""
    best = ("", "")
    for m in re.finditer(r'"url\\?":\\?"(https://ozorkow-api\.bip\.net\.pl/api/attachments/\d+)\\?",\\?"title\\?":\\?"([^"\\]+)', html):
        url, title = m.group(1), m.group(2)
        if "głoso" in title.lower() and "raport" in title.lower():
            return url, title
        if not best[0]:
            best = (url, title)
    if not best[0]:
        atts = sorted(set(re.findall(r'https://ozorkow-api\.bip\.net\.pl/api/attachments/\d+', html)))
        if atts:
            best = (atts[0], "")
    return best


def parse_report(pdf: bytes):
    doc = pymupdf.open(stream=pdf, filetype="pdf")
    text = "\n".join(p.get_text() for p in doc)
    doc.close()
    text = re.sub(r'\s*\n\s*', ' ', text)
    votes = []
    # blocks: "N. Głosowanie w sprawie ... wyniki: ZA: a, PRZECIW: b, WSTRZYMUJĘ SIĘ: c, BRAK GŁOSU: d, NIEOBECNI: e Wyniki imienne: <names...>"
    pat = re.compile(
        r'(\d{1,3})\.\s*Głosowanie w sprawie\s+(.{5,400}?)\s*-\s*(?:czas głosowania:.*?wyniki:|wyniki:)\s*'
        r'ZA:\s*(\d+),\s*PRZECIW:\s*(\d+),\s*WSTRZYMUJĘ SIĘ:\s*(\d+),\s*BRAK GŁOSU:\s*(\d+),\s*NIEOBECNI:\s*(\d+)'
        r'(.*?)(?=\d{1,3}\.\s*Głosowanie w sprawie|$)', re.S)
    for m in pat.finditer(text):
        num, topic = m.group(1), " ".join(m.group(2).split())
        za, pr, ws, bg, nb = (int(m.group(i)) for i in range(3, 8))
        tail = m.group(8)
        wi = tail.find("Wyniki imienne")
        if wi < 0:
            continue
        names_txt = tail[wi:]
        per = {}
        # "Imię Nazwisko (ZA), Jan Kowalski (PRZECIW)..."
        for mm in re.finditer(r'([A-ZŁŚŻŹĆĄŃÓ][\w\-ŁŚŻŹĆĄŃÓśżźćąńłó]+(?:\s+[A-ZŁŚŻŹĆĄŃÓ][\w\-ŁŚŻŹĆĄŃÓśżźćąńłó]+){1,3})\s*\((ZA|PRZECIW|WSTRZYMUJĘ SIĘ|BRAK GŁOSU|NIEOBECNI)\)', names_txt):
            name = " ".join(mm.group(1).split())
            key = KEYMAP[mm.group(2)]
            if name not in per:
                per[name] = key
        tally = {k: 0 for k in ("za", "przeciw", "wstrzymal_sie", "brak_glosu", "nieobecni")}
        for k in per.values():
            tally[k] += 1
        if not per:
            continue
        ok = (tally["za"] == za and tally["przeciw"] == pr and
              tally["wstrzymal_sie"] == ws and tally["brak_glosu"] == bg and
              tally["nieobecni"] == nb)
        votes.append({"topic": topic[:300],
                      "counts": {"uprawnieni": za + pr + ws + bg + nb,
                                 "za": za, "przeciw": pr, "wstrzymal_sie": ws},
                      "per": per, "ok": ok, "tally": tally,
                      "vote_no": num})
    return votes


def main(city_dir: Path):
    docs = city_dir / "docs"
    docs.mkdir(exist_ok=True)
    sess = list_sessions()
    print(f"[ozorkow] {len(sess)} artykułów-sesji")
    all_names, sessions, votes_out = [], [], []
    for roman, iso, url in sorted(sess, key=lambda x: x[1]):
        if not iso or iso < IX_START:
            print(f"  [skip] {roman} {iso}")
            continue
        try:
            html = _html(url)
        except Exception as e:
            print(f"  [warn] art: {e}")
            continue
        aurl, atitle = article_report_attachment(html)
        if not aurl:
            print(f"  [warn] brak raportu: sesja {roman} {iso}")
            continue
        try:
            pdf = _http(aurl)
        except Exception as e:
            print(f"  [warn] dl: {e}")
            continue
        if pdf[:4] != b"%PDF":
            print(f"  [warn] nie-PDF sesja {roman}")
            continue
        try:
            pv = parse_report(pdf)
        except Exception as e:
            print(f"  [warn] parse {roman}: {e}")
            continue
        good = [v for v in pv if v["ok"]]
        if len(good) < len(pv):
            print(f"  [warn] sesja {roman}: odrzucono {len(pv)-len(good)} niespójnych")
        if not good:
            print(f"  [skip] sesja {roman} {iso} -> 0 głosów")
            continue
        for v in good:
            for nm in v["per"]:
                if nm not in all_names:
                    all_names.append(nm)
        sessions.append({"date": iso, "number": roman,
                         "label": f"Sesja {roman} ({iso})",
                         "vote_count": len(good),
                         "attendee_count": None, "attendees": [], "speakers": []})
        idx = {nm: k for k, nm in enumerate(all_names)}
        for i2, v in enumerate(good):
            nv = {"za": [], "przeciw": [], "wstrzymal_sie": [], "brak_glosu": [], "nieobecni": []}
            for nm, key in v["per"].items():
                nv[key].append(idx[nm])
            votes_out.append({
                "id": f"{iso}_{i2:03d}",
                "source_url": aurl,
                "session_date": iso, "session_number": roman,
                "topic": v["topic"], "druk": "None",
                "resolution": "przyjete" if v["counts"]["za"] > v["counts"]["przeciw"] else "odrzucone",
                "counts": v["counts"],
                "named_votes": nv,
            })
        print(f"  [ok] sesja {roman} {iso} -> {len(good)} głosów")
        time.sleep(0.3)
    councilors = []
    for nm in all_names:
        i2 = all_names.index(nm)
        z = p_ = w = b = nb = 0
        for v in votes_out:
            nv = v["named_votes"]
            if i2 in nv["za"]:
                z += 1
            elif i2 in nv["przeciw"]:
                p_ += 1
            elif i2 in nv["wstrzymal_sie"]:
                w += 1
            elif i2 in nv["brak_glosu"]:
                b += 1
            elif i2 in nv["nieobecni"]:
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
    print(f"[ozorkow] DONE: {len(sessions)} sesji, {len(votes_out)} głosów, {len(all_names)} radnych")
    if not votes_out:
        sys.exit(2)


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent)
