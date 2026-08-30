#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop Żuromin — imienne głosowania Rady Miejskiej (IX kadencja 2024-2029).

Źródło: zuromin-strona.nowybip.pl (siteor CMS) → 'Protokoły 2024-2029' page
  lists per-session protocol PDFs (fs.siteor.com/zuromin/...). Each protocol
  PDF is a full narrative session protocol that EMBEDS per-vote blocks in the
  eSesja print text-table format (same as Opalenica):
      głosowanie
      <temat głosowania>
      jednostka / Rada Miejska w Żurominie
      wynik / Głosowanie zakończone wynikiem: ...
      data / czas / typ (głosowanie jawne imienne) / większość
      Podsumowanie
      ZA <n> / PRZECIW <n> / WSTRZYMAŁO SIĘ <n> / pula głosów
      Wyniki imienne
      lp nazwisko imię głos
      1 <nazwisko> <imię> ZA
      ...
  Każde głosowanie walidowane vs agregat (za+przeciw+wstrzymal == liczba nazwisk).

PUŁAPKI obsłużone:
  (1) PDF stawia numer strony jako pierwszy wiersz KAŻDEJ strony — usuwany.
  (2) 'WSTRZYMAŁO SIĘ' rozbite na 2 wiersze; przy 'głosowanie jawne imienne'
      słowo 'głosowanie' pojawia się też jako osobna linia w 'typ' — te
      fałszywe markery nie przechodzą walidacji agregatu (skip).
  (3) PDF-y są za Cloudflare+S3 (fs.siteor.com) — wymagają SESJI (cookies
      _siteor64 po wizycie na stronie) + Referer; pobieramy przez requests.
  (4) Wykrywamy sesje po protokołach; data sesji z nagłówka 'z dnia'.
  (5) filtr PDF-ów: tylko protokoły sesji (wyklucz Komisje Wspólne / Petycje /
      Diagnozę / inne dokumenty z tej samej kategorii).

Pokrycie: sesje I..XXII IX kad. 2024-05 .. 2026-05 (12 zwykłych protokołów
  Sekcji I-XI 2024-25 + XII/XIV/XVI/XIX/XXII 2025-26).
