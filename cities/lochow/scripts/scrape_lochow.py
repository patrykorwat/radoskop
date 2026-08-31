#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop Łochów — scraper głosowań imiennych (system DSSS Vote, BIP gminalochow.pl).

Źródło: BIP UM Łochów (platforma 'sesja-gpl' / gminalochow.pl) —
kategoria "Protokoły z sesji" (artykuly/294). Każdy protokół ma załącznik
PDF generowany z systemu DSSS Vote App: strona obecności (lista radnych +
godziny logowania) + na każdej kolejnej stronie jedno głosowanie imienne
("jestem za / jestem przeciw / wstrzymuję się", listy nazwisk w kolumnach).
Parser kolumnowy po współrzędnych x (lewa=kolumna ZA + Wstrzymuję się
pod tabelą, prawa=kolumna PRZECIW). Walidacja: liczby nazwisk == agregaty
z nagłówka głosowania; inaczej głos odrzucany (NIE fabrykujemy).
"""
import json
import re
import ssl
import sys
import unicodedata
import urllib.request
from datetime import datetime
from pathlib import Path

import pymupdf

BASE = "https://bip.gminalochow.pl"
CAT = "/artykuly/294/protokoly-z-sesji"
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


def slugify(name: str) -> str:
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace("ł", "l")
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or "radny"


def article_list() -> list[dict]:
    """All protocol articles: {url, title, date, num_roman}."""
    out, seen = [], set()
    page = 1
    while page <= 20:
        url = f"{BASE}/artykuly/294/{page}/10/protokoly-z-sesji" if page > 1 else BASE + CAT
        try:
            html = _http(url).decode("utf-8", "replace")
        except Exception as e:
            print(f"  [warn] list page {page}: {e}")
            break
        new = 0
        for href, title in re.findall(r'href="(?:https://bip\.gminalochow\.pl)?(/artykul/294/\d+/[^"]+)"[^>]*>(.*?)</a>', html, re.S):
            if "/artykuly/" in href:
                continue
            title = " ".join(re.sub(r"<[^>]+>", " ", title).split())
            if href in seen or "Protoko" not in title:
                continue
            seen.add(href)
            m = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", title)
            rm = re.search(r"Sesji[ ]+(%s)/(\d{4})" % ROMAN_RE, title)
            if not m:
                continue
            date = f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
            out.append({"url": BASE + href, "title": title, "date": date,
                        "roman": rm.group(1) if rm else "", "year": rm.group(2) if rm else m.group(3)})
            new += 1
        if new == 0:
            break
        page += 1
    return sorted(out, key=lambda a: a["date"], reverse=True)


def attachments(article_html: str) -> list[str]:
    out = []
    for u in re.findall(r'href="([^"]*attachments/download/\d+)"', article_html):
        if u not in out:
            out.append(u)
    return out


def _rows(page):
    rows = {}
    for w in page.get_text("words"):
        rows.setdefault(round(w[1]), []).append((round(w[0]), w[4]))
    return {y: sorted(v) for y, v in rows.items()}


def _name(line_words):
    toks = [w for _, w in line_words if not re.fullmatch(r"\d+\.", w) and w != "BRAK"]
    return " ".join(toks).strip()


def parse_dsss(pdf_bytes: bytes):
    """-> (session_info, roster, [votes]) for one DSSS Vote protocol PDF."""
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
            rf'(Uchwała numer (\S+) [{LQ}](.*?)[{RQ}]|Wniosek [^"]*?[{LQ}](.*?)[{RQ}]|'
            rf'Przeprowadzono głosowanie w ?sprawy [{LQ}](.*?)[{RQ}])'
            r'.*?proporcją głosów: jestem za (\d+), jestem przeciw (\d+), wstrzymuję się (\d+)', t)
        if not m:
            continue
        topic = m.group(3) or m.group(4) or m.group(5) or ""
        num = m.group(2) or ""
        za_n, pp_n, ws_n = int(m.group(6)), int(m.group(7)), int(m.group(8))
        head = t[:t.find("proporcją")] if "proporcją" in t else ""
        if re.search(r"nie został(a|y)? (podjęta|przyjęty|przeglądowa)", head):
            status = "odrzucone"
        else:
            status = "przyjete"
        rows = _rows(p)
        ys = sorted(rows)
        y_za = y_wsz = y_abs = y_foot = None
        for y in ys:
            txt = " ".join(w for _, w in rows[y])
            if "Jestem za" in txt and "Jestem przeciw" in txt and y_za is None:
                y_za = y
            if any(x < 300 and "Wstrzymuję" in w for x, w in rows[y]) and y_za and y > y_za and y_wsz is None:
                y_wsz = y
            if any(x >= X_SPLIT and ("Obecni" in w or "udziału" in w) for x, w in rows[y]) and y_za and y > y_za and y_abs is None:
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
                if y_za < y < min(y_wsz_lim, y_foot_lim) and n1 and "Jestem" not in n1:
                    za_l.append(n1)
                if y_za < y < min(y_abs_lim, y_foot_lim) and n2 and "Obecni" not in n2 and "udziału" not in n2:
                    pp_l.append(n2)
                if y_wsz and y_wsz_lim < y < y_foot_lim and n1 and "Wstrzymuję" not in n1:
                    ws_l.append(n1)
                if y_abs and y_abs_lim < y < y_foot_lim and n2:
                    abs_l.append(n2)
        ok = (len(za_l) == za_n and len(pp_l) == pp_n and len(ws_l) == ws_n)
        if not ok:
            print(f"    [skip-unverified] {num or topic[:40]} counts=({za_n},{pp_n},{ws_n}) parsed=({len(za_l)},{len(pp_l)},{len(ws_l)})")
            continue
        votes.append({"topic": topic, "num": num, "status": status,
                      "za": za_l, "przeciw": pp_l, "wstrz": ws_l, "abs": abs_l,
                      "counts": {"za": za_n, "przeciw": pp_n, "wstrzymal_sie": ws_n}})
    return session, roster, votes


def main(city_dir: Path):
    docs = city_dir / "docs"
    docs.mkdir(exist_ok=True)
    arts = article_list()
    arts = [a for a in arts if a["date"] >= IX_START]
    print(f"[lochow] {len(arts)} protokołów IX kadencji")
    all_names, sessions, votes_out = [], [], []
    for a in arts:
        try:
            html = _http(a["url"]).decode("utf-8", "replace")
        except Exception as e:
            print(f"  [warn] {a['url']}: {e}")
            continue
        atts = attachments(html)
        if not atts:
            print(f"  [warn] no attachments: {a['title'][:60]}")
            continue
        merged_votes, sess_present = [], 0
        for att in atts:
            att_url = att if att.startswith("http") else BASE + att
            try:
                pdf = _http(att_url)
            except Exception as e:
                print(f"  [warn] att {att}: {e}")
                continue
            if pdf[:4] != b"%PDF":
                continue
            sess, roster, pvotes = parse_dsss(pdf)
            for nm in roster:
                if nm not in all_names:
                    all_names.append(nm)
            if sess.get("present"):
                sess_present = sess["present"]
            merged_votes.extend(pvotes)
        if not merged_votes:
            print(f"  [warn] no votes parsed: {a['title'][:60]}")
            continue
        sid = a["date"]
        sessions.append({"date": sid, "number": sid,
                         "label": f"Sesja {a['roman']}/{a['year']} ({sid})",
                         "vote_count": len(merged_votes),
                         "attendee_count": sess_present or None,
                         "attendees": [], "speakers": []})
        for i, v in enumerate(merged_votes):
            idx = {nm: n for n, nm in enumerate(all_names)}
            votes_out.append({
                "id": f"{a['date']}_{i:03d}",
                "source_url": a["url"],
                "session_date": a["date"],
                "session_number": a["roman"],
                "topic": v["topic"],
                "druk": v["num"] or "None",
                "resolution": v["status"],
                "counts": {**v["counts"], "brak_glosu": len(v["abs"]), "nieobecni": 0},
                "named_votes": {
                    "za": [idx[n] for n in v["za"] if n in idx],
                    "przeciw": [idx[n] for n in v["przeciw"] if n in idx],
                    "wstrzymal_sie": [idx[n] for n in v["wstrz"] if n in idx],
                    "brak_glosu": [idx[n] for n in v["abs"] if n in idx],
                    "nieobecni": [],
                },
            })
        print(f"  [ok] {a['title'][:70]} -> {len(merged_votes)} votes")
    # councilor stats
    councilors = []
    for nm in all_names:
        i = len(all_names) and all_names.index(nm)
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
    profiles = {"scraped_at": datetime.utcnow().isoformat(), "profiles": [],
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
        "generated": datetime.utcnow().isoformat(),
        "default_kadencja": "2024-2029",
        "kadencje": [{"id": "2024-2029", "label": "IX kadencja (2024–2029)"}],
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[lochow] DONE: {len(sessions)} sessions, {len(votes_out)} votes, {len(all_names)} radnych")
    if not votes_out:
        sys.exit(2)


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent)
