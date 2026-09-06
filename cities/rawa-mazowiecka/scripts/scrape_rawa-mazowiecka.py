#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop Rawa Mazowiecka — głosowania imienne (BIP bip.rawamazowiecka.pl).

BIP = serwer-renderowany CMS (kategorie 'plik,<id>,<slug>'):
  kategoria 2839 'Imienne wykazy głosowania radnych' — jeden HTML z linkami
  <a href="plik,N,...pdf" class="pliki_link..."> po rokach (Rok 2026 / 2025 / ...)
  Dwa tytuły: 'Wyniki głosowań z <RZYM> sesji ... w dniu D.M.RRRR r.' (2026+)
  oraz 'Imienny wykaz głosowania radnych podczas <RZYM> Sesji w dniu D.M.RRRR r.' (2024-2025).
PDF TEKSTOWY, per głosowanie:
  'Wyniki głosowania' / 'Sesja: ... nr <RZYM>' / 'Punkt obrad: ...' /
  'Nazwa głosowania: ...' / 'Data głosowania: D.M.RRRR HH:MM' /
  'Oddane głosy - podsumowanie zbiorcze: Uprawnionych: n, Za: n, Przeciw: n,
   Nieobecni: n, Wstrzymało się: n' / 'podsumowanie szczegółowe' /
  wiersze: <Lp> / <imię nazwisko> / <Za|Przeciw|Wstrzymało się|Wstrzymał(a) się|Nieobecny> / <timestamp|--->
Walidacja: liczby per kategoria == podsumowanie zbiorcze.

Użycie: python scrape_rawa-mazowiecka.py [city_dir]
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

BASE = "https://bip.rawamazowiecka.pl"
KAT_GLOSOWANIA = "2839,imienne-wykazy-glosowania-radnych"
UA = {"User-Agent": "Mozilla/5.0 Radoskop/1.0 (info@radoskop.eu)"}
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE
IX_START = "2024-05-07"
MONTHS = {"stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5,
          "czerwca": 6, "lipca": 7, "sierpnia": 8, "września": 9, "wrzesnia": 9,
          "października": 10, "pazdziernika": 10, "listopada": 11, "grudnia": 12}

VOTE_MAP = {"za": "za", "przeciw": "przeciw",
            "wstrzymało się": "wstrzymal_sie", "wstrzymalo sie": "wstrzymal_sie",
            "wstrzymał się": "wstrzymal_sie", "wstrzymal sie": "wstrzymal_sie",
            "wstrzymała się": "wstrzymal_sie", "wstrzymala sie": "wstrzymal_sie",
            "wstrzymuję się": "wstrzymal_sie", "wstrzymuje sie": "wstrzymal_sie",
            "nieobecny": "nieobecni", "nieobecna": "nieobecni",
            "brak głosu": "brak_glosu", "brak glosu": "brak_glosu"}
NAME_RE = re.compile(r"^[A-ZŁŚŻŹĆĄŃÓ][\włżźćąóńś-]*(?:\s+[A-ZŁŚŻŹĆĄŃÓ][\włżźćąóńś-]*){1,4}$")
AGG_MAP = {"za": "za", "przeciw": "przeciw", "wstrzymało": "wstrzymal_sie",
           "wstrzymalo": "wstrzymal_sie", "wstrzymało się": "wstrzymal_sie",
           "nieobecni": "nieobecni", "brak głosu": "brak_glosu"}


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


def slugify(name: str) -> str:
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace("ł", "l")
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-") or "radny"


def parse_date(text: str) -> str:
    m = re.search(r"w dniu (\d{1,2})\.(\d{1,2})\.(\d{4})", text)
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1).zfill(2)}"
    m = re.search(r"w dniu (\d{1,2}) (" + "|".join(MONTHS) + r") (\d{4})", text, re.I)
    if m:
        return f"{m.group(3)}-{MONTHS[m.group(2).lower()]:02d}-{int(m.group(1)):02d}"
    return ""


def list_pdf_links():
    import html as H
    h = _get(BASE + "/" + KAT_GLOSOWANIA).decode("utf-8", "replace")
    out = []
    for m in re.finditer(r'<a href="(plik,[^"]+\.pdf)" class="pliki_link[^"]*"><span class="pliki_nazwa">([^<]+)</span>', h):
        href = " ".join(m.group(1).split())
        title = " ".join(H.unescape(m.group(2)).split())
        out.append((href, title))
    return out


