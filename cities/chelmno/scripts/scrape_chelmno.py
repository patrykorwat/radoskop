#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop Chełmno — scraper głosowań imiennych (BIP bip.chelmno.pl, CMS Logonet).

Źródło: BIP Urzędu Miasta Chełmna, kategoria 150 "Protokoły, imienne głosowania
z sesji Rady miasta" (paginacja /artykuly/150/{page}/10/...). Każdy artykuł-sesja
ma załączniki typu "załącznik nr K" (/attachments/download/ID); te, które są
wydrukami głosowania, mają nagłówek "N NN Sesja IX kadencji Rady Miasta Chełmna /
Głosowanie / <temat> / Typ głosowania jawne ... / Liczba uprawnionych n ...".
Tabela imienna w 2 kolumnach: "Lp Nazwisko i imię Głos" x2 — per wiersz pozycja
x głosu (ZA / PRZECIW / WSTRZYMUJĘ SIĘ / NIEOBECNY/A). Walidacja: policzone
głosy == "Głosy za/przeciw/wstrzymujące się" z nagłówka. Roster = unia
"Uprawnieni do głosowania" z PDF-ów.
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

BASE = "https://bip.chelmno.pl"
CAT = "artykuly/150/protokoly-imienne-glosowania-z-sesji-rady-miasta"
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


def parse_date(title: str) -> str:
    t = title
    m = re.search(r'(\d{1,2})[.-](\d{1,2})[.-](\d{4})', t)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}-{d:02d}"
    m = re.search(r'(\d{1,2})\s+(' + "|".join(MONTHS) + r')\s+(\d{4})', t, re.I)
    if m:
        return f"{m.group(3)}-{MONTHS[m.group(2).lower()]:02d}-{int(m.group(1)):02d}"
    return ""


def roman_of(title: str) -> str:
    m = re.search(rf'\b({ROMAN_RE})\b', title, re.I)
    return m.group(1).upper() if m and len(m.group(1)) >= 1 else ""


def list_sessions() -> list[tuple[str, str, str]]:
    """(title, article_id, url) all protocol articles, newest-first."""
    out, seen = [], set()
    for pg in range(1, 12):
        u = f"{BASE}/artykuly/150/" + (f"{pg}/10/" if pg > 1 else "") + \
            "protokoly-imienne-glosowania-z-sesji-rady-miasta"
        try:
            h = _html(u)
        except Exception as e:
            print(f"  [warn] strona {pg}: {e}")
            break
        new = 0
        for m in re.finditer(r'href="https://bip\.chelmno\.pl/artykul/150/(\d+)/[^"]*"[^>]*>([^<]{4,140})<', h):
            aid, title = m.group(1), " ".join(m.group(2).split())
            if aid in seen:
                continue
            seen.add(aid)
            new += 1
            full = h[m.start():m.start()+300]
            mu = re.search(r'href="(https://bip\.chelmno\.pl/artykul/150/\d+/[^"]+)"', full)
            out.append((title, aid, mu.group(1) if mu else ""))
        print(f"  strona {pg}: +{new} (razem {len(out)})")
        if new == 0:
            break
        time.sleep(0.4)
    return out


VOTE_MAP = {"ZA": "za", "PRZECIW": "przeciw", "WSTRZYMUJĘ": "wstrzymal_sie",
            "WSTRZYMUJE": "wstrzymal_sie", "NIEOBECNY": "nieobecni",
            "NIEOBECNA": "nieobecni", "NIE": "brak_glosu"}


def _norm_tok(tok: str) -> str:
    t = tok.upper().rstrip(".:")
    if t.startswith("WSTRZYMUJ"):
        return "wstrzymal_sie"
    if t.startswith("NIEOBECN"):
        return "nieobecni"
    if t == "PRZECIW":
        return "przeciw"
    if t == "ZA":
        return "za"
    if t == "NIE":
        return "brak_glosu"
    return None


def _num_after(lines, label_re):
    """Label line followed by a bare number line -> int (pymupdf layout)."""
    for i, ln in enumerate(lines):
        if re.fullmatch(label_re, ln.strip(), re.I):
            for ln2 in lines[i + 1:i + 3]:
                m = re.fullmatch(r'(\d+)', ln2.strip())
                if m:
                    return int(m.group(1))
    return None


