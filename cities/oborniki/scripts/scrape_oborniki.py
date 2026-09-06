#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop Oborniki — głosowania imienne (BIP Madkom bip.umoborniki.nv.pl, API JSON).

BIP = React-SPA Madkom z jawnym API JSON:
  /api/menu/{id}/submenu            podkategorie
  /api/menu/{id}/articles?limit=..  artykuły kategorii (Uchwały RM menu 135 -> lata 409/361/335)
  /api/articles/{id}                artykuł + attachments[]
Każdy artykuł "Sesja <RZYMSKA> <data> r." ma załącznik "Wyniki głosowań"
(/e,pobierz,get.html?id=N) = eSesja-print TEXT: per głosowanie
  "Głosowano w sprawie: <temat>"
  " ZA: n, PRZECIW: n, WSTRZYMUJĘ SIĘ: n, BRAK GŁOSU: n, NIEOBECNI: n"
  "Wyniki imienne:" / "ZA (n)" / listy nazwisk po przecinkach / "Głosowanie z dnia: DD.MM.RRRR"
Walidacja: liczba nazwisk per kategoria == licznik w nagłówku.

Użycie: python scrape_oborniki.py [city_dir]
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

BASE = "https://bip.umoborniki.nv.pl"
MENU_UCHWALY = "135"
UA = {"User-Agent": "Mozilla/5.0 Radoskop/1.0 (info@radoskop.eu)"}
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE
IX_START = "2024-05-07"
MONTHS = {"stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5,
          "czerwca": 6, "lipca": 7, "sierpnia": 8, "września": 9,
          "października": 10, "listopada": 11, "grudnia": 12}


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=60, context=CTX) as r:
        return r.read()


def _json(path: str):
    return json.loads(_get(BASE + path).decode("utf-8"))


def slugify(name: str) -> str:
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace("ł", "l")
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-") or "radny"


def parse_date(text: str) -> str:
    m = re.search(r"(\d{1,2})\s+(" + "|".join(MONTHS) + r")\s+(\d{4})", text, re.I)
    if m:
        return f"{m.group(3)}-{MONTHS[m.group(2).lower()]:02d}-{int(m.group(1)):02d}"
    m = re.search(r"(\d{1,2})\.(\d{2})\.(\d{4})", text)
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1).zfill(2)}"
    return ""


CAT_MAP = {"ZA": "za", "PRZECIW": "przeciw", "WSTRZYMUJĘ SIĘ": "wstrzymal_sie",
           "WSTRZYMUJE SIE": "wstrzymal_sie", "BRAK GŁOSU": "brak_glosu",
           "BRAK GLOSU": "brak_glosu", "NIEOBECNI": "nieobecni"}


def parse_wyniki(pdf: bytes):
    doc = pymupdf.open(stream=pdf, filetype="pdf")
    text = "\n".join(p.get_text() for p in doc)
    doc.close()
    # złącz przeniesienia "Witek-\nStefańska" i złamania linii
    text = re.sub(r"-\n(?=[A-ZŁŚŻŹĆĄŃÓ])", "-", text)
    votes = []
    blocks = re.split(r"\nWyniki głosowania\n", "\n" + text)
    for b in blocks[1:] if len(blocks) > 1 else blocks:
        gm = re.search(r"Głosowano w sprawie:\s*(.+?)\s*\n\s*(ZA:\s*\d+,.+?)\n", b, re.S)
        if not gm:
            continue
        topic = " ".join(gm.group(1).split())
        agg_line = " ".join(gm.group(2).split())
        counts = dict(re.findall(r"(ZA|PRZECIW|WSTRZYMUJĘ SIĘ|BRAK GŁOSU|NIEOBECNI):\s*(\d+)", agg_line))
        dm = re.search(r"Głosowanie z dnia:\s*(\d{2})\.(\d{2})\.(\d{4})", b)
        date = f"{dm.group(3)}-{dm.group(2)}-{dm.group(1)}" if dm else ""
        if not date:
            continue
        # sekcje imienne — finditer (puste sekcje '(0)' nie zjadają następnego nagłówka)
        hdr = re.compile(r"\n(WSTRZYMUJĘ SIĘ|WSTRZYMUJE SIE|ZA|PRZECIW|BRAK GŁOSU|BRAK GLOSU|NIEOBECNI)\s*\((\d+)\)")
        heads = list(hdr.finditer("\n" + b))
        full = "\n" + b
        per = {}
        for j, h in enumerate(heads):
            key_raw, n_decl = h.group(1), int(h.group(2))
            key = CAT_MAP.get(key_raw.upper())
            if not key:
                continue
            end = heads[j + 1].start() if j + 1 < len(heads) else len(full)
            names_txt = " ".join(full[h.end():end].split())
            names_txt = re.split(r"Głosowanie z dnia:|Wygenerowano", names_txt)[0]
            names = [x.strip(" .") for x in names_txt.split(",") if x.strip(" .")]
            names = [re.sub(r"\s+", " ", x) for x in names if re.match(r"^[A-ZŁŚŻŹĆĄŃÓ][\włżźćąóńś-]+", x)]
            if len(names) != n_decl:
                per = None
                break
            per[key] = names
        if per is None:
            continue
        votes.append({"date": date, "topic": topic[:250], "per": per,
                      "counts": {CAT_MAP.get(k, k): int(v) for k, v in counts.items()}})
    return votes


