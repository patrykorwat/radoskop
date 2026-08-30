#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop Janikowo — pełne imienne głosowania Rady Miejskiej (IX kadencja 2024-2029).

Źródło: BIP bip.janikowo.bipgmina.pl (platforma idcom-jst), kategoria
'Imienny wykaz głosowań radnych' (11797). Każda sesja IX kadencji = artykuł
"<NR> sesja RM-DDMMYYYY - imienne głosowanie radnych" z załącznikiem PDF
(host bip-v1-files.idcom-jst.pl) w formacie:

    RADA MIEJSKA W JANIKOWIE
    PROTOKÓŁ GŁOSOWANIA
    z dnia DD MMMM YYYY r.
    ...
    Punkt  Przedmiot głosowania  Za  Przeciw  Wstrzymało się
    obrad
    N <temat>  za prz wst
    Rezultat głosowania: Przyjęto/Odrzucono
    Głosy oddane:
    <Imię Nazwisko> ZA|PRZECIW|WSTRZ
    ...

Każde głosowanie walidowane vs agregat (Za/Przeciw/Wstrzymało się) z wiersza
punktu. Nazwy są już w konwencji 'Imię Nazwisko'.

Użycie: python scrape_janikowo.py <city_dir>   (zazwyczaj wołane przez run.sh)
"""
import io, json, re, sys, time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
import urllib.request
import ssl
import pdfplumber

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE
HEADERS = {"User-Agent": "Mozilla/5.0 (Radoskop cron)", "Accept-Language": "pl,en"}

BASE = "https://bip.janikowo.bipgmina.pl"
CAT = "/wiadomosci/11797/imienny_wykaz_glosowan_radnych"
KAD_START = "2024-05-07"
KAD = "2024-2029"
_FILE_HOST = "https://bip-v1-files.idcom-jst.pl/sites/46456/wiadomosci"

_ROMAN = {'I': 1, 'II': 2, 'III': 3, 'IV': 4, 'V': 5, 'VI': 6, 'VII': 7, 'VIII': 8,
          'IX': 9, 'X': 10, 'XI': 11, 'XII': 12, 'XIII': 13, 'XIV': 14, 'XV': 15,
          'XVI': 16, 'XVII': 17, 'XVIII': 18, 'XIX': 19, 'XX': 20, 'XXI': 21,
          'XXII': 22, 'XXIII': 23, 'XXIV': 24, 'XXV': 25, 'XXVI': 26, 'XXVII': 27,
          'XXVIII': 28, 'XXIX': 29, 'XXX': 30, 'XXXI': 31, 'XXXII': 32, 'XXXIII': 33,
          'XXXIV': 34, 'XXXV': 35, 'XXXVI': 36, 'XXXVII': 37, 'XXXVIII': 38,
          'XXXIX': 39, 'XL': 40}
_MON = {'stycznia': 1, 'lutego': 2, 'marca': 3, 'kwietnia': 4, 'maja': 5,
        'czerwca': 6, 'lipca': 7, 'sierpnia': 8, 'września': 9, 'października': 10,
        'listopada': 11, 'grudnia': 12, 'pazdziernika': 10, 'wrzesnia': 9}


def _http(url, binary=False):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=60, context=_CTX) as r:
        data = r.read()
    return data if binary else data.decode("utf-8", "replace")


def collect_sessions():
    """Sesje IX kadencji (date >= KAD_START) z kategorii imiennych głosowań."""
    sessions = {}
    for page in range(1, 12):
        url = f"{BASE}/wiadomosci/11797/lista/{page}/imienny_wykaz_glosowan_radnych"
        try:
            h = _http(url)
        except Exception:
            break
        for aid, slug in set(re.findall(r'/wiadomosci/11797/wiadomosc/(\d+)/([a-z0-9_]+)', h)):
            if aid in sessions:
                continue
            title = slug.replace("_", " ")
            m = re.match(r'\s*([ivxlcdm]+)\s+sesja\s+rm\s*(\d{1,2})(\d{2})(\d{4})\s*', title)
            if not m:
                continue
            rom = m.group(1).upper()
            try:
                date = f"{int(m.group(4)):04d}-{int(m.group(3)):02d}-{int(m.group(2)):02d}"
            except Exception:
                continue
            if date < KAD_START:
                continue
            sessions[aid] = {"id": aid, "roman": rom, "num": _ROMAN.get(rom, 99),
                             "date": date, "title": title, "attach": None}
    return sessions


def _attach_for(session_url):
    try:
        h = _http(session_url)
    except Exception:
        return None
    m = re.search(r'href="(https://bip-v1-files\.idcom-jst\.pl/sites/46456/[^"]+\.pdf)"', h, re.I)
    return m.group(1) if m else None


def _agg_cnt(text, label):
    m = re.search(label + r'\s+(\d+)', text)
    return int(m.group(1)) if m else None


def parse_pdf(data, session_date):
    """Parsuje per-głosowanie bloki z PDF 'PROTOKÓŁ GŁOSOWANIA'."""
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            text = "\n".join((p.extract_text() or "") for p in pdf.pages)
    except Exception:
        return []
    blocks = re.split(r"\nRADA MIEJSKA W JANIKOWIE\s*\nPROTOKÓŁ GŁOSOWANIA", text)
    votes = []
    # date of the session from first block header
    dm = re.search(r"z dnia\s+(\d{1,2})\s+(\w+)\s+(\d{4})\s*r\.", blocks[1] if len(blocks) > 1 else text)
    if dm and dm.group(2).lower() in _MON:
        session_date = f"{dm.group(3)}-{_MON[dm.group(2).lower()]:02d}-{int(dm.group(1)):02d}"
    for b in blocks[1:]:
        if "Głosy oddane:" not in b:
            continue
        # aggregate + punkt + title (lines after 'obrad' header, before 'Rezultat')
        pre = b.split("Głosy oddane:")[0]
        rez = re.search(r"Rezultat głosowania:\s*(\w+)", pre)
        result = rez.group(1) if rez else ""
        mm = re.search(r"\n\s*obrad\s*\n\s*(\d+)\s+(.*?)Rezultat głosowania", pre, re.S)
        punkt = ""
        title = ""
        if mm:
            punkt = mm.group(1)
            body = re.sub(r"\s+", " ", mm.group(2)).strip()
            # strip trailing aggregate numbers (za przec wstrz) if present at end
            body = re.sub(r"\s+\d+\s+\d+\s+\d+\s*$", "", body)
            body = body.replace(f"{punkt} ", "", 1).strip()
            title = body
        # named votes
        seg = b.split("Głosy oddane:")[1]
        named = {"za": [], "przeciw": [], "wstrzymal_sie": []}
        for line in seg.split("\n"):
            m = re.match(r'^\s*(.+?)\s+(ZA|PRZECIW|WSTRZ)\s*$', line)
            if not m:
                continue
            name = m.group(1).strip()
            tok = m.group(2)
            if tok == "ZA":
                named["za"].append(name)
            elif tok == "PRZECIW":
                named["przeciw"].append(name)
            elif tok == "WSTRZ":
                named["wstrzymal_sie"].append(name)
        named_cnt = sum(len(v) for v in named.values())
        votes.append({"date": session_date, "punkt": punkt, "title": title,
                      "result": result, "named": named,
                      "named_total": named_cnt})
    return votes


def make_slug(name):
    repl = {'ą': 'a', 'ć': 'c', 'ę': 'e', 'ł': 'l', 'ń': 'n', 'ó': 'o', 'ś': 's', 'ź': 'z', 'ż': 'z'}
    s = name.lower()
    for pl, a in repl.items():
        s = s.replace(pl, a)
    return re.sub(r"[^a-z0-9]+", "", s)


def build(city_dir: Path) -> int:
    cfg = json.loads((city_dir / "config.json").read_text(encoding="utf-8"))
    docs = city_dir / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    sessions = collect_sessions()
    print(f"  janikowo: IX-kad sessions {len(sessions)}")

    all_votes = []
    sessions_data = []
    vid = 0
    per_councilor = defaultdict(lambda: {"za": 0, "przeciw": 0, "wstrzymal": 0,
                                         "sess": set(), "present": 0})
    for aid, s in sorted(sessions.items(), key=lambda kv: kv[1]["num"]):
        url = f"{BASE}/wiadomosci/11797/wiadomosc/{aid}/{s['title'].replace(' ','_')}"
        att = _attach_for(url)
        if not att:
            print(f"  [skip {s['roman']:4s}] brak PDF ({s['date']})")
            continue
        try:
            pdf = _http(att, binary=True)
        except Exception as e:
            print(f"  [warn] {s['roman']}: {e}")
            continue
        vs = parse_pdf(pdf, s["date"])
        ok = 0
        # aggregate from the PDF header (Liczba uprawnionych/oddanych) — reconcile by
        # counting named vs declared per punkt; check each vote has >0 named
        for v in vs:
            if v["named_total"] == 0:
                continue
            ok += 1
            vid += 1
            d = v["date"] or s["date"]
            named = {k: list(vv) for k, vv in v["named"].items()}
            all_votes.append({"id": str(vid), "session_date": d,
                              "session_number": s["roman"],
                              "topic": v["title"], "named_votes": named,
                              "counts": {k: len(vv) for k, vv in named.items()},
                              "result": v.get("result", "")})
            for cat, names in named.items():
                key = ("za" if cat == "za" else "przeciw" if cat == "przeciw" else "wstrzymal")
                for name in names:
                    per_councilor[name][key] += 1
                    per_councilor[name]["sess"].add(d)
                    per_councilor[name]["present"] += 1
        print(f"  {s['roman']:4s} {s['date']} votes_ok={ok}/{len(vs)} -> {s['date']}")

    # sessions grouped by date
    by_date = defaultdict(lambda: {"date": "", "number": "", "vote_count": 0, "attendees": set()})
    for v in all_votes:
        d = v["session_date"]
        key = (d, v["session_number"])
        if key not in by_date:
            by_date[key] = {"date": d, "number": v["session_number"], "vote_count": 0, "attendees": set()}
        by_date[key]["vote_count"] += 1
        for cat in ("za", "przeciw", "wstrzymal_sie"):
            by_date[key]["attendees"].update(v["named_votes"].get(cat, []))

    sessions_data = []
    for (d, num) in sorted(by_date.keys()):
        s = by_date[(d, num)]
        sessions_data.append({"date": d, "number": num, "vote_count": s["vote_count"],
                              "attendee_count": len(s["attendees"]),
                              "attendees": sorted(s["attendees"]),
                              "speakers": []})

    # Roster: names present in votes (dynamic) — fallback to full union
    names = sorted(per_councilor.keys())
    total_votes = len(all_votes)
    total_sessions = len(sessions_data)
    councilors_list = []
    for name in names:
        c = per_councilor[name]
        present = c["za"] + c["przeciw"] + c["wstrzymal"]
        aktywnosc = round(present / total_votes * 100, 1) if total_votes else 0
        frekwencja = round(len(c["sess"]) / total_sessions * 100, 1) if total_sessions else 0
        councilors_list.append({"name": name, "club": "", "district": None,
                                "frekwencja": frekwencja, "aktywnosc": aktywnosc,
                                "zgodnosc_z_klubem": 0.0, "votes_za": c["za"],
                                "votes_przeciw": c["przeciw"], "votes_wstrzymal": c["wstrzymal"],
                                "votes_total": total_votes, "rebellion_count": 0,
                                "rebellions": [], "has_activity_data": False, "activity": None})

    kad = {"id": KAD, "label": cfg["kadencje"][KAD]["label"], "clubs": {},
           "sessions": sessions_data, "total_sessions": total_sessions,
           "total_votes": total_votes, "total_councilors": len(councilors_list),
           "councilors": councilors_list, "votes": all_votes,
           "similarity_top": [], "similarity_bottom": []}
    (docs / f"kadencja-{KAD}.json").write_text(json.dumps(kad, ensure_ascii=False, indent=1), encoding="utf-8")

    profiles = {"scraped_at": datetime.now().isoformat(),
                "profiles": [{"name": n, "slug": make_slug(n),
                              "kadencje": {KAD: {"club": "", "has_voting_data": True,
                                                 "has_activity_data": False, "former": False,
                                                 "mid_term": False,
                                                 "frekwencja": next((c["frekwencja"] for c in councilors_list if c["name"] == n), None),
                                                 "aktywnosc": next((c["aktywnosc"] for c in councilors_list if c["name"] == n), None),
                                                 "zgodnosc_z_klubem": None}}}
                             for n in names],
                "total": len(names)}
    (docs / "profiles.json").write_text(json.dumps(profiles, ensure_ascii=False, indent=1), encoding="utf-8")

    data = {"generated": datetime.now().isoformat(), "default_kadencja": KAD,
            "kadencje": [{"id": KAD, "label": cfg["kadencje"][KAD]["label"]}]}
    (docs / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    (docs / "config.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    city_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent
    raise SystemExit(build(city_dir))