def parse_votes_pdf(pdf: bytes):
    doc = pymupdf.open(stream=pdf, filetype="pdf")
    text = "\n".join(p.get_text() for p in doc)
    doc.close()
    blocks = re.split(r"\nWyniki głosowania\n", "\n" + text)
    votes = []
    for b in blocks[1:] if len(blocks) > 1 else []:
        sm = re.search(r"Sesja:\s*(.+?)\s*\n", b)
        roman = ""
        if sm:
            rm = re.search(r"nr\s+([IVXLCDM]+)", sm.group(1))
            roman = rm.group(1) if rm else ""
        tm = re.search(r"Name glasowania|Nazwa głosowania:\s*(.+?)\s*\n(?=Typ|Data)", b, re.S)
        topic = " ".join(tm.group(1).split()) if tm else ""
        pm = re.search(r"Punkt obrad:\s*(.+?)\s*/\s*Numer punktu", b, re.S)
        punkt = " ".join(pm.group(1).split()) if pm else ""
        if not topic:
            topic = punkt
        elif punkt and punkt.lower() not in topic.lower():
            topic = punkt[:120] + " — " + topic
        dm = re.search(r"Data głosowania:\s*(\d{1,2})\.(\d{2})\.(\d{4})", b)
        if not dm:
            continue
        date = f"{dm.group(3)}-{dm.group(2)}-{dm.group(1)}"
        counts = {}
        for k, v in re.findall(r"(Uprawnionych|Zagłosowało|Za|Przeciw|Nieobecni|Wstrzymało się|Brak głosu):\s*(\d+)", b):
            kk = AGG_MAP.get(k.lower().replace(" się", ""))
            if kk:
                counts.setdefault(kk, int(v))
        # tabela szczegółowa: po 'podsumowanie szczegółowe'
        si = b.find("podsumowanie szczegółowe")
        body = b[si:] if si >= 0 else b
        lines = [l.strip() for l in body.split("\n")]
        per = {"za": [], "przeciw": [], "wstrzymal_sie": [], "nieobecni": [], "brak_glosu": []}
        i = 0
        while i < len(lines):
            if re.fullmatch(r"\d{1,3}", lines[i]) and i + 2 < len(lines) and NAME_RE.match(lines[i + 1]):
                nm, vote = lines[i + 1], " ".join(lines[i + 2].lower().split())
                key = VOTE_MAP.get(vote)
                if key:
                    per[key].append(nm)
                    i += 4
                    continue
            i += 1
        ok = all(len(per.get(k, [])) == n for k, n in counts.items())
        votes.append({"date": date, "topic": topic[:250], "roman": roman,
                      "per": per, "counts": counts, "valid": ok})
    return votes


def main(city_dir: Path):
    docs = city_dir / "docs"
    docs.mkdir(exist_ok=True)
    links = list_pdf_links()
    print(f"[rawa] {len(links)} linków PDF w kategorii imiennych wykazów")
    items = []
    for href, title in links:
        d = parse_date(title)
        if not d or d < IX_START:
            continue
        rm = re.search(r"z ([IVXLCDM]+)[ -]?(?:z )?sesji|podczas ([IVXLCDM]+) sesji", title, re.I)
        roman = (rm.group(1) or rm.group(2)).upper() if rm else ""
        items.append((d, roman, title, BASE + "/" + href))
    items = {(d, roman): (d, roman, t, u) for d, roman, t, u in items}.values()
    items = sorted(items)
    print(f"[rawa] {len(items)} sesji IX")
    all_names, sessions, votes_out = [], [], []
    for date, roman, title, url in items:
        try:
            vs = parse_votes_pdf(_get(url))
        except Exception as e:
            print(f"  [warn] {title[:60]}: {e}")
            continue
        time.sleep(0.3)
        valid = [v for v in vs if v["valid"]]
        if not valid:
            print(f"  [skip] {title[:60]} — parser 0/0")
            continue
        roman2 = roman or (valid[0]["roman"] or date)
        idxs = {nm: n for n, nm in enumerate(all_names)}
        n_ok = 0
        for v in valid:
            nv = {k: [] for k in ("za", "przeciw", "wstrzymal_sie", "brak_glosu", "nieobecni")}
            for k, names in v["per"].items():
                for nm in names:
                    if nm not in idxs:
                        idxs[nm] = len(all_names)
                        all_names.append(nm)
                    nv[k].append(idxs[nm])
            c = {"uprawnieni": v["counts"].get("za", 0) + v["counts"].get("przeciw", 0)
                 + v["counts"].get("wstrzymal_sie", 0) + v["counts"].get("nieobecni", 0)
                 + v["counts"].get("brak_glosu", 0),
                 **{k: len(nv[k]) for k in ("za", "przeciw", "wstrzymal_sie")}}
            votes_out.append({
                "id": f"{v['date']}_{len(votes_out):03d}", "source_url": url,
                "session_date": v["date"], "session_number": roman2,
                "topic": v["topic"], "druk": "None",
                "resolution": "przyjete" if c["za"] > c["przeciw"] else "odrzucone",
                "counts": c, "named_votes": nv})
            n_ok += 1
        sessions.append({"date": date, "number": roman2,
                         "label": f"Sesja {roman2} ({date})", "vote_count": n_ok,
                         "attendee_count": None, "attendees": [], "speakers": []})
        print(f"  [ok] {title[:60]} -> {n_ok} głosów ({len(vs)-len(valid)} odrzuconych)")
    sessions.sort(key=lambda s: s["date"], reverse=True)
    councilors = []
    for i, nm in enumerate(all_names):
        z = p_ = w = nb = tot = 0
        for v in votes_out:
            nv = v["named_votes"]
            if i in nv["za"]:
                z += 1; tot += 1
            elif i in nv["przeciw"]:
                p_ += 1; tot += 1
            elif i in nv["wstrzymal_sie"]:
                w += 1; tot += 1
            elif i in nv["nieobecni"]:
                nb += 1
        uprawn = tot + nb
        councilors.append({"name": nm, "slug": slugify(nm), "club": "",
                           "za": z, "przeciw": p_, "wstrzymal_sie": w,
                           "brak_glosu": 0, "nieobecny": nb, "glosowal": tot,
                           "frekwencja": round(100 * tot / uprawn, 1) if uprawn else 0,
                           "aktywnosc": round(100 * tot / uprawn, 1) if uprawn else 0,
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
    data = {"scraped_at": now, "city": "Rawa Mazowiecka", "bip": BASE,
            "kadencje": [{"id": "2024-2029", "label": "IX kadencja (2024–2029)"}],
            "stats": {"sessions": len(sessions), "votes": len(votes_out),
                      "councilors": len(all_names)}}
    (docs / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[rawa] DONE: {len(sessions)} sesji / {len(votes_out)} głosów / {len(all_names)} radnych")


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else Path("."))
