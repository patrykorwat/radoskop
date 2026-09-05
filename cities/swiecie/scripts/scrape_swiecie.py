#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Radoskop Świecie — imienne głosowania Rady Miejskiej w Świeciu (IX kadencja).

Źródło: BIP bip.swiecie.eu (ten sam CMS co bip.bialogard.info), kategoria
"Protokoły z Sesji → Kadencja 2024-2029" (/artykuly/268/kadencja-2024-2029).
Każdy protokół (artykuł /artykul/268/{id}/...) ma załącznik "Wyniki do {N}"
(/attachments/download/{id}) — eksport systemu obrad z pełnymi WYNIKAMI IMIENNYMI
każdego głosowania na sesji.

Format tekstu PDF (per głosowanie):
    głosowanie {topic}
    jednostka Rada Miejska w Świeciu
    wynik Głosowanie zakończone wynikiem: {przyjęto|odrzucono|...}
    data {DD miesiąc YYYY r.} czas HH:MM:SS - HH:MM:SS
    typ głosowanie jawne imienne ...
    Podsumowanie
    status ilość procent ...
    ZA {n} ...  PRZECIW {n} ...  WSTRZYMAŁO SIĘ {n} ...
    Wyniki imienne
    lp nazwisko imię głos
    {lp} {Nazwisko} {Imię} {ZA|PRZECIW|WSTRZYMAŁ SIĘ|nieobecny|nieobecna|nie głosował|...}

Walidacja per głos: liczby imienne == Podsumowanie (ZA/PRZECIW/WSTRZYMAŁO).
"nieobecny/a" → kategoria nieobecni; "nie głosował/a" → obecny-niegłosujący
(poza named_votes, do frekwencji).

Użycie:
    python scrape_swiecie.py --city-dir <cities/swiecie> [--cache-dir dir]
