#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Radoskop Głogów — imienne głosowania Rady Miejskiej (IX kadencja).

Źródło: BIP glogow.bip.info.pl (nowa platforma b.info.pl, Angular SPA + JSON API).
API wyszukiwarki: GET /api/fo/search/getResult?page&count&contains=<q> zwraca
CmsFile-e z breadcrumbs; kategoria „Wyniki głosowań jawnych z sesji Rady Miejskiej
kadencja 2024 - 2029" zawiera per-sesję pliki DOCX „wyniki głosowań jawnych z sesji
Nr ...". Plik przez /api/fo/files/{id}/download.

DOCX = eksport eSesja 'Protokół z głosowań' (format tekstowy ZA(n)/listy): bloki
'Głosowano w sprawie: … Wyniki głosowania ZA: n, PRZECIW: n, WSTRZYMUJĘ SIĘ: n,
BRAK GŁOSU: n, NIEOBECNI: n / Wyniki imienne: ZA (n) <lista> …'. Parser jak
cities/naklo-nad-notecia, ale tekst wyciągany per-paragraf z word/document.xml
(bez sztucznych spacji wewnątrz nazwisk).

eSesja glogow.esesja.pl = Portal Mieszkańca instance B (martwa lista) — niewykorzystana.

Użycie:
    python scrape_glogow.py --city-dir <cities/glogow> [--cache-dir dir]