def main(city_dir: Path):
    docs = city_dir / "docs"
    docs.mkdir(exist_ok=True)
    subs = _json(f"/api/menu/{MENU_UCHWALY}/submenu")
    years = subs if isinstance(subs, list) else subs.get("data", [])
    art_ids = []
    for y in years:
        yid, yname = str(y.get("id")), str(y.get("name", ""))
        ym = re.search(r"(\d{4})", yname)
        if not ym or ym.group(1) < "2024":
            continue
        off = 0
        while True:
            d = _json(f"/api/menu/{yid}/articles?limit=50&offset={off}")
            arts = d.get("articles", [])
            for a in arts:
                t = ""
                for al in a.get("aliasFields") or []:
                    if al.get("alias") == "title":
                        t = str(al.get("value", ""))
                if not t:
                    for cf in a.get("columnFields") or []:
                        if cf.get("fieldId") == 22:
                            t = str(cf.get("value", ""))
                t = " ".join(t.split())
                if re.match(r"^S[ae]sja\b", t, re.I):
                    art_ids.append((str(a["id"]), t))
            off += len(arts)
            if len(arts) < 50 or off >= d.get("total", 0):
                break
        time.sleep(0.2)
    print(f"[oborniki] {len(art_ids)} artykułów 'Sesja*'")
    all_names, sessions, votes_out = [], [], []
    seen_pdf = set()
    for aid, title in art_ids:
        date = parse_date(title)
        if not date or date < IX_START:
            continue
        try:
            art = _json(f"/api/articles/{aid}")
        except Exception as e:
            print(f"  [warn] art {aid}: {e}")
            continue
        atts = [a for a in art.get("attachments", [])
                if str(a.get("name", "")).strip().lower().startswith("wyniki głosowań")]
        if not atts:
            print(f"  [skip] {title} — brak 'Wyniki głosowań'")
            continue
        att = atts[0]
        url = BASE + "/" + att["link"].lstrip("/")
        if url in seen_pdf:
            continue
        seen_pdf.add(url)
        try:
            vs = parse_wyniki(_get(url))
        except Exception as e:
            print(f"  [warn] parse {title}: {e}")
            continue
        time.sleep(0.25)
        if not vs:
            print(f"  [skip] {title} — parser 0")
            continue
        rm = re.search(r"S[ae]sja\s+(?:\w+\s+)?([IVXLCDM]+)", title, re.I)
        roman = rm.group(1).upper() if rm else date
        idx = {nm: n for n, nm in enumerate(all_names)}
        n_ok = 0
        for v in vs:
            nv = {"za": [], "przeciw": [], "wstrzymal_sie": [], "brak_glosu": [], "nieobecni": []}
            for k, names in v["per"].items():
                for nm in names:
                    if nm not in idx:
                        idx[nm] = len(all_names)
                        all_names.append(nm)
                    nv[k].append(idx[nm])
            c = {"uprawnieni": sum(len(x) for x in nv.values()),
                 **{k: len(v["per"].get(k, [])) for k in ("za", "przeciw", "wstrzymal_sie")}}
            votes_out.append({
                "id": f"{v['date']}_{len(votes_out):03d}", "source_url": url,
                "session_date": v["date"], "session_number": roman,
                "topic": v["topic"], "druk": "None",
                "resolution": "przyjete" if c["za"] > c["przeciw"] else "odrzucone",
                "counts": c, "named_votes": nv})
            n_ok += 1
        sessions.append({"date": date, "number": roman,
                         "label": f"Sesja {roman} ({date})", "vote_count": n_ok,
                         "attendee_count": None, "attendees": [], "speakers": []})
        print(f"  [ok] {title} -> {n_ok} głosów")
    councilors = []
    for nm in all_names:
        i = all_names.index(nm)
        z = p_ = w = b_ = nb = 0
        for v in votes_out:
            nv = v["named_votes"]
            if i in nv["za"]:
                z += 1
            elif i in nv["przeciw"]:
                p_ += 1
            elif i in nv["wstrzymal_sie"]:
                w += 1
            elif i in nv["brak_glosu"]:
                b_ += 1
            elif i in nv["nieobecni"]:
                nb += 1
        tot = z + p_ + w + b_
        councilors.append({"name": nm, "club": "", "district": None,
                           "votes_za": z, "votes_przeciw": p_, "votes_wstrzymal": w,
                           "votes_brak": b_, "votes_nieobecny": nb, "votes_total": tot,
                           "frekwencja": round(100.0 * (z + p_ + w) / tot, 1) if tot else None,
                           "aktywnosc": None, "zgodnosc_z_klubem": None,
                           "rebellion_count": 0, "has_activity_data": False})
    councilors.sort(key=lambda c: -c["votes_total"])
    sessions.sort(key=lambda x: x["date"], reverse=True)
    kad = {"id": "2024-2029", "label": "IX kadencja (2024–2029)",
           "names_normalized": True, "clubs": {},
           "sessions": sessions, "total_sessions": len(sessions),
           "total_votes": len(votes_out), "total_councilors": len(all_names),
           "councilors": councilors, "votes": votes_out,
           "similarity_top": [], "similarity_bottom": [],
           "councilor_index": list(all_names)}
    (docs / "kadencja-2024-2029.json").write_text(
        json.dumps(kad, ensure_ascii=False, indent=1), encoding="utf-8")
    profiles = {"scraped_at": datetime.now(timezone.utc).isoformat(), "profiles": [],
                "total": len(councilors)}
    for c in councilors:
        profiles["profiles"].append({"name": c["name"], "slug": slugify(c["name"]),
                                     "club": "", "role": "", "photo_url": "", "bio": "",
                                     "email": "", "social_links": {},
                                     "kadencje": {"2024-2029": {
                                         "club": "", "frekwencja": c["frekwencja"],
                                         "aktywnosc": 0, "zgodnosc_z_klubem": None,
                                         "votes_za": c["votes_za"],
                                         "votes_przeciw": c["votes_przeciw"],
                                         "votes_wstrzymal": c["votes_wstrzymal"],
                                         "votes_brak": c["votes_brak"],
                                         "votes_nieobecny": c["votes_nieobecny"],
                                         "votes_total": c["votes_total"],
                                         "rebellion_count": 0, "rebellions": [],
                                         "has_voting_data": True,
                                         "has_activity_data": False, "former": False,
                                         "mid_term": False}}})
    (docs / "profiles.json").write_text(
        json.dumps(profiles, ensure_ascii=False, indent=1), encoding="utf-8")
    (docs / "data.json").write_text(json.dumps({
        "generated": datetime.now(timezone.utc).isoformat(),
        "default_kadencja": "2024-2029",
        "kadencje": [{"id": "2024-2029", "label": "IX kadencja (2024–2029)"}],
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[oborniki] DONE: {len(sessions)} sesji, {len(votes_out)} głosów, {len(all_names)} radnych")
    if not votes_out:
        sys.exit(2)


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent)
