#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop Kolno — imienne głosowania Rady Miasta Kolno (BIP bip-umkolno.podlaskie.eu).

Źródło: kategoria BIP "Imienny wykaz głosowań radnych" (kadencja 2024-2029, podkategorie
roczne 2024-r/2025-r/rok-2026). Per sesja: artykuł HTML z załącznikami PDF ("Załącznik nr N")
generowanymi z systemu DSSS Vote — jeden PDF = jedno głosowanie imienne:
  - nagłówek: temat w cudzysłowie + agregaty "jestem za N, jestem przeciw N, wstrzymuję się N"
  - data: "Data i godzina głosowania: YYYY-MM-DD HH:MM:SS"
  - tabela dwukolumnowa (współrzędne x): lewa kolumna = "Jestem za", prawa = "Jestem przeciw";
    dolna sekcja: lewa = "Wstrzymuję się", prawa = "Obecni radni, którzy nie wzięli udziału".
    BRAK = kolumna pusta. Atrybucja per nazwisko przez klasteryzację x + sekwencję "N.".
Walidacja: liczby nazwisk == agregaty z nagłówka (inaczej głos odrzucany z ostrzeżeniem).

Roster: "Skład osobowy Rady" .doc (OLE Word 97, piece table) — 15 radnych IX kadencji.
Kluby: Rada Miasta Kolno nie publikuje przynależności klubowej -> club_assignments pusty.

Użycie:
    python scrape_kolno.py --city-dir cities/kolno [--cache-dir .cache]
