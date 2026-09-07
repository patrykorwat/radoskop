#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop Tuchola — imienne głosowania Rady Miejskiej (IX kadencja 2024-2029).

Źródło: BIP bip.tuchola.pl (React-SPA Madkom 'playout' CMS, API /api/):
  GET /api/contexts/default/articles?limit=100&offset=N   — pełny indeks artykułów
  GET /api/articles/{id}                                  — treść + attachments
Kategoria 'Imienne wykazy głosowań radnych': jeden artykuł = jedno głosowanie
("Imienny wykaz głosowań radnych - Uchwała Nr X/NN/RR ..."), załącznik PDF z systemu
DSSS Vote (TEKSTOWY): nagłówek 'jestem za N, jestem przeciw M, wstrzymało się K',
'Data i godzina głosowania: DD.MM.RRRR', 'na <R> sesji w dniu D miesiac Y r.',
tabela dwukolumnowa LEWA='Jestem za' / PRAWA (x>=340)='Jestem przeciw',
pod tabelą LEWA lista 'Wstrzymuję się', PRAWA 'Obecni radni, którzy nie wzięli
udziału w głosowaniu' (marker BRAK = kolumna pusta).
Walidacja per głos: długości list == agregaty; niezwalidowany głos odrzucany.

Użycie: python3 scrape_tuchola.py --city-dir <cities/tuchola> [--cache-dir dir]
Zapisuje: docs/data.json, docs/kadencja-2024-2029.json, docs/profiles.json
"""
import argparse
import hashlib
import io
import json
import re
import sys
import time
import unicodedata
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

try:
    import pdfplumber
except ImportError:
    sys.exit("pdfplumber required")

BIP = "https://bip.tuchola.pl"
API = BIP + "/api"
KAD_START = "2024-05-07"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"
PRZECIW_X = 340.0
UA = {"User-Agent": "Mozilla/5.0 (Radoskop/1.0; info@radoskop.eu)"}
REQ_DELAY = 0.35
_LAST = 0.0

MONTHS = {"stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5,
          "czerwca": 6, "lipca": 7, "sierpnia": 8, "września": 9, "pazdziernika": 10,
          "października": 10, "listopada": 11, "grudnia": 12}
_ROM = {r: i for i, r in enumerate(
    ["I","II","III","IV","V","VI","VII","VIII","IX","X","XI","XII","XIII","XIV","XV","XVI",
     "XVII","XVIII","XIX","XX","XXI","XXII","XXIII","XXIV","XXV","XXVI","XXVII","XXVIII",
     "XXIX","XXX","XXXI","XXXII","XXXIII","XXXIV","XXXV","XXXVI","XXXVII","XXXVIII"], 1)}


def _rate():
    global _LAST
    d = time.time() - _LAST
    if d < REQ_DELAY:
        time.sleep(REQ_DELAY - d)
    _LAST = time.time()


def _get(url, cache_dir=None, tries=5):
    key = hashlib.md5(url.encode()).hexdigest()
    cf = None
    if cache_dir:
        cd = Path(cache_dir)
        cd.mkdir(parents=True, exist_ok=True)
        cf = cd / (key + ".dat")
        if cf.is_file() and cf.stat().st_size > 0:
            return cf.read_bytes()
    last = None
    for a in range(tries):
        try:
            _rate()
            req = urllib.request.Request(url, headers=UA)
            data = urllib.request.urlopen(req, timeout=60).read()
            if cf is not None:
                cf.write_bytes(data)
            return data
        except Exception as e:
            last = e
            time.sleep(1.5 * (a + 1))
    raise RuntimeError(f"fail {url}: {last}")


def _nk(s):
    s = (s or "").lower().replace("ł", "l")
    s = unicodedata.normalize("NFKD", s)
    return re.sub(r"[^a-z0-9]", "", "".join(c for c in s if not unicodedata.combining(c)))


def roman_val(r):
    return _ROM.get(r)


def parse_pdf(data):
    """-> dict(session_num, vote_date, uchwala, topic, agg{za,przeciw,wstrzymal_sie},
              named{za,przeciw,wstrzymal_sie,nieobecni}) albo None."""
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        t = "\n".join((p.extract_text() or "") for p in pdf.pages)
    if "DSSS" not in t and "jestem za" not in t.lower():
        return None
    mdate = re.search(r"w dniu (\d{1,2})\s+([A-Za-ząęłńóśźż]+)\s+(\d{4})\s*r\.? sesji", t) or \
            re.search(r"sesji\s+w dniu (\d{1,2})\s+([A-Za-ząęłńóśźż]+)\s+(\d{4})", t)
    vote_date = None
    if mdate and _nk(mdate.group(2)) in {_nk(m) for m in MONTHS}:
        mon = [v for k, v in MONTHS.items() if _nk(k) == _nk(mdate.group(2))][0]
        vote_date = f"{mdate.group(3)}-{mon:02d}-{int(mdate.group(1)):02d}"
    if not vote_date:
        mv = re.search(r"Data i godzina g\u0142osowania:\s*(\d{1,2})\.(\d{1,2})\.(\d{4})", t)
        if mv:
            vote_date = f"{mv.group(3)}-{int(mv.group(2)):02d}-{int(mv.group(1)):02d}"
    msess = re.search(r"na\s+([IVXL]+)\s+sesji", t)
    sess_num = roman_val(msess.group(1)) if msess else None
    muc = re.search(r"Uch\u0142awa numer\s+([IVXL]+[/-]\d+[/-]\d+)", t)
    uchwala = muc.group(1) if muc else None
    magg = re.search(r"jestem za\s*(\d+),\s*\n?jestem przeciw\s*(\d+),\s*\n?wstrzyma\u0142o si\u0119\s*(\d+)", t)
    if not magg:
        return None
    agg = {"za": int(magg.group(1)), "przeciw": int(magg.group(2)),
           "wstrzymal_sie": int(magg.group(3))}
    # kolumny z word-ami
    named = {"za": [], "przeciw": [], "wstrzymal_sie": [], "nieobecni": []}
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        page = pdf.pages[0]
        words = page.extract_words()
    lines = defaultdict(list)
    for w in words:
        lines[round(w["top"])].append(w)
    tops = sorted(lines)
    # sekcja główna: od nagłówka 'Jestem za' do 'Wstrzymuję się' / 'Obecni'
    y_head = y_wsz = y_abs = None
    for top in tops:
        lw = sorted(lines[top], key=lambda w: w["x0"])
        if y_head is None and any(w["x0"] >= PRZECIW_X and w["text"].startswith("przeciw") for w in lw):
            y_head = top
        if y_wsz is None and any(w["x0"] < PRZECIW_X and w["text"].startswith("Wstrzymuj") for w in lw):
            y_wsz = top
        if y_abs is None and any(w["x0"] >= PRZECIW_X - 40 and w["text"] == "Obecni" for w in lw):
            y_abs = top
    y_tail = min(x for x in (y_wsz or 10**9, y_abs or 10**9, 10**9))
    for top in tops:
        if y_head is None or top <= y_head or top >= y_tail:
            continue
        lw = sorted(lines[top], key=lambda w: w["x0"])
        left = " ".join(w["text"] for w in lw if w["x0"] < PRZECIW_X)
        right = " ".join(w["text"] for w in lw if w["x0"] >= PRZECIW_X)
        for side, cat in ((left, "za"), (right, "przeciw")):
            nm = re.sub(r"^\d+\.\s*", "", side).strip()
            if not nm or nm == "BRAK":
                continue
            named[cat].append(nm)
    # dolne sekcje
    def footlist(x_lo, x_hi, y_from):
        out = []
        stop = ("udzialu", "glosowaniu", "wziedli", "wźli", "obecni", "radni",
                "operator", "systemu", "wygenerowano", "ktorzy")
        for top in tops:
            if top <= y_from:
                continue
            lw = sorted(lines[top], key=lambda w: w["x0"])
            seg = " ".join(w["text"] for w in lw if x_lo <= w["x0"] < x_hi)
            seg = seg.replace("BRAK", "").strip()
            for part in re.split(r",| \d+\.\s", seg):
                part = re.sub(r"^\d+\.\s*", "", part).strip(" ,")
                if not part:
                    continue
                nk = _nk(part)
                if any(s in nk for s in stop):
                    continue
                out.append(part)
            if top > y_from + 90:
                break
        return out
    if y_wsz:
        named["wstrzymal_sie"] = footlist(60, PRZECIW_X - 10, y_wsz)
    if y_abs:
        named["nieobecni"] = footlist(PRZECIW_X - 40, 560, y_abs)
    return {"vote_date": vote_date, "sess_num": sess_num, "uchwala": uchwala,
            "agg": agg, "named": named}


def validate(p):
    nm, agg = p["named"], p["agg"]
    if len(nm["za"]) != agg["za"]:
        return False
    if len(nm["przeciw"]) != agg["przeciw"]:
        return False
    if len(nm["wstrzymal_sie"]) != agg["wstrzymal_sie"]:
        return False
    return any(agg[k] for k in ("za", "przeciw", "wstrzymal_sie")) or agg["za"] == 0


def canon_name(s):
    s = re.sub(r"\s+", " ", s).strip(" .,-")
    s = re.sub(r"^\d+\.\s*", "", s)
    return s


def crawl_articles(cache_dir):
    """Pełny indeks artykułów -> {id: {t,l,d}} (缓存 plikiem na 1 przebieg)."""
    out = {}
    off = 0
    empty = 0
    while off < 14000:
        j = None
        try:
            raw = _get(f"{API}/contexts/default/articles?limit=100&offset={off}", cache_dir, tries=4)
        except RuntimeError:
            # per-offset 5xx (znany quirk Madkom SPA): przes滚 dalej, zgubione
            # offsety do nadrobienia w nastepnym przebiegu (kacha nie zapisano)
            print(f"[tuchola] indeks FAIL off={off}", flush=True)
            off += 100
            continue
        try:
            j = json.loads(raw)
        except Exception:
            j = None
        els = (j or {}).get("elements") if isinstance(j, dict) else None
        if not els:
            empty += 1
            if empty >= 3:
                break
            off += 100
            continue
        empty = 0
        for el in els:
            out[el["id"]] = {"t": el.get("title", ""), "l": el.get("link", ""),
                             "d": el.get("date", "")}
        off += 100
        if off % 2000 == 0:
            print(f"[tuchola] indeks off={off} zebrano={len(out)}", flush=True)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--city-dir", required=True)
    ap.add_argument("--cache-dir", default="")
    args = ap.parse_args()
    city = Path(args.city_dir)
    docs = city / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    cdir = args.cache_dir or None

    arts = crawl_articles(cdir)
    print(f"[tuchola] indeks: {len(arts)} artykulo")
    votes_arts = [(v["d"], k) for k, v in arts.items()
                  if "imiennywykazglosowan" in _nk(v["t"])]
    votes_arts.sort()
    print(f"[tuchola] artykuly imienne: {len(votes_arts)}")

    all_votes = []
    seen_names = set()
    sess_dates = {}
    fail = 0
    for dpub, aid in votes_arts:
        if dpub[:10] < "2024-01-01":
            continue
        det_raw = _get(f"{API}/articles/{aid}", cdir)
        try:
            det = json.loads(det_raw)
        except Exception:
            fail += 1
            continue
        atts = [a for a in (det.get("attachments") or [])
                if str(a.get("extension", "")).lower() == "pdf"]
        if not atts:
            fail += 1
            continue
        title = det.get("title", "")
        mt = re.search(r"(w sprawie|zmieniaj[aą]ca|w zmieniaj[aą]ca|dotycz[aą]c[aą])\s*(.*)$", title)
        topic = re.sub(r"\s+", " ", (mt.group(0) if mt else title)).strip(" -")
        topic = re.sub(r"^Imienny wyk[a-z]+ g\u0142osowa\u0144 radnych\s*-\s*", "", topic)
        ok = False
        for a in atts:
            link = a.get("link", "")
            url = link if link.startswith("http") else BIP + "/" + link.lstrip("/")
            try:
                p = parse_pdf(_get(url, cdir))
            except Exception:
                continue
            if not p or not p["vote_date"] or not validate(p):
                continue
            if p["vote_date"] < KAD_START:
                ok = True
                break
            nm = p["named"]
            for k in nm:
                seen_names.update(canon_name(x) for x in nm[k])
            all_votes.append({"date": p["vote_date"], "session_num": p["sess_num"],
                              "uchwala": p["uchwala"], "topic": topic,
                              **{k: [canon_name(x) for x in v] for k, v in nm.items()}})
            ok = True
            break
        if not ok:
            fail += 1
    print(f"[tuchola] zwalidowane glosowan: {len(all_votes)}, odrzuconych artykulo: {fail}")
    if len(all_votes) < 20:
        raise SystemExit(f"ZA MAŁO głosów ({len(all_votes)}) — przerywam")

    all_votes.sort(key=lambda v: (v["date"], str(v["uchwala"])))
    by_sess = defaultdict(list)
    for i, v in enumerate(all_votes, 1):
        v["id"] = str(i)
        by_sess[v["date"]].append(v)
    sessions_data = []
    for dd in sorted(by_sess):
        vs = by_sess[dd]
        nums = [v["session_num"] for v in vs if v["session_num"]]
        num = max(nums) if nums else None
        label = f"Sesja {_to_roman(num)} ({dd})" if num else f"Sesja ({dd})"
        sessions_data.append({"date": dd, "number": dd, "label": label,
                              "vote_count": len(vs)})

    votes_out = []
    for v in all_votes:
        nv = {"za": v["za"], "przeciw": v["przeciw"], "wstrzymal_sie": v["wstrzymal_sie"]}
        votes_out.append({"id": v["id"], "session_date": v["date"],
                          "session_number": v["uchwala"] or v["date"],
                          "topic": v["topic"], "named_votes": nv,
                          "counts": {"for_": len(v["za"]), "against": len(v["przeciw"]),
                                     "abstain": len(v["wstrzymal_sie"]),
                                     "absent": len(v["nieobecni"])}})
    total_votes = len(votes_out)
    total_sessions = len(sessions_data)
    councilors_seen = sorted(seen_names)
    cdata = {n: {"name": n, "club": "", "votes_za": 0, "votes_przeciw": 0,
                 "votes_wstrzymal": 0, "votes_brak": 0, "votes_nieobecny": 0}
             for n in councilors_seen}
    csess = defaultdict(set)
    for v in votes_out:
        for cat, key in (("za", "votes_za"), ("przeciw", "votes_przeciw"),
                        ("wstrzymal_sie", "votes_wstrzymal")):
            for nm in v["named_votes"][cat]:
                if nm in cdata:
                    cdata[nm][key] += 1
                    csess[nm].add(v["session_date"])
    councilors_list = []
    for cc in cdata.values():
        present = cc["votes_za"] + cc["votes_przeciw"] + cc["votes_wstrzymal"]
        councilors_list.append({
            "name": cc["name"], "club": "", "district": None,
            "frekwencja": round((len(csess.get(cc["name"], set())) / total_sessions * 100) if total_sessions else 0, 1),
            "aktywnosc": round((present / total_votes * 100) if total_votes else 0, 1),
            "zgodnosc_z_klubem": 0.0,
            "votes_za": cc["votes_za"], "votes_przeciw": cc["votes_przeciw"],
            "votes_wstrzymal": cc["votes_wstrzymal"], "votes_brak": cc["votes_brak"],
            "votes_nieobecny": cc["votes_nieobecny"],
            "votes_total": present,
            "rebellion_count": 0, "rebellions": [],
            "has_activity_data": False, "activity": None})
    kad = {"id": KADENCJA_ID, "label": KADENCJA_LABEL, "clubs": {},
           "sessions": sessions_data, "total_sessions": total_sessions,
           "total_votes": total_votes, "total_councilors": len(councilors_list),
           "councilors": councilors_list, "votes": votes_out,
           "similarity_top": [], "similarity_bottom": []}
    (docs / f"kadencja-{KADENCJA_ID}.json").write_text(
        json.dumps(kad, ensure_ascii=False, indent=1), encoding="utf-8")

    def slugify(nm):
        s = unicodedata.normalize("NFKD", nm.lower())
        s = "".join(ch for ch in s if not unicodedata.combining(ch))
        s = s.replace("ł", "l")
        return "".join(ch for ch in s if ch.isalnum() or ch == " ").strip().replace(" ", "-")

    profiles = {"profiles": [{"name": cc["name"], "slug": slugify(cc["name"]),
                              "kadencje": {KADENCJA_ID: {
                                  "club": cc["club"], "has_voting_data": True,
                                  "has_activity_data": False,
                                  "frekwencja": cc["frekwencja"], "aktywnosc": cc["aktywnosc"],
                                  "zgodnosc_z_klubem": 0.0,
                                  "votes_za": cc["votes_za"], "votes_przeciw": cc["votes_przeciw"],
                                  "votes_wstrzymal": cc["votes_wstrzymal"], "votes_brak": cc["votes_brak"],
                                  "votes_nieobecny": cc["votes_nieobecny"],
                                  "votes_total": cc["votes_total"],
                                  "rebellion_count": 0, "rebellions": [],
                                  "roles": [], "notes": "", "former": False,
                                  "mid_term": False}}}
                             for cc in councilors_list],
                "total": len(councilors_list)}
    (docs / "profiles.json").write_text(json.dumps(profiles, ensure_ascii=False, indent=1), encoding="utf-8")
    (docs / "data.json").write_text(json.dumps({
        "generated": datetime.now(timezone.utc).isoformat(),
        "default_kadencja": KADENCJA_ID,
        "kadencje": [{"id": KADENCJA_ID, "label": KADENCJA_LABEL}]},
        ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[tuchola] ZAPISANO: {total_sessions} sesji, {total_votes} głosowań, "
          f"{len(councilors_list)} radnych")


def _to_roman(n):
    vals = [(1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"),
            (90, "XC"), (50, "L"), (40, "XL"), (10, "X"), (9, "IX"), (5, "V"),
            (4, "IV"), (1, "I")]
    out = ""
    for v, sym in vals:
        while n >= v:
            out += sym
            n -= v
    return out


if __name__ == "__main__":
    main()