"""
import argparse, hashlib, json, re, time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
import requests, urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

SESSION_URL = "https://zuromin-strona.nowybip.pl/protokoly-2024--2029"
PDF_HOST = "https://fs.siteor.com/zuromin"
KAD_START = "2024-05-07"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Radoskop/1.0",
           "Accept-Language": "pl,en"}
REQ_DELAY = 0.6
_LAST = 0.0

MON = {'stycznia':1,'lutego':2,'marca':3,'kwietnia':4,'maja':5,'czerwca':6,'lipca':7,
       'sierpnia':8,'września':9,'października':10,'listopada':11,'grudnia':12,
       'pazdziernika':10,'wrzesnia':9}

_GLOS = {
    'ZA':'za','PRZECIW':'przeciw',
    'WSTRZYMAŁO SIĘ':'wstrzymal_sie','WSTRZYMALO SIE':'wstrzymal_sie',
    'WSTRZYMAŁ SIĘ':'wstrzymal_sie','WSTRZYMAL SIĘ':'wstrzymal_sie',
    'WSTRZYMAŁY SIĘ':'wstrzymal_sie','WSTRZYMUJE SIĘ':'wstrzymal_sie',
    'WSTRZYMUJĘ SIĘ':'wstrzymal_sie',
    'NIEOBECNY':'nieobecny','NIE GŁOSOWAŁ':'nie_glosowal','NIEGŁOSOWAŁ':'nie_glosowal',
    'BRAK':'brak_glosu',
}
_MULTI = ['WSTRZYMAŁO SIĘ','WSTRZYMALO SIE','WSTRZYMAŁ SIĘ','WSTRZYMAŁY SIĘ',
          'WSTRZYMUJE SIĘ','WSTRZYMUJĘ SIĘ','NIE GŁOSOWAŁ','NIE GŁOSOWAŁA','NIE GŁOSOWALA']

_session = requests.Session()
_session.headers.update(HEADERS)
_session.verify = False


def _rate():
    global _LAST
    now = time.time()
    if now - _LAST < REQ_DELAY:
        time.sleep(REQ_DELAY - (now - _LAST))
    _LAST = time.time()


def _get_page(url, cache=None):
    """Fetch HTML with session (sets _siteor64 cookie)."""
    if cache is not None:
        key = hashlib.md5(("html:"+url).encode()).hexdigest()
        cf = Path(cache) / (key + ".html")
        if cf.is_file():
            return cf.read_text(encoding="utf-8", errors="ignore")
    _rate()
    r = _session.get(url, timeout=60)
    r.raise_for_status()
    if cache is not None:
        Path(cache).mkdir(parents=True, exist_ok=True)
        (Path(cache) / (hashlib.md5(("html:"+url).encode()).hexdigest() + ".html")).write_text(
            r.text, encoding="utf-8", errors="ignore")
    return r.text


def _get_pdf(url, cache=None):
    if cache is not None:
        key = hashlib.md5(("pdf:"+url).encode()).hexdigest()
        cf = Path(cache) / (key + ".bin")
        if cf.is_file():
            return cf.read_bytes()
    _rate()
    r = _session.get(url, headers={"Referer": SESSION_URL}, timeout=90)
    r.raise_for_status()
    if cache is not None:
        Path(cache).mkdir(parents=True, exist_ok=True)
        (Path(cache) / (hashlib.md5(("pdf:"+url).encode()).hexdigest() + ".bin")).write_bytes(r.content)
    return r.content


def discover_protocol_pdfs(cache):
    """ZNajdź protokoły sesji IX kad. (PDF-y) ze strony 'Protokoły 2024-2029'."""
    html = _get_page(SESSION_URL, cache)
    links = re.findall(r'href="(https://fs\.siteor\.com/zuromin[^"]+)"', html)
    out = set()
    skip_kw = ["komisje", "wspol", "wspól", "petyc", "diagno", "plan_pracy", "strateg"]
    for l in links:
        ll = l.lower()
        if any(k in ll for k in skip_kw):
            continue
        if "esji" in ll or "sesji" in ll or "sesja" in ll or "sesj" in ll:
            out.add(l)
    return sorted(out)


def read_cleaned_pdf(data):
    import fitz
    d = fitz.open(stream=data, filetype="pdf")
    pages = []
    for i in range(d.page_count):
        lines = d[i].get_text().split("\n")
        if lines and lines[0].strip().isdigit():
            lines = lines[1:]
        pages.append("\n".join(lines))
    return "\n".join(pages), d


def session_date_from_pdf(doc):
    cover = doc[0].get_text()
    m = re.search(r'z dnia\s+(\d{1,2})\s+(\w+)\s+(\d{4})', cover) or \
        re.search(r'z dnia\s+(\d{1,2})\s+(\w+)\s+(\d{4})', cover, re.I)
    if m:
        return f"{m.group(3)}-{MON.get(m.group(2).lower(),0):02d}-{int(m.group(1)):02d}"
    return None


def split_vote_blocks(text):
    lines = text.split("\n")
    glos = [i for i, l in enumerate(lines) if l.strip().lower() in ("glosowanie", "głosowanie")]
    blocks = []
    for j in range(len(glos)):
        st = glos[j]
        en = glos[j + 1] if j + 1 < len(glos) else len(lines)
        blocks.append(lines[st:en])
    return blocks


def _topic_of(block):
    """Temat głosowania = pierwsza linia PO markerze 'głosowanie'."""
    for l in block:
        s = l.strip()
        if s and s.lower() not in ("glosowanie", "głosowanie") and s != "jednostka":
            return s.rstrip(":").strip()
    return ""


def _glos_key(tok_upper):
    return _GLOS.get(tok_upper)


def _parse_rows(tail):
    toks = [t.strip() for t in tail.split("\n")]
    merged = []
    i = 0
    while i < len(toks):
        if i + 1 < len(toks):
            two = toks[i].upper() + " " + toks[i + 1].upper()
            if two in _GLOS or two in _MULTI:
                merged.append(two); i += 2; continue
        merged.append(toks[i]); i += 1
    rows = []
    pending = []
    for t in merged:
        if not t:
            continue
        if t.isdigit():
            pending = []
            continue
        gk = _glos_key(t.upper())
        if gk:
            nm = ""
            if pending:
                nm = (" ".join(pending[1:]) + " " + pending[0]).strip()
            rows.append((nm, gk))
            pending = []
        else:
            pending.append(t)
    return rows


def parse_pdf_text(full_text):
    """Zwraca listę zwalidowanych głosowań (aggregate == sumy list)."""
    out = []
    for raw in split_vote_blocks(full_text):
        txt = "\n".join(raw)
        dm = re.search(r'z dnia\s+(\d{1,2})\s+(\w+)\s+(\d{4})', txt)
        date = None
        if dm:
            date = f"{dm.group(3)}-{MON.get(dm.group(2).lower(),0):02d}-{int(dm.group(1)):02d}"
        topic = _topic_of(raw)
        agg = re.search(r'\bZA\b\s*\n\s*(\d+).*?\bPRZECIW\b\s*\n\s*(\d+).*?\bWSTRZYMAŁO\s+SIĘ\b\s*\n\s*(\d+)', txt, re.S)
        if not agg:
            agg = re.search(r'\bZA\b\s*\n\s*(\d+).*?\bPRZECIW\b\s*\n\s*(\d+).*?\bWSTRZYMALO\s+SIE\b\s*\n\s*(\d+)', txt, re.S)
        if not agg:
            continue
        a, p, wz = int(agg.group(1)), int(agg.group(2)), int(agg.group(3))
        gm = re.search(r'(?m)^\s*głos\s*$', txt)
        gi = gm.end() if gm else -1
        named = {"za": [], "przeciw": [], "wstrzymal_sie": []}
        if gi >= 0:
            for nm, gk in _parse_rows(txt[gi:]):
                if gk in named and nm:
                    named[gk].append(nm)
        if a != len(named["za"]) or p != len(named["przeciw"]) or wz != len(named["wstrzymal_sie"]):
            continue
        out.append({"topic": topic, "date": date, "za": a, "przeciw": p,
                    "wstrzymal_sie": wz, "named": named})
    return out


def make_slug(name):
    repl = {'ą':'a','ć':'c','ę':'e','ł':'l','ń':'n','ó':'o','ś':'s','ź':'z','ż':'z'}
    slug = name.lower()
    for pl, a in repl.items():
        slug = slug.replace(pl, a)
    return re.sub(r"[^a-z0-9]+", "", slug)


def build_output(records):
    all_votes = []
    vid = 0
    sessions_by_date = {}
    for rec in records:
        d = rec.get("date") or ""
        if not d or d < KAD_START:
            continue
        if d not in sessions_by_date:
            sessions_by_date[d] = {"date": d, "number": d, "vote_count": 0, "attendees": set()}
        sessions_by_date[d]["vote_count"] += 1
        vid += 1
        for cat in ("za", "przeciw", "wstrzymal_sie"):
            sessions_by_date[d]["attendees"].update(rec["named"].get(cat, []))
        all_votes.append({"id": str(vid), "session_date": d, "session_number": d,
                          "topic": rec.get("topic", ""), "named_votes": rec["named"],
                          "counts": {"za": rec["za"], "przeciw": rec["przeciw"],
                                     "wstrzymal_sie": rec["wstrzymal_sie"]}})
    sessions_data = []
    for d in sorted(sessions_by_date.keys()):
        s = sessions_by_date[d]
        sessions_data.append({"date": d, "number": s["number"], "vote_count": s["vote_count"],
                              "attendee_count": len(s["attendees"]),
                              "attendees": sorted(s["attendees"]), "speakers": []})
    all_names = set()
    for v in all_votes:
        for names in v["named_votes"].values():
            all_names.update(names)
    councilors = {n: {"name": n, "club": "", "district": None, "votes_za": 0,
                      "votes_przeciw": 0, "votes_wstrzymal": 0, "votes_brak": 0,
                      "votes_nieobecny": 0, "rebellions": []} for n in all_names}
    for v in all_votes:
        for cat, names in v["named_votes"].items():
            key = {"za": "votes_za", "przeciw": "votes_przeciw",
                   "wstrzymal_sie": "votes_wstrzymal"}.get(cat, "votes_wstrzymal")
            for nm in names:
                if nm in councilors:
                    councilors[nm][key] += 1
    total_votes = len(all_votes)
    total_sessions = len(sessions_data)
    councillor_sess = defaultdict(set)
    for v in all_votes:
        for cat, names in v["named_votes"].items():
            for nm in names:
                councillor_sess[nm].add(v["session_date"])
    councilors_list = []
    for c in sorted(councilors.values(), key=lambda x: x["name"]):
        present = c["votes_za"] + c["votes_przeciw"] + c["votes_wstrzymal"]
        aktywnosc = (present / total_votes * 100) if total_votes else 0
        frekwencja = (len(councillor_sess.get(c["name"], set())) / total_sessions * 100) if total_sessions else 0
        councilors_list.append({"name": c["name"], "club": c["club"], "district": None,
            "frekwencja": round(frekwencja, 1), "aktywnosc": round(aktywnosc, 1),
            "zgodnosc_z_klubem": 0.0, "votes_za": c["votes_za"],
            "votes_przeciw": c["votes_przeciw"], "votes_wstrzymal": c["votes_wstrzymal"],
            "votes_brak": c["votes_brak"], "votes_nieobecny": c["votes_nieobecny"],
            "votes_total": total_votes, "rebellion_count": 0, "rebellions": [],
            "has_activity_data": False, "activity": None})
    vectors = defaultdict(dict)
    for v in all_votes:
        for cat in ("za", "przeciw", "wstrzymal_sie"):
            for nm in v["named_votes"].get(cat, []):
                vectors[nm][v["id"]] = cat
    pairs = []
    names_sorted = sorted(vectors.keys())
    from itertools import combinations
    for a, b in combinations(names_sorted, 2):
        common = set(vectors[a]) & set(vectors[b])
        if len(common) < 10:
            continue
        same = sum(1 for x in common if vectors[a][x] == vectors[b][x])
        pairs.append({"a": a, "b": b, "club_a": "", "club_b": "",
                      "score": round(same / len(common) * 100, 1), "common_votes": len(common)})
    pairs.sort(key=lambda x: x["score"], reverse=True)
    kad = {"id": KADENCJA_ID, "label": KADENCJA_LABEL, "clubs": {},
           "sessions": sessions_data, "total_sessions": total_sessions,
           "total_votes": total_votes, "total_councilors": len(councilors_list),
           "councilors": councilors_list, "votes": all_votes,
           "similarity_top": pairs[:20], "similarity_bottom": pairs[-20:][::-1]}
    return {"generated": datetime.now().isoformat(), "default_kadencja": KADENCJA_ID,
            "kadencje": [kad]}


def build_profiles(records):
    cv = defaultdict(lambda: {"za": 0, "przeciw": 0, "wstrzymal_sie": 0, "votes": []})
    sess = set()
    n_votes = 0
    for rec in records:
        d = rec.get("date") or ""
        if d < KAD_START:
            continue
        n_votes += 1
        sess.add(d)
        for cat, names in rec["named"].items():
            for nm in names:
                cv[nm][cat] += 1
                cv[nm]["votes"].append({"session": d, "vote": cat})
    profiles = []
    n_sessions = len(sess) or 1
    for nm in sorted(cv.keys()):
        vd = cv[nm]
        total = sum(vd[k] for k in ("za", "przeciw", "wstrzymal_sie")) or 1
        sess_n = len({v["session"] for v in vd["votes"]})
        aktywn = (vd["za"] + vd["przeciw"] + vd["wstrzymal_sie"]) / max(1, n_votes) * 100
        profiles.append({"name": nm, "slug": make_slug(nm),
                         "kadencje": {KADENCJA_ID: {"club": "", "has_voting_data": True,
                             "has_activity_data": False, "frekwencja": round(sess_n / n_sessions * 100, 1),
                             "aktywnosc": round(aktywn, 1), "zgodnosc_z_klubem": 0.0,
                             "votes_za": vd["za"], "votes_przeciw": vd["przeciw"],
                             "votes_wstrzymal": vd["wstrzymal_sie"], "votes_brak": 0,
                             "votes_nieobecny": 0, "votes_total": total,
                             "rebellion_count": 0, "rebellions": [], "roles": [],
                             "notes": "", "former": False, "mid_term": False}}})
    return {"profiles": profiles, "total": len(profiles)}


def save_split(output, out_path, profiles):
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    stubs = []
    for kad in output.get("kadencje", []):
        kid = kad["id"]
        stubs.append({"id": kid, "label": kad.get("label", f"Kadencja {kid}")})
        (out_path.parent / f"kadencja-{kid}.json").write_text(
            json.dumps(kad, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    out_path.write_text(json.dumps({"generated": output.get("generated", ""),
                                    "default_kadencja": output.get("default_kadencja", ""),
                                    "kadencje": stubs}, ensure_ascii=False,
                                   separators=(",", ":")), encoding="utf-8")
    (out_path.parent / "profiles.json").write_text(
        json.dumps(profiles, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--city-dir", required=True)
    ap.add_argument("--cache-dir", default=None)
    args = ap.parse_args()
    city_dir = Path(args.city_dir)
    cache = Path(args.cache_dir) if args.cache_dir else None
    pdfs = discover_protocol_pdfs(cache)
    print(f"[zuromin] protocol pdfs: {len(pdfs)}")
    per_date = {}
    for url in pdfs:
        try:
            data = _get_pdf(url, cache)
        except Exception as e:
            print(f"  [ERR fetch] {e}"); continue
        try:
            full_text, doc = read_cleaned_pdf(data)
        except Exception as e:
            print(f"  [ERR pdf] {e}"); continue
        sdate = session_date_from_pdf(doc)
        vs = parse_pdf_text(full_text)
        if not sdate or sdate < KAD_START:
            print(f"  [skip {sdate}] poza IX kad / brak daty")
            continue
        if len(vs) == 0:
            print(f"  [stale/glitch {sdate}] 0 zwalidowanych — pomijam")
            continue
        prev = per_date.get(sdate)
        if prev is None or len(vs) > len(prev):
            per_date[sdate] = vs
        print(f"  {sdate} votes={len(vs)}")
    records = []
    for sdate, vs in sorted(per_date.items()):
        for v in vs:
            v["date"] = sdate
            records.append(v)
    if not records:
        print("[zuromin] BRAK danych — nie generuję")
        return 1
    output = build_output(records)
    profiles = build_profiles(records)
    save_split(output, city_dir / "docs" / "data.json", profiles)
    # copy config.json into docs for SPA
    cfg_path = city_dir / "config.json"
    if cfg_path.is_file():
        (city_dir / "docs" / "config.json").write_text(cfg_path.read_text(encoding="utf-8"), encoding="utf-8")
    k = output["kadencje"][0]
    print(f"[zuromin] TOTAL votes={k['total_votes']} sessions={k['total_sessions']} councilors={k['total_councilors']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