Zapisuje: docs/data.json, docs/kadencja-2024-2029.json, docs/profiles.json
"""
import argparse
import hashlib
import io
import json
import re
import ssl
import time
import unicodedata
import zipfile
from collections import Counter, defaultdict
from datetime import datetime
from itertools import combinations
from pathlib import Path
from urllib.request import Request, urlopen

BIP = "https://glogow.bip.info.pl"
SEARCH = BIP + "/api/fo/search/getResult"
KAD_CAT_KEY = "kadencja 2024"
KAD_START = "2024-05-07"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"

MONTHS = {"stycznia":1,"lutego":2,"marca":3,"kwietnia":4,"maja":5,"czerwca":6,"lipca":7,
          "sierpnia":8,"września":9,"października":10,"listopada":11,"grudnia":12,
          "styczeń":1,"luty":2,"marzec":3,"kwiecień":4}

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE
_UA = {"User-Agent": "Mozilla/5.0 (Radoskop; info@radoskop.eu)", "Accept": "application/json"}

REQ_DELAY = 0.3
_LAST = 0.0


def _rate():
    global _LAST
    d = time.time() - _LAST
    if d < REQ_DELAY:
        time.sleep(REQ_DELAY - d)
    _LAST = time.time()


def _get(url, cache_dir, binary=False):
    key = hashlib.md5(url.encode()).hexdigest()
    if cache_dir:
        cd = Path(cache_dir)
        cd.mkdir(parents=True, exist_ok=True)
        cf = cd / (key + (".bin" if binary else ".dat"))
        if cf.is_file():
            data = cf.read_bytes()
            return data if binary else data.decode("utf-8", "ignore")
    _rate()
    data = urlopen(Request(url, headers=_UA), timeout=60, context=CTX).read()
    if cache_dir:
        (Path(cache_dir) / (key + (".bin" if binary else ".dat"))).write_bytes(data)
    return data if binary else data.decode("utf-8", "ignore")


def _nk(s):
    s = s.lower().replace("ł", "l")
    s = unicodedata.normalize("NFKD", s)
    return re.sub(r"[^a-z0-9]", "", "".join(c for c in s if not unicodedata.combining(c)))


def make_slug(s):
    s = s.lower().replace("ł", "l")
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


# ---------------- discovery ----------------
_ROM = {t: i for i, t in enumerate(
    ["I","II","III","IV","V","VI","VII","VIII","IX","X","XI","XII","XIII","XIV","XV",
     "XVI","XVII","XVIII","XIX","XX","XXI","XXII","XXIII","XXIV","XXV","XXVI","XXVII",
     "XXVIII","XXIX","XXX","XXXI","XXXII","XXXIII","XXXIV","XXXV","XXXVI","XXXVII",
     "XXXVIII","XXXIX","XL","XLI","XLII","XLIII","XLIV","XLV","XLVI"], 1)}


def _session_date(title):
    m = re.search(r"(\d{1,2})[.\s]+(\d{1,2})[.\s]+(\d{4})", title)
    if m:
        dd, mm, yy = m.groups()
        return f"{yy}-{int(mm):02d}-{int(dd):02d}"
    m = re.search(r"(\d{1,2})\s+(" + "|".join(MONTHS) + r")\.?\s+(\d{4})", title, re.I)
    if m:
        return f"{m.group(3)}-{MONTHS[m.group(2).lower()]:02d}-{int(m.group(1)):02d}"
    return None


def discover_files(cache):
    """Wszystkie CmsFile z kategorii kadencja 2024-2029 (paginacja search API)."""
    import urllib.parse
    seen = {}
    page = 1
    while True:
        u = f"{SEARCH}?page={page}&count=100&contains={urllib.parse.quote('Wyniki głosowań')}"
        d = json.loads(_get(u, cache))
        meta = d["meta"]
        for x in d["data"]:
            if x.get("type") != "CmsFile":
                continue
            a = x["attributes"]
            bc = [b["attributes"]["title"] for b in a.get("additionalFields", {}).get("breadcrumbs", [])]
            if not any(KAD_CAT_KEY in b.lower() for b in bc):
                continue
            url = a.get("url", "")
            if "/api/fo/files/" not in url:
                continue
            fid = url.rstrip("/").split("/")[-2]
            title = a.get("title") or a.get("content") or ""
            if fid not in seen or (title and not seen[fid][0]):
                seen[fid] = (title.strip(), url)
        if page >= meta.get("pages", 1):
            break
        page += 1
    out = []
    for fid, (title, url) in seen.items():
        date = _session_date(title)
        rm = re.search(r"(?:sesji|Sesji|Sesja)\s+(?:nr\s+)?([IVXL]+)", title)
        out.append({"fid": fid, "url": url, "title": title, "date": date,
                    "roman": rm.group(1) if rm else ""})
    return sorted(out, key=lambda x: x["date"] or "0000")


# ---------------- DOCX text ----------------
def docx_text(data):
    z = zipfile.ZipFile(io.BytesIO(data))
    xml = z.read("word/document.xml").decode("utf-8")
    paras = []
    for pm in re.finditer(r"<w:p[ >].*?</w:p>", xml, re.S):
        txt = "".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", pm.group(0)))
        txt = (txt.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">"))
        paras.append(txt)
    return "\n".join(paras)


# ---------------- parsing (naklo pattern na tekście DOCX) ----------------
_LABEL_RE = re.compile(r"\b(ZA|PRZECIW|WSTRZYMUJĘ SIĘ|BRAK GŁOSU|NIEOBECNI)\s*\((\d+)\)")
_COUNTS_RE = re.compile(
    r"ZA:\s*(\d+),?\s*PRZECIW:\s*(\d+),?\s*WSTRZYMUJĘ SIĘ:\s*(\d+),?\s*"
    r"BRAK GŁOSU:\s*(\d+),?\s*NIEOBECNI:\s*(\d+)")
_CAT_MAP = {"ZA": "za", "PRZECIW": "przeciw", "WSTRZYMUJĘ SIĘ": "wstrzymal_sie",
            "BRAK GŁOSU": "brak", "NIEOBECNI": "nieobecni"}
_NAME_OK = re.compile(r"^[A-ZŁŚŹŻÓĆĘĄŃ][\wŁłŚśŹźŻżÓóĆćĘęĄąŃń'’\-]*(\s+[A-ZŁŚŹŻÓĆĘĄŃ][\wŁłŚśŹźŻżÓóĆćĘęĄąŃń'’\-]*)+$")


def _clean_name(s):
    s = re.sub(r"\s+", " ", s.strip())
    if not s or not any(c.isalpha() for c in s):
        return None
    if not _NAME_OK.match(s):
        return None
    return s


def _parse_named_pairs(section):
    inv = {"ZA": "za", "PRZECIW": "przeciw", "WSTRZYMUJĘ SIĘ": "wstrzymal_sie",
           "BRAK GŁOSU": "brak", "NIEOBECNI": "nieobecni"}
    name_mark = re.compile(r"([\wŁłŚśŹźŻżÓóĆćĘęĄąŃń'’\-]+(?:\s+[\wŁłŚśŹźŻżÓóĆćĘęĄąŃń'’\-]+)*)\s*\((ZA|PRZECIW|WSTRZYMUJĘ SIĘ|BRAK GŁOSU|NIEOBECNI)\)")
    named = defaultdict(list)
    for nm, mark in name_mark.findall(re.sub(r"\s+", " ", section)):
        named[inv[mark]].append(re.sub(r"\s+", " ", nm.strip()))
    return dict(named)


def _parse_named_labels(section):
    labels = list(_LABEL_RE.finditer(section))
    named = defaultdict(list)
    for i, m in enumerate(labels):
        cat = _CAT_MAP.get(m.group(1))
        if not cat:
            continue
        start = m.end()
        end = labels[i + 1].start() if i + 1 < len(labels) else len(section)
        toks = []
        for line in section[start:end].splitlines():
            line = line.strip().rstrip(";")
            if not line:
                continue
            parts = [t for t in (_clean_name(x) for x in line.split(",")) if t]
            n_raw = len([x for x in line.split(",") if x.strip()])
            if len(parts) != n_raw:
                break
            toks.extend(parts)
        named[cat] = toks
    return dict(named)


def _score(named, counts):
    return sum(1 for k, v in counts.items() if len(named.get(k, [])) == v)


def parse_report_text(text, session_date, roman):
    """Warianty raportowe: nagłówek 'Głosowano [wniosek] w sprawie: <temat>
    - czas głosowania: …' + agregaty + 'Wyniki imienne:' z listą IM to
    pary 'Name (ZA)' IM etykiety 'ZA (n)' + wiersz nazwisk — per blok."""
    records = []
    head = re.compile(
        r"(?:(\d+\.\s*)?Głosowano (?:wniosek )?w sprawie[:\s]|Głosowanie w sprawie\s+)(.{5,400}?) - czas głosowania:")
    heads = list(head.finditer(text))
    for i, m in enumerate(heads):
        start = m.end()
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        blk = text[start:end]
        cm = _COUNTS_RE.search(blk)
        if not cm:
            continue
        topic = re.sub(r"\s+", " ", m.group(2)).strip(" .,;:")
        topic = re.sub(r"^[\d.]+\s*", "", topic) or "(glosowanie)"
        counts = {"za": int(cm.group(1)), "przeciw": int(cm.group(2)),
                  "wstrzymal_sie": int(cm.group(3)), "brak": int(cm.group(4)),
                  "nieobecni": int(cm.group(5))}
        wi = blk.find("Wyniki imienne")
        if wi == -1:
            continue
        section = blk[wi:]
        a = _parse_named_pairs(section)
        b = _parse_named_labels(section)
        sa, sb = _score(a, counts), _score(b, counts)
        # pary wygrywaja remis (bardziej eksplicytne), ale labels przy pelnej reconcili
        named = b if sb > sa else a
        ok = _score(named, counts) == len(counts)
        records.append({"date": session_date, "num": roman, "topic": topic,
                        "named": dict(named), "counts": counts, "ok": ok})
    return records


def parse_text(text, session_date, roman):
    """Blok głosowania = 'Głosowano w sprawie:' … 'Wyniki głosowanie…' … 'Wyniki imienne:'."""
    if "- czas głosowania:" in text:
        return parse_report_text(text, session_date, roman)
    records = []
    blocks = re.split(r"(?=Głosowano w sprawie)", text)
    for blk in blocks:
        if "Wyniki imienne" not in blk:
            continue
        cm = _COUNTS_RE.search(blk)
        if not cm:
            continue
        counts = {"za": int(cm.group(1)), "przeciw": int(cm.group(2)),
                  "wstrzymal_sie": int(cm.group(3)), "brak": int(cm.group(4)),
                  "nieobecni": int(cm.group(5))}
        gs = blk.find("Głosowano w sprawie:")
        topic_raw = blk[gs + len("Głosowano w sprawie:"):cm.start()]
        topic = re.sub(r"\s+", " ", topic_raw).strip(" .,:;-")
        topic = re.sub(r"^[\d.]+\s*", "", topic)
        topic = topic or "(glosowanie)"
        wi = blk.find("Wyniki imienne")
        remainder = blk[wi:]
        labels = list(_LABEL_RE.finditer(remainder))
        named = defaultdict(list)
        for i, m in enumerate(labels):
            cat = _CAT_MAP.get(m.group(1))
            if not cat:
                continue
            start = m.end()
            end = labels[i + 1].start() if i + 1 < len(labels) else len(remainder)
            # zachowaj wiersze: nazwiska sa w jednym wierszu (rozdzielone przecinami);
            # pierwszy wiersz ktory nie jest lista nazwisk = koniec sekcji (dym stopki)
            toks = []
            for line in remainder[start:end].splitlines():
                line = line.strip().rstrip(";")
                if not line:
                    continue
                parts = [t for t in (_clean_name(x) for x in line.split(",")) if t]
                n_raw = len([x for x in line.split(",") if x.strip()])
                if len(parts) != n_raw:
                    break
                toks.extend(parts)
            named[cat] = toks
        ok = all(len(named.get(k, [])) == v for k, v in counts.items())
        records.append({"date": session_date, "num": roman, "topic": topic,
                        "named": dict(named), "counts": counts, "ok": ok})
    return records


# ---------------- output (stargard pattern) ----------------
def build_output(records, roster, club_assign=None):
    club_assign = club_assign or {}
    sessions_by_date = defaultdict(lambda: {"number": "", "vote_count": 0, "attendees": set()})
    all_votes = []
    vid = 0
    for rec in records:
        d = rec["date"]
        if not d or d < KAD_START:
            continue
        sessions_by_date[d]["vote_count"] += 1
        if not sessions_by_date[d]["number"]:
            sessions_by_date[d]["number"] = rec.get("num", "")
        named = {k: v for k, v in rec["named"].items() if k in ("za", "przeciw", "wstrzymal_sie")}
        if rec["named"].get("nieobecni"):
            named["nieobecni"] = rec["named"]["nieobecni"]
        if rec["named"].get("brak"):
            named["nie_glosowal"] = rec["named"]["brak"]
        for cat in ("za", "przeciw", "wstrzymal_sie", "nie_glosowal"):
            sessions_by_date[d]["attendees"].update(rec["named"].get(cat, []))
        vid += 1
        all_votes.append({"id": str(vid), "session_date": d, "session_number": rec.get("num", ""),
                          "topic": rec.get("topic", ""), "named_votes": named,
                          "counts": {k: len(named.get(k, [])) for k in ("za", "przeciw", "wstrzymal_sie")}})
    sessions_data = []
    for d in sorted(sessions_by_date.keys()):
        s = sessions_by_date[d]
        sessions_data.append({"date": d, "number": s["number"], "vote_count": s["vote_count"],
                              "attendee_count": len(s["attendees"]), "attendees": sorted(s["attendees"]),
                              "speakers": []})
    all_names = set(roster)
    for v in all_votes:
        for names in v["named_votes"].values():
            all_names.update(names)
    councilors_data = {}
    for name in all_names:
        councilors_data[name] = {"name": name, "club": club_assign.get(name, "NZ"), "district": None,
                                 "votes_za": 0, "votes_przeciw": 0, "votes_wstrzymal": 0,
                                 "votes_brak": 0, "votes_nieobecny": 0, "rebellions": []}
    for v in all_votes:
        for cat, names in v["named_votes"].items():
            for nm in names:
                if nm not in councilors_data:
                    continue
                if cat == "za":
                    councilors_data[nm]["votes_za"] += 1
                elif cat == "przeciw":
                    councilors_data[nm]["votes_przeciw"] += 1
                elif cat == "wstrzymal_sie":
                    councilors_data[nm]["votes_wstrzymal"] += 1
    total_votes = len(all_votes)
    total_sessions = len(sessions_data)
    councillor_sess = defaultdict(set)
    for v in all_votes:
        for names in v["named_votes"].values():
            for nm in names:
                councillor_sess[nm].add(v["session_date"])
    councilors_list = []
    for c in sorted(councilors_data.values(), key=lambda x: x["name"]):
        present = c["votes_za"] + c["votes_przeciw"] + c["votes_wstrzymal"]
        aktywn = (present / total_votes * 100) if total_votes else 0
        frekw = (len(councillor_sess.get(c["name"], set())) / total_sessions * 100) if total_sessions else 0
        councilors_list.append({"name": c["name"], "club": c["club"], "district": None,
                                "frekwencja": round(frekw, 1), "aktywnosc": round(aktywn, 1),
                                "zgodnosc_z_klubem": 0.0,
                                "votes_za": c["votes_za"], "votes_przeciw": c["votes_przeciw"],
                                "votes_wstrzymal": c["votes_wstrzymal"], "votes_brak": c["votes_brak"],
                                "votes_nieobecny": c["votes_nieobecny"], "votes_total": total_votes,
                                "rebellion_count": 0, "rebellions": [],
                                "has_activity_data": False, "activity": None})
    vectors = defaultdict(dict)
    for v in all_votes:
        for cat in ("za", "przeciw", "wstrzymal_sie"):
            for nm in v["named_votes"].get(cat, []):
                vectors[nm][v["id"]] = cat
    pairs = []
    for a, b in combinations(sorted(vectors.keys()), 2):
        common = set(vectors[a].keys()) & set(vectors[b].keys())
        if len(common) < 10:
            continue
        same = sum(1 for v_id in common if vectors[a][v_id] == vectors[b][v_id])
        pairs.append({"a": a, "b": b, "club_a": "", "club_b": "",
                      "score": round(same / len(common) * 100, 1), "common_votes": len(common)})
    pairs.sort(key=lambda x: x["score"], reverse=True)
    kad = {"id": KADENCJA_ID, "label": KADENCJA_LABEL,
           "clubs": dict(Counter(club_assign.get(c["name"], "NZ") for c in councilors_list)),
           "sessions": sessions_data, "total_sessions": total_sessions,
           "total_votes": total_votes, "total_councilors": len(councilors_list),
           "councilors": councilors_list, "votes": all_votes,
           "similarity_top": pairs[:20], "similarity_bottom": pairs[-20:][::-1]}
    return {"generated": datetime.now().isoformat(), "default_kadencja": KADENCJA_ID, "kadencje": [kad]}, total_votes, total_sessions


def build_profiles(records, roster, club_assign=None):
    club_assign = club_assign or {}
    cv = defaultdict(lambda: {"za": 0, "przeciw": 0, "wstrzymal_sie": 0, "nieobecni": 0, "votes": []})
    for rec in records:
        d = rec["date"]
        if not d or d < KAD_START:
            continue
        for cat, names in rec["named"].items():
            if cat == "brak":
                cat = "brak_"
            for nm in names:
                if cat in cv[nm]:
                    cv[nm][cat] += 1
                cv[nm]["votes"].append({"session": d, "vote": cat})
    for nm in roster:
        cv.setdefault(nm, {"za": 0, "przeciw": 0, "wstrzymal_sie": 0, "nieobecni": 0, "votes": []})
    profiles = []
    sess_set = {r["date"] for r in records if r["date"] and r["date"] >= KAD_START}
    n_sessions = len(sess_set) or 1
    for nm in sorted(cv.keys()):
        vd = cv[nm]
        total = sum(vd[k] for k in ("za", "przeciw", "wstrzymal_sie")) or 1
        sess = len({v["session"] for v in vd["votes"]})
        aktywn = (vd["za"] + vd["przeciw"] + vd["wstrzymal_sie"]) / n_sessions * 100
        profiles.append({"name": nm, "slug": make_slug(nm),
                         "kadencje": {KADENCJA_ID: {"club": club_assign.get(nm, "NZ"),
                             "has_voting_data": True, "has_activity_data": False,
                             "frekwencja": round(sess / n_sessions * 100, 1),
                             "aktywnosc": round(aktywn, 1), "zgodnosc_z_klubem": 0.0,
                             "votes_za": vd["za"], "votes_przeciw": vd["przeciw"],
                             "votes_wstrzymal": vd["wstrzymal_sie"], "votes_brak": 0,
                             "votes_nieobecny": vd["nieobecni"], "votes_total": total,
                             "rebellion_count": 0, "rebellions": [], "roles": [],
                             "notes": "", "former": False, "mid_term": False}}})
    return {"profiles": profiles, "total": len(profiles)}


def canon_name(name, canon):
    toks = name.split()
    if len(toks) < 2:
        return name
    key = tuple(sorted(_nk(t) for t in toks))
    return canon.get(key, name)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--city-dir", required=True)
    ap.add_argument("--cache-dir", default=None)
    args = ap.parse_args()
    city_dir = Path(args.city_dir)
    cache = Path(args.cache_dir) if args.cache_dir else None
    cfg = {}
    if (city_dir / "config.json").is_file():
        cfg = json.loads((city_dir / "config.json").read_text(encoding="utf-8"))
    club_assign = cfg.get("club_assignments", {}) or {}
    files = [f for f in discover_files(cache) if f["date"] and f["date"] >= KAD_START]
    print(f"[glogow] {len(files)} plikow sesji IX kad.")
    canon = {}
    records = []
    roster = set()
    bad = 0
    seen_date = {}
    for f in files:
        try:
            data = _get(f["url"], cache, binary=True)
            if f["url"].split("?")[0].endswith(".pdf") or data[:4] == b"%PDF":
                continue
            text = docx_text(data)
            recs = parse_text(text, f["date"], f["roman"])
            if not recs:
                print(f"  [skip {f['date']}] brak blokow imiennych ({f['title'][:50]})")
                continue
            # dedup: ten sam dzień i ten sam pierwszy topic = duplikat pliku
            sig = (f["date"], recs[0]["topic"])
            if sig in seen_date:
                continue
            seen_date[sig] = f["fid"]
            for r in recs:
                r["named"] = {cat: [canon_name(nm, canon) for nm in names]
                              for cat, names in r["named"].items()}
                if not r["ok"]:
                    bad += 1
                for names in r["named"].values():
                    roster.update(names)
                records.append(r)
            print(f"  [ok] {f['date']} {f['roman']:>5} votes={len(recs)}")
        except Exception as e:
            print(f"  [ERR {f['date']}] {type(e).__name__}: {e}")
    if bad:
        print(f"[glogow] WARNING {bad} głosów bez reconciliacji")
    output, total_votes, total_sessions = build_output(records, roster, club_assign)
    profiles = build_profiles(records, roster, club_assign)
    docs = city_dir / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "kadencja-2024-2029.json").write_text(
        json.dumps(output["kadencje"][0], ensure_ascii=False, indent=1), encoding="utf-8")
    data = {"generated": output["generated"], "default_kadencja": KADENCJA_ID,
            "kadencje": [{"id": KADENCJA_ID, "label": KADENCJA_LABEL}]}
    (docs / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    (docs / "profiles.json").write_text(json.dumps(profiles, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[glogow] ZAPISANO votes={total_votes} sessions={total_sessions} councilors={len(profiles['profiles'])} bad={bad}")


if __name__ == "__main__":
    main()