"""

import argparse
import hashlib
import io
import json
import re
import ssl
import time
import urllib.request
from collections import defaultdict
from datetime import datetime
from itertools import combinations
from pathlib import Path

import pdfplumber

BIP = "https://bip-umkolno.podlaskie.eu"
IMIENNY = f"{BIP}/efc2c7f0af27e48/imienny_wykaz_gosowa_radnych/kadencja-2024-2029"
YEARS = ["2024-r", "2025-r", "rok-2026"]
ROSTER_DOC = f"{BIP}/resource/120021/sklad_osobowy_rady_miasta_kolno_kadencja_2024-2029.doc"
KAD_START = "2024-05-07"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/127.0"}
_ctx = ssl.create_default_context()
_ctx.check_hostname = False
_ctx.verify_mode = ssl.CERT_NONE

REQ_DELAY = 0.4
_LAST = 0.0


def _rate():
    global _LAST
    d = time.time() - _LAST
    if d < REQ_DELAY:
        time.sleep(REQ_DELAY - d)
    _LAST = time.time()


def fetch(url: str, cache_dir: Path | None = None, binary: bool = False):
    key = hashlib.md5(url.encode()).hexdigest()
    ext = ".bin" if binary else ".html"
    if cache_dir is not None:
        cf = cache_dir / (key + ext)
        if cf.is_file():
            return cf.read_bytes() if binary else cf.read_text(encoding="utf-8")
    _rate()
    req = urllib.request.Request(url, headers=UA)
    raw = urllib.request.urlopen(req, timeout=60, context=_ctx).read()
    if cache_dir is not None:
        cf = cache_dir / (key + ext)
        cf.parent.mkdir(parents=True, exist_ok=True)
        cf.write_bytes(raw)
    return raw if binary else raw.decode("utf-8", errors="replace")


# --------------------------------------------------------------------------
# Roster z .doc (OLE2 Word 97, piece table)
# --------------------------------------------------------------------------
def fetch_roster(cache_dir) -> list[str]:
    import struct
    try:
        import olefile
    except ImportError:
        return []
    raw = fetch(ROSTER_DOC, cache_dir, binary=True)
    p = Path(cache_dir or ".") / "kolno_roster.doc"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(raw)
    o = olefile.OleFileIO(p)
    wd = o.openstream("WordDocument").read()
    tbl = o.openstream("1Table").read()
    fcClx, = struct.unpack_from("<I", wd, 0x01A2)
    lcbClx, = struct.unpack_from("<I", wd, 0x01A6)
    clx = tbl[fcClx:fcClx + lcbClx]
    pieces = []
    i = 0
    while i < len(clx):
        t = clx[i]
        if t == 2:
            lcb, = struct.unpack_from("<I", clx, i + 1)
            plc = clx[i + 5:i + 5 + lcb]
            n = (len(plc) - 4) // 12
            cps = [struct.unpack_from("<I", plc, j * 4)[0] for j in range(n + 1)]
            for k in range(n):
                pcd = plc[(n + 1) * 4 + k * 8:(n + 1) * 4 + k * 8 + 8]
                fc, = struct.unpack_from("<I", pcd, 2)
                comp = (fc >> 30) & 3
                fc = fc & 0x3FFFFFFF
                cp = cps[k + 1] - cps[k]
                if comp:
                    pieces.append(wd[fc:fc + cp * 2].decode("utf-16-le", errors="replace"))
                else:
                    pieces.append(wd[fc:fc + cp].decode("cp1250", errors="replace"))
            break
        elif t == 1:
            cb, = struct.unpack_from("<H", clx, i + 1)
            i += 3 + cb
        else:
            break
    text = "".join(pieces)
    lines = re.split(r"[\r\x07\x0b]", text)
    roster = []
    for l in lines:
        l = l.replace("\x00", "")
        l = "".join(ch for ch in l if ord(ch) >= 32 and not (0x80 <= ord(ch) <= 0xFF))
        l = re.sub(r"\s+", " ", l).strip()
        m = re.match(r"^([A-ZŁŚŃÓŹŻĆ][\wŁŚŃÓŹŻĆąęłśńóźżć-]+(?: [A-ZŁŚŃÓŹŻĆąęłśńóźżć-]+){1,3})"
                     r"(?:\s+[–-]\s+.*)?$", l)
        if m and not re.search(r"Rady|Miasta|kadencj|Skład", m.group(1)):
            roster.append(m.group(1))
    # NOTE: ten .doc ma uszkodzoną tablicę kawałków (diakrytyki tracone) — roster
    # i tak budowany jest z PDF-ów głosowań (czyste nazwiska); .doc = pomocniczy.
    return [n for n in roster if len(n) > 4]


# --------------------------------------------------------------------------
# Artykuły sesji + załączniki
# --------------------------------------------------------------------------
def discover_session_articles(cache_dir):
    out = []
    for yr in YEARS:
        h = fetch(f"{IMIENNY}/{yr}/", cache_dir)
        arts = sorted(set(re.findall(
            rf'href="(/efc2c7f0af27e48/imienny_wykaz_gosowa_radnych/kadencja-2024-2029/{yr}/[^"]+?\.html)"', h)))
        for a in arts:
            slug = a.rstrip("/").split("/")[-1]
            if slug.startswith(("rok-", "20")):
                continue  # byt kategorii
            m = re.match(r"([ivxlcdm]+)-sesja---(\d{1,2})-([a-ząęłśńóźż]+)-(\d{4})-r", slug)
            if not m:
                continue
            num, day, mon, year = m.group(1).upper(), m.group(2), m.group(3), m.group(4)
            out.append({"href": a, "numeral": num, "date": f"{year}-{_month(mon)}-{int(day):02d}"})
    out.sort(key=lambda s: s["date"])
    return out


MONTHS = {"stycznia": "01", "lutego": "02", "marca": "03", "kwietnia": "04",
          "maja": "05", "czerwca": "06", "lipca": "07", "sierpnia": "08",
          "wrzesnia": "09", "września": "09", "pazdziernika": "10", "października": "10",
          "listopada": "11", "grudnia": "12"}


def _month(mon):
    return MONTHS.get(mon, MONTHS.get(mon.replace("ż", "z"), "01"))


def article_attachments(href, cache_dir):
    h = fetch(BIP + href, cache_dir)
    seen = {}
    for href_pdf in re.findall(r'href="(/resource/\d+/[^"]+\.pdf)"', h):
        un = href_pdf
        m = re.search(r"nr\+(\d+)", un) or re.search(r"nr %25C3%252B(\d+)", un)
        num = int(m.group(1)) if m else 999
        seen.setdefault((num, un), un)
    return [u for (_n, u) in sorted(seen.keys())]


# --------------------------------------------------------------------------
# Parser PDF DSSS Vote (układ 2-kolumnowy, analiza x)
# --------------------------------------------------------------------------
FOOTER_TOKENS = {"Operatorem", "systemu", "był", "Admin.", "Wygenerowano", "z",
                 "pośrednictwem", "oprogramowania", "DSSS", "Vote", "App.", "w", "App"}


def parse_vote_pdf(raw: bytes):
    with pdfplumber.open(io.BytesIO(raw)) as pdf:
        page = pdf.pages[0]
        text = "\n".join((p.extract_text() or "") for p in pdf.pages)
        words = page.extract_words()

    head = re.search(r"proporcj\w*\s+głos[óo]w:\s*jestem za (\d+),?\s*jestem przeciw (\d+),?\s*wstrzymuj\w+ się (\d+)", text)
    if not head:
        head = re.search(r"za (\d+), jestem przeciw (\d+), wstrzymuję się (\d+)", text)
    if not head:
        return None
    agg = {"za": int(head.group(1)), "przeciw": int(head.group(2)), "wstrzymal_sie": int(head.group(3))}

    dm = re.search(r"Data i godzina głosowania:\s*(\d{4}-\d{2}-\d{2})", text)
    vdate = dm.group(1) if dm else None

    tm = re.search(r"[“\"](.+?)[”\"]", text)
    topic = tm.group(1).strip() if tm else ""
    um = re.search(r"Uchwała numer\s+(\S+?)\s", text)
    if um:
        topic = f"Uchwała {um.group(1)}: {topic}"

    # punkt odcięcia: nagłówek "Wstrzymuję" / "Obecni" (dolna sekcja)
    split_top = None
    for w in words:
        if w["text"] == "Wstrzymuję":
            split_top = w["top"]
            break
    if split_top is None:
        return None

    RIGHT_X = 320.0
    # zakotwiczenie: nagłówek "Jestem za|przeciw" (górna tabela) i stopka "Operatorem"
    jestem_top = None
    footer_top = None
    for w in words:
        if w["text"] == "Jestem" and (jestem_top is None or w["top"] < jestem_top):
            jestem_top = w["top"]
        if w["text"] == "Operatorem":
            footer_top = w["top"]
    if jestem_top is None or footer_top is None:
        return None

    cols = {"za": [], "przeciw": [], "wstrzymal_sie": [], "bez_glosu": []}
    for area, left_key, right_key in (("top", "za", "przeciw"), ("bottom", "wstrzymal_sie", "bez_glosu")):
        pts = []
        for w in words:
            if area == "top" and not (jestem_top + 4 < w["top"] < split_top - 4):
                continue
            if area == "bottom" and not (split_top - 4 < w["top"] < footer_top - 4):
                continue
            if w["text"].startswith(("Jestem", "Wstrzymuję", "Obecni", "radni", "udziału", "głosowaniu")):
                continue
            pts.append(w)
        for key, side in ((left_key, True), (right_key, False)):
            cur = None
            for w in sorted(pts, key=lambda w: (round(w["top"], 0), w["x0"])):
                on_right = w["x0"] >= RIGHT_X
                if on_right != (not side):
                    continue
                t = w["text"]
                if t == "BRAK":
                    continue
                if re.fullmatch(r"\d+\.", t):
                    cur = []
                    cols[key].append(cur)
                    continue
                if cur is None:
                    continue
                if re.fullmatch(r"[A-ZŁŚŃÓŹŻĆ][\wŁŚŃÓŹŻĆąęłśńóźżć'’-]+\.?", t) and not re.fullmatch(r"\d+", t):
                    cur.append(t)
            cols[key] = [" ".join(c) for c in cols[key] if c and len(" ".join(c)) > 3]

    named = {"za": cols["za"], "przeciw": cols["przeciw"], "wstrzymal_sie": cols["wstrzymal_sie"]}
    if any(len(named[k]) != agg[k] for k in agg):
        return {"agg": agg, "named": named, "date": vdate, "topic": topic,
                "bez_glosu": cols["bez_glosu"], "valid": False}
    return {"agg": agg, "named": named, "date": vdate, "topic": topic,
            "bez_glosu": cols["bez_glosu"], "valid": True}


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------
def make_slug(name):
    repl = {'ą': 'a', 'ć': 'c', 'ę': 'e', 'ł': 'l', 'ń': 'n', 'ó': 'o',
            'ś': 's', 'ź': 'z', 'ż': 'z'}
    slug = name.lower()
    for pl, a in repl.items():
        slug = slug.replace(pl, a)
    return re.sub(r"[^a-z0-9]+", "", slug)


def build(records, roster):
    all_votes = []
    sess = {}
    vid = 0
    for rec in records:
        d = rec["date"]
        if not d or d < KAD_START:
            continue
        if d not in sess:
            sess[d] = {"date": d, "number": rec["numeral"], "vote_count": 0,
                       "attendees": set(), "speakers": []}
        vid += 1
        sess[d]["vote_count"] += 1
        for cat in ("za", "przeciw", "wstrzymal_sie"):
            sess[d]["attendees"].update(rec["named"].get(cat, []))
        sess[d]["attendees"].update(rec.get("bez_glosu", []))
        all_votes.append({
            "id": str(vid), "session_date": d, "session_number": rec["numeral"],
            "topic": rec["topic"], "named_votes": {k: list(v) for k, v in rec["named"].items()},
            "counts": {k: len(rec["named"].get(k, [])) for k in ("za", "przeciw", "wstrzymal_sie")},
        })

    sessions_data = []
    for d in sorted(sess):
        s = sess[d]
        sessions_data.append({"date": d, "number": s["number"], "vote_count": s["vote_count"],
                              "attendee_count": len(s["attendees"]),
                              "attendees": sorted(s["attendees"]), "speakers": []})

    names = set(roster)
    for v in all_votes:
        for arr in v["named_votes"].values():
            names.update(arr)

    cstats = {n: {"votes_za": 0, "votes_przeciw": 0, "votes_wstrzymal": 0} for n in names}
    csess = defaultdict(set)
    for v in all_votes:
        for cat, key in (("za", "votes_za"), ("przeciw", "votes_przeciw"), ("wstrzymal_sie", "votes_wstrzymal")):
            for nm in v["named_votes"].get(cat, []):
                cstats[nm][key] += 1
                csess[nm].add(v["session_date"])
        for arr in v["named_votes"].values():
            for nm in arr:
                csess[nm].add(v["session_date"])

    tv, ts = len(all_votes), len(sessions_data)
    councilors = []
    for n in sorted(names):
        st = cstats[n]
        present = st["votes_za"] + st["votes_przeciw"] + st["votes_wstrzymal"]
        councilors.append({
            "name": n, "club": "", "district": None,
            "frekwencja": round(len(csess[n]) / ts * 100, 1) if ts else 0,
            "aktywnosc": round(present / tv * 100, 1) if tv else 0,
            "zgodnosc_z_klubem": 0.0,
            "votes_za": st["votes_za"], "votes_przeciw": st["votes_przeciw"],
            "votes_wstrzymal": st["votes_wstrzymal"], "votes_brak": 0, "votes_nieobecny": 0,
            "votes_total": tv, "rebellion_count": 0, "rebellions": [],
            "has_activity_data": False, "activity": None})

    vectors = defaultdict(dict)
    for v in all_votes:
        for cat in ("za", "przeciw", "wstrzymal_sie"):
            for nm in v["named_votes"].get(cat, []):
                vectors[nm][v["id"]] = cat
    pairs = []
    ns = sorted(vectors)
    for a, b in combinations(ns, 2):
        common = set(vectors[a]) & set(vectors[b])
        if len(common) < 10:
            continue
        same = sum(1 for x in common if vectors[a][x] == vectors[b][x])
        pairs.append({"a": a, "b": b, "club_a": "", "club_b": "",
                      "score": round(same / len(common) * 100, 1), "common_votes": len(common)})
    pairs.sort(key=lambda x: x["score"], reverse=True)

    kad = {"id": KADENCJA_ID, "label": KADENCJA_LABEL, "clubs": {},
           "sessions": sessions_data, "total_sessions": ts, "total_votes": tv,
           "total_councilors": len(councilors), "councilors": councilors, "votes": all_votes,
           "similarity_top": pairs[:20], "similarity_bottom": pairs[-20:][::-1]}
    out = {"generated": datetime.now().isoformat(), "default_kadencja": KADENCJA_ID,
           "kadencje": [kad]}

    profiles = []
    for n in sorted(names):
        st = cstats[n]
        present = st["votes_za"] + st["votes_przeciw"] + st["votes_wstrzymal"]
        profiles.append({"name": n, "slug": make_slug(n), "kadencje": {KADENCJA_ID: {
            "club": "", "has_voting_data": True, "has_activity_data": False,
            "frekwencja": round(len(csess[n]) / ts * 100, 1) if ts else 0,
            "aktywnosc": round(present / tv * 100, 1) if tv else 0,
            "zgodnosc_z_klubem": 0.0,
            "votes_za": st["votes_za"], "votes_przeciw": st["votes_przeciw"],
            "votes_wstrzymal": st["votes_wstrzymal"], "votes_brak": 0, "votes_nieobecny": 0,
            "votes_total": tv, "rebellion_count": 0, "rebellions": [], "roles": [],
            "notes": "", "former": False, "mid_term": False}}})
    prof = {"profiles": profiles, "total": len(profiles)}
    return out, prof


def save_split(output, out_path, profiles):
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    stubs = []
    for kad in output.get("kadencje", []):
        kid = kad["id"]
        stubs.append({"id": kid, "label": kad.get("label", f"Kadencja {kid}")})
        with open(out_path.parent / f"kadencja-{kid}.json", "w", encoding="utf-8") as f:
            json.dump(kad, f, ensure_ascii=False, separators=(",", ":"))
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"generated": output.get("generated", ""),
                   "default_kadencja": output.get("default_kadencja", ""),
                   "kadencje": stubs}, f, ensure_ascii=False, separators=(",", ":"))
    with open(out_path.parent / "profiles.json", "w", encoding="utf-8") as f:
        json.dump(profiles, f, ensure_ascii=False, separators=(",", ":"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--city-dir", required=True)
    ap.add_argument("--cache-dir", default=None)
    args = ap.parse_args()
    city_dir = Path(args.city_dir)
    cache = Path(args.cache_dir) if args.cache_dir else None

    roster = fetch_roster(cache)
    print(f"[kolno] roster: {len(roster)}: {', '.join(roster[:5])}...")

    arts = discover_session_articles(cache)
    print(f"[kolno] sesje: {len(arts)}")
    records = []
    for a in arts:
        atts = article_attachments(a["href"], cache)
        n_ok = 0
        for href in atts:
            try:
                raw = fetch(BIP + href, cache, binary=True)
                v = parse_vote_pdf(raw)
            except Exception as e:
                print(f"  [ERR {a['date']} {href[-40:]}] {type(e).__name__}: {e}")
                continue
            if not v:
                continue
            if not v["valid"]:
                print(f"  [AGG-MISMATCH] {a['date']} {href.split('/')[-1][:50]}: agg={v['agg']} "
                      f"got={{{', '.join(f'{k}:{len(v['named'][k])}' for k in v['named'])}}}")
                continue
            if v["date"]:
                a["date"] = v["date"]
            records.append({"date": v["date"] or a["date"], "numeral": a["numeral"],
                            "topic": v["topic"], "named": v["named"],
                            "bez_glosu": v.get("bez_glosu", [])})
            n_ok += 1
        print(f"  {a['date']} {a['numeral']:<5} zal={len(atts)} imienne={n_ok}")

    out, prof = build(records, roster)
    save_split(out, city_dir / "docs" / "data.json", prof)
    k = out["kadencje"][0]
    print(f"[kolno] total votes={k['total_votes']} sessions={k['total_sessions']} "
          f"councilors={k['total_councilors']}")


if __name__ == "__main__":
    main()