Zapisuje: docs/data.json, docs/kadencja-2024-2029.json, docs/profiles.json
"""
import argparse
import hashlib
import io
import json
import re
import ssl
import time
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import pdfplumber

BIP = "https://bip.swiecie.eu"
KADENCJA_CATEGORY = "/artykuly/268/kadencja-2024-2029"
KAD_START = "2024-05-07"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"}
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

_MONTHS = {
    "stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5, "czerwca": 6,
    "lipca": 7, "sierpnia": 8, "września": 9, "pazdziernika": 10, "października": 10,
    "listopada": 11, "grudnia": 12,
}

# vote-label suffixes checked longest-first (roll-call lines end with the label)
VOTE_SUFFIXES = [
    ("WSTRZYMAŁO SIĘ", "wstrzymal_sie"), ("WSTRZYMAŁ SIĘ", "wstrzymal_sie"),
    ("WSTRZYMUJĘ SIĘ", "wstrzymal_sie"), ("nie głosowała", "nieglosuje"),
    ("nie głosował", "nieglosuje"), ("PRZECIW", "przeciw"),
    ("nieobecna", "nieobecni"), ("nieobecny", "nieobecni"),
    ("ZA", "za"),
]

_last_call = [0.0]


def _rate():
    el = time.time() - _last_call[0]
    if el < 1.0:
        time.sleep(1.0 - el)
    _last_call[0] = time.time()


def _get(url, cache_dir=None, binary=False):
    if cache_dir:
        key = hashlib.md5(url.encode()).hexdigest()
        cf = Path(cache_dir) / key
        if cf.is_file():
            return cf.read_bytes() if binary else cf.read_text(encoding="utf-8", errors="replace")
    for attempt in range(4):
        _rate()
        try:
            req = urllib.request.Request(url, headers=UA)
            r = urllib.request.urlopen(req, timeout=30, context=CTX)
            data = r.read()
            if cache_dir:
                Path(cache_dir).mkdir(parents=True, exist_ok=True)
                (Path(cache_dir) / hashlib.md5(url.encode()).hexdigest()).write_bytes(data)
            return data if binary else data.decode("utf-8", "replace")
        except Exception:
            if attempt == 3:
                raise
            time.sleep(2 + attempt * 3)


ROMAN = r"M{0,4}(?:CM|CD|D?C{0,3})(?:XC|XL|L?X{0,3})(?:IX|IV|V?I{0,3})"


def discover_sessions(cache_dir):
    """Articles /artykul/268/{id}/protokol-nr-... across category pages."""
    arts = {}
    page_urls = [f"{BIP}{KADENCJA_CATEGORY}"]
    h = _get(page_urls[0], cache_dir)
    mmax = max((int(m.group(1)) for m in re.finditer(r"/artykuly/268/(\d+)/10/kadencja-2024-2029", h)), default=1)
    for p in range(2, mmax + 1):
        page_urls.append(f"{BIP}/artykuly/268/{p}/10/kadencja-2024-2029")
    for pu in page_urls:
        try:
            h = _get(pu, cache_dir)
        except Exception:
            continue
        for m in re.finditer(r'<a href="(https://bip\.swiecie\.eu/artykul/268/\d+/[^"]+)"[^>]*>([^<]+)</a>', h):
            arts[m.group(1)] = m.group(2).strip()
    out = []
    for url, title in arts.items():
        rm = re.search(rf"Protokół Nr ({ROMAN})/(\d\d)", title)
        num = rm.group(1).upper() if rm else ""
        out.append({"url": url, "title": title, "num": num})
    return out


def find_wyniki_pdf(article_html, cache_dir):
    """Return (url, bytes) of the 'Wyniki do ...' attachment, or (None, None)."""
    links = re.findall(r'<a[^>]+href="(https://bip\.swiecie\.eu/attachments/download/\d+)"[^>]*>\s*([^<]{0,60})',
                       article_html)
    for href, txt in links:
        if re.search(r"wyniki", txt, re.I):
            return href, _get(href, cache_dir, binary=True)
    return None, None


def _split_vote_label(line):
    """('Nazwisko Imię', vote_cat) or (None, None)."""
    for suf, cat in VOTE_SUFFIXES:
        if line.endswith(suf):
            nm = line[: -len(suf)].strip()
            return nm, cat
    return None, None


def parse_pdf(data, url=""):
    """Parse one per-session Wyniki PDF → list of vote records."""
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        full = "\n".join((p.extract_text() or "") for p in pdf.pages)
    lines = full.split("\n")
    # split into per-vote chunks at '^głosowanie ' headers
    starts = [i for i, l in enumerate(lines) if re.match(r"^głosowanie \S", l)]
    records = []
    for si, start in enumerate(starts):
        end = starts[si + 1] if si + 1 < len(starts) else len(lines)
        chunk = lines[start:end]
        topic_m = re.match(r"^głosowanie (.+)$", chunk[0])
        topic = topic_m.group(1).strip() if topic_m else ""
        # topic may wrap: append until 'jednostka'
        ti = 1
        while ti < len(chunk) and not chunk[ti].startswith("jednostka"):
            topic += " " + chunk[ti].strip()
            ti += 1
        topic = re.sub(r"\s+", " ", topic).strip()
        ctext = "\n".join(chunk)
        dm = re.search(r"data (\d{1,2}) (" + "|".join(_MONTHS) + r") (\d{4}) r\.", ctext)
        date = None
        if dm:
            date = f"{dm.group(3)}-{_MONTHS[dm.group(2)]:02d}-{int(dm.group(1)):02d}"
        res_m = re.search(r"wynik Głosowanie zakończone wynikiem: (\S+)", ctext)
        result = res_m.group(1) if res_m else ""

        def summ(label):
            m = re.search(rf"^{label} (\d+) ", ctext, re.M)
            return int(m.group(1)) if m else None

        s_za, s_prz, s_wsz = summ("ZA"), summ("PRZECIW"), summ("WSTRZYMAŁO SIĘ")

        named = {"za": [], "przeciw": [], "wstrzymal_sie": [], "nieobecni": []}
        nieglosuje = []
        try:
            wi = next(i for i, l in enumerate(chunk) if l.strip() == "Wyniki imienne")
        except StopIteration:
            wi = None
        if wi is not None:
            for l in chunk[wi + 1:]:
                if l.startswith("lp nazwisko"):
                    continue
                m = re.match(r"^\d+ (.+)$", l)
                if not m:
                    break
                nm, cat = _split_vote_label(m.group(1).strip())
                if not nm:
                    continue
                parts = nm.split()
                nm_norm = " ".join(parts[1:]) + " " + parts[0] if len(parts) >= 2 else nm
                if cat == "nieglosuje":
                    nieglosuje.append(nm_norm)
                else:
                    named[cat].append(nm_norm)
        ok = (s_za is not None and date
              and len(named["za"]) == s_za
              and len(named["przeciw"]) == (s_prz or 0)
              and len(named["wstrzymal_sie"]) == (s_wsz or 0))
        if not ok:
            print(f"    [VALIDATION FAIL {url}] summ za/prz/wsz={s_za}/{s_prz}/{s_wsz} parsed "
                  f"{len(named['za'])}/{len(named['przeciw'])}/{len(named['wstrzymal_sie'])} date={date} topic={topic[:50]}")
            continue
        records.append({"date": date, "topic": topic, "result": result, "named": named,
                        "obecni_nie_glosujacy": nieglosuje})
    return records


def roman_to_int(s):
    vals = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
    tot = prev = 0
    for ch in reversed(s):
        v = vals.get(ch, 0)
        tot += v if v >= prev else -v
        prev = max(prev, v)
    return tot


def make_slug(name):
    repl = {'ą': 'a', 'ć': 'c', 'ę': 'e', 'ł': 'l', 'ń': 'n', 'ó': 'o', 'ś': 's', 'ź': 'z', 'ż': 'z',
            'Ą': 'A', 'Ć': 'C', 'Ę': 'E', 'Ł': 'L', 'Ń': 'N', 'Ó': 'O', 'Ś': 'S', 'Ź': 'Z', 'Ż': 'Z'}
    slug = name.lower()
    for pl, a in repl.items():
        slug = slug.replace(pl, a)
    return re.sub(r"[^a-z0-9]+", "", slug)


def build_output(records, club_assign=None):
    club_assign = club_assign or {}
    all_votes = []
    vid = 0
    sessions_by_date = {}
    for rec in records:
        d = rec["date"]
        if not d or d < KAD_START:
            continue
        if d not in sessions_by_date:
            sessions_by_date[d] = {"date": d, "number": rec.get("num", ""), "vote_count": 0, "attendees": set()}
        sessions_by_date[d]["vote_count"] += 1
        named = {k: list(v) for k, v in rec["named"].items()}
        for cat in ("za", "przeciw", "wstrzymal_sie"):
            sessions_by_date[d]["attendees"].update(rec["named"].get(cat, []))
        sessions_by_date[d]["attendees"].update(rec.get("obecni_nie_glosujacy", []))
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
    all_names = set()
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
            if cat == "nieobecni":
                for nm in names:
                    if nm in councilors_data:
                        councilors_data[nm]["votes_nieobecny"] += 1
                continue
            for nm in names:
                if nm not in councilors_data:
                    continue
                if cat == "za":
                    councilors_data[nm]["votes_za"] += 1
                elif cat == "przeciw":
                    councilors_data[nm]["votes_przeciw"] += 1
                else:
                    councilors_data[nm]["votes_wstrzymal"] += 1
    total_votes = len(all_votes)
    total_sessions = len(sessions_data)
    councillor_sess = defaultdict(set)
    for v in all_votes:
        for cat, names in v["named_votes"].items():
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
                                "rebellion_count": 0, "rebellions": [], "has_activity_data": False,
                                "activity": None})
    vectors = defaultdict(dict)
    for v in all_votes:
        for cat in ("za", "przeciw", "wstrzymal_sie"):
            for nm in v["named_votes"].get(cat, []):
                vectors[nm][v["id"]] = cat
    from itertools import combinations
    pairs = []
    names_sorted = sorted(vectors.keys())
    for a, b in combinations(names_sorted, 2):
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


def build_profiles(records, club_assign=None):
    club_assign = club_assign or {}
    cv = defaultdict(lambda: {"za": 0, "przeciw": 0, "wstrzymal_sie": 0, "nieobecni": 0, "votes": []})
    for rec in records:
        d = rec["date"]
        if not d or d < KAD_START:
            continue
        for cat, names in rec["named"].items():
            for nm in names:
                cv[nm][cat] += 1
                cv[nm]["votes"].append({"session": d, "vote": cat})
    profiles = []
    sess_set = {r["date"] for r in records if r["date"] >= KAD_START}
    n_sessions = len(sess_set) or 1
    for nm in sorted(cv.keys()):
        vd = cv[nm]
        total = sum(vd[k] for k in ("za", "przeciw", "wstrzymal_sie")) or 1
        sess = len({v["session"] for v in vd["votes"]})
        aktywn = (vd["za"] + vd["przeciw"] + vd["wstrzymal_sie"]) / n_sessions * 100
        profiles.append({"name": nm, "slug": make_slug(nm),
                         "kadencje": {KADENCJA_ID: {
                             "club": club_assign.get(nm, "NZ"), "has_voting_data": True,
                             "has_activity_data": False, "frekwencja": round(sess / n_sessions * 100, 1),
                             "aktywnosc": round(aktywn, 1), "zgodnosc_z_klubem": 0.0,
                             "votes_za": vd["za"], "votes_przeciw": vd["przeciw"],
                             "votes_wstrzymal": vd["wstrzymal_sie"], "votes_brak": 0,
                             "votes_nieobecny": vd["nieobecni"], "votes_total": total,
                             "rebellion_count": 0, "rebellions": [], "roles": [], "notes": "",
                             "former": False, "mid_term": False}}})
    return {"profiles": profiles, "total": len(profiles)}


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

    sessions = discover_sessions(cache)
    print(f"[swiecie] {len(sessions)} protokołów IX kad.")
    records = []
    for se in sorted(sessions, key=lambda s: roman_to_int(s["num"] or "0")):
        try:
            art = _get(se["url"], cache)
        except Exception as e:
            print(f"  [ERR page {se['url']}] {e}")
            continue
        wurl, pdfdata = find_wyniki_pdf(art, cache)
        if not pdfdata:
            print(f"  [skip {se['num']} no wyniki pdf]")
            continue
        try:
            recs = parse_pdf(pdfdata, wurl or "")
        except Exception as e:
            print(f"  [ERR pdf {wurl}] {type(e).__name__}: {e}")
            continue
        for r in recs:
            r["num"] = se["num"]
        records += recs
        print(f"  [ok] {se['num'] or '?':>4} votes={len(recs)}")

    output, total_votes, total_sessions = build_output(records, club_assign)
    profiles = build_profiles(records, club_assign)
    docs = city_dir / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "data.json").write_text(json.dumps(output, ensure_ascii=False, indent=1), encoding="utf-8")
    kad = output["kadencje"][0]
    (docs / f"kadencja-{KADENCJA_ID}.json").write_text(json.dumps(kad, ensure_ascii=False), encoding="utf-8")
    (docs / "profiles.json").write_text(json.dumps(profiles, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[swiecie] DONE votes={total_votes} sessions={total_sessions} councilors={kad['total_councilors']}")


if __name__ == "__main__":
    main()