def parse_vote_pdf(pdf: bytes):
    """One attachment PDF -> list of vote dicts, or [] if not a vote print.

    pymupdf text layout: label line, then value line. Roll-call rows are two
    councilors per text line: 'K. Nazwisko imię  ZA   KK. Nazwisko imię  PRZECIW'.
    """
    doc = pymupdf.open(stream=pdf, filetype="pdf")
    votes = []
    for page in doc:
        text = page.get_text()
        if "kadencji" not in text or "Głosowanie" not in text:
            continue
        lines = text.split("\n")
        n_up = _num_after(lines, r'Liczba uprawnionych')
        n_za = _num_after(lines, r'G[łl]osy za')
        n_pr = _num_after(lines, r'G[łl]osy przeciw')
        n_ws = _num_after(lines, r'G[łl]osy wstrzymuj\w*(\s+si[ęe])?')
        if None in (n_up, n_za, n_pr, n_ws):
            continue
        # topic: line like '2. Przyjęcie porządku...' near the top
        topic = ""
        for ln in lines[:4]:
            if re.match(r'\d+\.\s+\S', ln.strip()) and "Sesja" not in ln:
                topic = ln.strip()
                break
        if not topic:
            topic = " ".join(lines[:2])[:120]
        # rows: tokens on each text line
        words = page.get_text("words")
        rows: dict[int, list] = {}
        for w in words:
            rows.setdefault(round((w[1] + w[3]) / 2 / 3) * 3, []).append(w)
        tally = {"za": 0, "przeciw": 0, "wstrzymal_sie": 0, "brak_glosu": 0, "nieobecni": 0}
        per: dict[str, str] = {}
        started = False
        for y in sorted(rows):
            ws = sorted(rows[y], key=lambda w: w[0])
            toks = [w[4] for w in ws]
            joined = " ".join(toks)
            if "Uprawnieni do głosowania" in joined:
                started = True
                continue
            if not started or "Wydrukowano" in joined:
                continue
            keys = [_norm_tok(t) for t in toks]
            vidx = [i for i, k in enumerate(keys) if k]
            if not vidx or len(vidx) * 2 != len(vidx):  # placeholder
                pass
            if len(vidx) not in (1, 2):
                continue
            # split tokens into 2 segments ending at each vote token
            bounds = [0] + [i + 1 for i in vidx]
            for si, vi in enumerate(vidx):
                seg = toks[bounds[si]:vi]
                # drop lp numbers (1-99 optional dot) and stray vote-header words
                seg = [t for t in seg if not re.fullmatch(r'\d{1,2}\.?', t)
                       and _norm_tok(t) is None and t not in ("SIĘ", "SIE")]
                if len(seg) < 2:
                    continue
                surname, firstname = seg[0], seg[1]
                if not re.match(r'[A-ZŁŚŻŹĆĄŃÓŚ]', surname):
                    continue
                name = f"{firstname} {surname}"
                name = re.sub(r'\s+', ' ', name).strip()
                if re.search(r'Nazwisko|Uprawnieni|G[łl]os', name):
                    continue
                key = keys[vi]
                if name in per:
                    continue
                per[name] = key
                tally[key] += 1
        if not per:
            continue
        ok = (tally["za"] == n_za and tally["przeciw"] == n_pr
              and tally["wstrzymal_sie"] == n_ws)
        votes.append({"topic": topic[:300],
                      "counts": {"uprawnieni": n_up, "za": n_za,
                                 "przeciw": n_pr, "wstrzymal_sie": n_ws},
                      "per": per, "ok": ok, "tally": tally})
    doc.close()
    return votes


def main(city_dir: Path):
    docs = city_dir / "docs"
    docs.mkdir(exist_ok=True)
    sessions_meta = list_sessions()
    print(f"[chelmno] {len(sessions_meta)} artykułów-protokołów")
    all_names, sessions, votes_out = [], [], []
    for title, aid, url in sessions_meta:
        date = parse_date(title)
        if not date or date < IX_START:
            continue
        roman = roman_of(title)
        try:
            h = _html(f"{BASE}/artykul/150/{aid}/x")
        except Exception:
            try:
                h = _html(url)
            except Exception as e:
                print(f"  [warn] art {aid}: {e}")
                continue
        atts = []
        for m in re.finditer(r'href="(https://bip\.chelmno\.pl/attachments/download/(\d+))"', h):
            if m.group(1) not in [a[0] for a in atts]:
                atts.append((m.group(1), m.group(2)))
        svotes = []
        for aurl, aid2 in atts:
            try:
                pdf = _http(aurl)
            except Exception as e:
                print(f"  [warn] att {aid2}: {e}")
                continue
            if pdf[:4] != b"%PDF":
                continue
            try:
                vs = parse_vote_pdf(pdf)
            except Exception as e:
                print(f"  [warn] parse att {aid2}: {e}")
                continue
            for v in vs:
                v["src"] = aurl
                svotes.append(v)
            time.sleep(0.25)
        good = [v for v in svotes if v["ok"]]
        if len(good) < len(svotes):
            print(f"  [warn] {title[:50]}: odrzucono {len(svotes)-len(good)} niespójnych")
        if not good:
            print(f"  [skip] {title[:60]} -> 0 głosów imiennych")
            continue
        for v in good:
            for nm in v["per"]:
                if nm not in all_names:
                    all_names.append(nm)
        sessions.append({"date": date, "number": roman or date,
                         "label": f"Sesja {roman} ({date})" if roman else f"Sesja ({date})",
                         "vote_count": len(good),
                         "attendee_count": None, "attendees": [], "speakers": []})
        idx = {nm: n for n, nm in enumerate(all_names)}
        base_i = len(votes_out)
        for i, v in enumerate(good):
            nv = {"za": [], "przeciw": [], "wstrzymal_sie": [], "brak_glosu": [], "nieobecni": []}
            for nm, key in v["per"].items():
                nv[key].append(idx[nm])
            votes_out.append({
                "id": f"{date}_{i:03d}",
                "source_url": v.get("src", url or f"{BASE}/artykul/150/{aid}"),
                "session_date": date, "session_number": roman,
                "topic": v["topic"], "druk": "None",
                "resolution": "przyjete" if v["counts"]["za"] > v["counts"]["przeciw"] else "odrzucone",
                "counts": v["counts"],
                "named_votes": nv,
            })
        print(f"  [ok] {title[:66]} -> {len(good)} głosów")
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
    print(f"[chelmno] DONE: {len(sessions)} sesji, {len(votes_out)} głosów, {len(all_names)} radnych")
    if not votes_out:
        sys.exit(2)


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent)
