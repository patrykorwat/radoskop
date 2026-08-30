#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop Sępólno Krajeńskie — imienne głosowania Rady Miejskiej (IX kadencja 2024-2029).

Źródło: BIP bip.gmina-sepolno.pl (platforma idcom/"Waw", kategoria
'Imienny wykaz głosowań' 651/327). Każda sesja IX kadencji = artykuł w tej
kategorii z załącznikiem PDF 'Imienny wykaz głosowań radnych RM podczas obrad
<XX[n]> sesji Rady Miejskiej w Sępólnie Krajeńskim' (format eSesja PRINT,
tekstowy, bez OCR):

    <XXVII Sesja Rady Miejskiej w Sępólnie Krajeńskim>
    GŁOSOWANIE
    <n>. <temat uchwały/wniosku>.
    TYP GŁOSOWANIA Jawne   DATA GŁOSOWANIA DD.MM.YYYY HH:MM
    LICZBA UPRAWNIONYCH 15 GŁOSY ZA 13
    LICZBA OBECNYCH 13    GŁOSY PRZECIW o
    LICZBA NIEOBECNYCH 2  GŁOSY WSTRZYMUJĄCE SIĘ o
    GŁOSY NIEODDANE o
    UPRAWNIENI DO GŁOSOWANIA
    LP NAZWISKO | IMIĘ GŁOS    LP NAZWISKO | IMIĘ GŁOS
    1 Bukolt Jadwiga za        9 Miczko-Gierakowska Anna za
    ...

Każde głosowanie walidowane vs agregat GŁOSY ZA/PRZECIW/WSTRZYMUJĄCE SIĘ +
LICZBA NIEOBECNYCH: liczba wierszy w tabeli imiennej == suma. Radni z listy
'UPRAWNIENI DO GŁOSOWANIA' mają głos 'za/przeciw/wstrzymał(a) się/nieobecn(a)y'.

Nazwy w PDF w kolejności 'Nazwisko Imię' — odwracane do konwencji Radoskopa.
Sesja = artykuł; data i numer sesji z tytułu artykułu ('XXVII sesja ... - 27 maja 2026 r.').
Pokrycie: sesje I..XXVII IX kadencji (25 sesji z wykazem głosowań; IV i XVII bez
osobnego wykazu PDF). 15 radnych.

Użycie: python scrape_sepolno.py --output docs/data.json --profiles docs/profiles.json [--cache-dir .cache]
"""
import argparse, hashlib, io, json, re, sys, time
from collections import Counter, defaultdict
from datetime import datetime
from itertools import combinations
from pathlib import Path
import requests, urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
import pdfplumber

BASE = "https://bip.gmina-sepolno.pl"
CAT = "/651/327/imienny-wykaz-glosowan.html"
KADENCJA_FILTER = "294"          # wartość filtra "Kadencja: 2024-2029"
KAD_START = "2024-05-07"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/120 Radoskop/1.0",
           "Accept-Language": "pl,en"}
REQ_DELAY = 0.4
_LAST = 0.0

_MON = {'stycznia': 1, 'lutego': 2, 'marca': 3, 'kwietnia': 4, 'maja': 5,
        'czerwca': 6, 'lipca': 7, 'sierpnia': 8, 'września': 9, 'października': 10,
        'listopada': 11, 'grudnia': 12, 'pazdziernika': 10, 'wrzesnia': 9}

_ROMAN = {'I': 1, 'II': 2, 'III': 3, 'IV': 4, 'V': 5, 'VI': 6, 'VII': 7, 'VIII': 8,
          'IX': 9, 'X': 10, 'XI': 11, 'XII': 12, 'XIII': 13, 'XIV': 14, 'XV': 15,
          'XVI': 16, 'XVII': 17, 'XVIII': 18, 'XIX': 19, 'XX': 20, 'XXI': 21,
          'XXII': 22, 'XXIII': 23, 'XXIV': 24, 'XXV': 25, 'XXVI': 26, 'XXVII': 27,
          'XXVIII': 28, 'XXIX': 29, 'XXX': 30, 'XXXI': 31}

_VOTE_TOKENS = (r"za|przeciw|wstrzymał\s+się|wstrzymała\s+się|wstrzymali\s+się|"
                r"wstrzymal\s+sie|wstrzymala\s+sie|wstrzymujący\s+się|wstrzymująca\s+się|"
                r"nie\s+głosował|nie\s+glosowal|nie\s+głosowała|nie\s+glosowala|"
                r"nie\s+wziął\s+udziału|nie\s+wzial\s+udzialu|nie\s+wzięła\s+udziału|"
                r"nie\s+wziela\s+udzialu|nieobecn\w*|bez\s+głosu|nie\s+oddan\w*"
                r"|nieoddan\w*|brak\s+głosu")
# LP bywa wyciągane przez pdfplumber jako LITERA (np. '9' -> 'g'); dlatego LP jest
# opcjonalne i dopuszcza 1-2 znaki alfanumeryczne (cyfra albo litera).
_PAIR_RE = re.compile(
    r"(?:(?:[0-9]{1,3}|[A-Za-z]{1,2})\s+)?([A-ZĄĆĘŁŃÓŚŹŻ][A-Za-zĄĆĘŁŃÓŚŹŻŻa-ząćęłńóśźż.\- ]*?)\s+(" + _VOTE_TOKENS + r")\b")


def _rate():
    global _LAST
    now = time.time()
    if now - _LAST < REQ_DELAY:
        time.sleep(REQ_DELAY - (now - _LAST))
    _LAST = time.time()


def fetch(url, cache_dir=None, binary=False):
    if cache_dir is not None:
        key = hashlib.md5(url.encode()).hexdigest()
        cf = Path(cache_dir) / (key + (".bin" if binary else ".html"))
        if cf.is_file():
            return cf.read_bytes() if binary else cf.read_text(encoding="utf-8", errors="ignore")
    _rate()
    last = None
    for i in range(5):
        try:
            r = requests.get(url, headers=HEADERS, timeout=60, verify=False)
            if r.status_code == 200:
                out = r.content if binary else r.text
                if cache_dir is not None:
                    Path(cache_dir).mkdir(parents=True, exist_ok=True)
                    cf = Path(cache_dir) / (hashlib.md5(url.encode()).hexdigest() + (".bin" if binary else ".html"))
                    if binary:
                        cf.write_bytes(out)
                    else:
                        cf.write_text(r.text, encoding="utf-8", errors="ignore")
                return out
            last = f"HTTP {r.status_code}"
        except Exception as e:  # noqa: BLE001
            last = str(e)
        time.sleep(1 + i)
    raise RuntimeError(f"fetch fail {url}: {last}")


def _session_meta_from_title(title):
    """'XXVII sesja Rady Miejskiej w Sępólnie Krajeńskim - 27 maja 2026 r.'
    -> (roman, 'YYYY-MM-DD')."""
    m = re.search(r"([IVXLCDM]+)\s+sesja", title, re.I)
    roman = m.group(1).upper() if m else ""
    d = re.search(r"(\d{1,2})\s+(\w+)\s+(\d{4})", title)
    date = ""
    if d and d.group(2).lower() in _MON:
        date = f"{d.group(3)}-{_MON[d.group(2).lower()]:02d}-{int(d.group(1)):02d}"
    return roman, date


def collect_sessions(cache_dir=None):
    """Strona-artykuły w kategorii Imienny wykaz głosowań dla kadencji 294 (2024-2029)."""
    sessions = {}
    page = 1
    while True:
        url = (BASE + CAT + f"?Page={page}&cct-search=&t60_f252_from=&t60_f252_to="
               f"&t60_f255={KADENCJA_FILTER}&sort_field=create_date&sort_direction=DESC&is_content_type_search=1")
        h = fetch(url, cache_dir)
        found = 0
        for m in re.finditer(r'href="(' + re.escape(BASE) + r'/(\d+)/([^"]*[Ss]esja[^"]*?)\.html)"', h):
            full, sid, slug = m.group(1), m.group(2), m.group(3)
            if sid in sessions:
                continue
            title = re.sub(r"[_-]+", " ", slug).strip()
            roman, date = _session_meta_from_title(title)
            sessions[sid] = {"id": sid, "roman": roman, "date": date, "title": title,
                             "attach": None, "url": full}
            found += 1
        # następna strona istnieje?
        if f"paginationButton_{page + 1}" not in h and "button next" not in h:
            break
        page += 1
        if page > 40:
            break
        time.sleep(0.25)
    return sessions


def _attach_for_session(url, cache_dir=None):
    h = fetch(url, cache_dir)
    m = re.search(r'href="([^"]*download/attachment/[^"]+\.pdf[^"]*)"', h, re.I)
    if not m:
        # spróbuj pierwszego linku z klasą fileLink (załącznik)
        m = re.search(r'<a[^>]+class="fileLink"[^>]+href="([^"]+)"', h)
    return m.group(1) if m else None


def collect_sessions_with_attach(cache_dir=None):
    sessions_map = collect_sessions(cache_dir)
    out = {}
    for sid, s in sessions_map.items():
        att = _attach_for_session(s["url"], cache_dir)
        if att:
            s["attach"] = att if att.startswith("http") else BASE + att
            out[sid] = s
    return out


def _agg_num(pattern, text):
    """Wyciąga liczbę z agregatu; PDF renderuje zerówkę jako literę 'o'."""
    m = re.search(pattern, text)
    if not m:
        return None
    v = m.group(1).strip().replace("o", "0").replace("O", "0")
    return int(v) if v.isdigit() else None


def parse_pdf(data):
    """Parsuje per-głosowanie bloki z PDF eSesja PRINT."""
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            text = "\n".join((p.extract_text() or "") for p in pdf.pages)
    except Exception:
        return []
    blocks = re.split(r"\nGŁOSOWANIE\s*\n|\nGLOSOWANIE\s*\n", text)
    votes = []
    for b in blocks[1:]:
        dm = re.search(r"DATA GŁOSOWANIA\s+([\d.\- :]+)", b, re.I)
        date = dm.group(1).strip().split(" ")[0] if dm else ""
        date = date.replace(".", "-")
        try:
            y, m, d = date.split("-")
            date = f"{int(y):04d}-{int(m):02d}-{int(d):02d}"
        except Exception:
            date = ""
        za = _agg_num(r"GŁOSY ZA\s+([\dOo]+)", b)
        prz = _agg_num(r"GŁOSY PRZECIW\s+([\dOo]+)", b)
        wst = _agg_num(r"GŁOSY WSTRZYMUJ\w*\s+SIĘ\s+([\dOo]+)", b)
        if wst is None:
            wst = _agg_num(r"GŁOSY WSTRZYMUJ\w*\s+SIE\s+([\dOo]+)", b)
        nie = _agg_num(r"LICZBA NIEOBECNYCH\s+([\dOo]+)", b)
        no = _agg_num(r"GŁOSY NIEODDANE\s+([\dOo]+)", b)
        t = re.split(r"\nTYP GŁOSOWANIA", b)[0]
        title = " ".join(x.strip() for x in t.split("\n") if x.strip())
        region = b.split("UPRAWNIENI DO GŁOSOWANIA", 1)[1] if "UPRAWNIENI DO GŁOSOWANIA" in b else b
        named = {"za": [], "przeciw": [], "wstrzymal_sie": [], "nieobecni": [], "brak_glosu": []}
        for name, tok in _PAIR_RE.findall(region):
            name = _canonical(name)
            tok = re.sub(r"\s+", " ", tok).lower().replace("ł", "l")
            if tok == "za":
                named["za"].append(name)
            elif tok.startswith("przeciw"):
                named["przeciw"].append(name)
            elif "wstrzym" in tok:
                named["wstrzymal_sie"].append(name)
            elif "nieobecn" in tok or "udzialu" in tok or "glosowal" in tok:
                named["nieobecni"].append(name)
            else:
                named["brak_glosu"].append(name)
        agg = {"za": za, "przeciw": prz, "wstrzym": wst,
               "nieobecni": nie, "nieoddane": no}
        votes.append({"date": date, "title": title, "agg": agg, "named": named})
    return votes


def _canonical(raw):
    raw = raw.strip()
    parts = raw.split()
    if len(parts) >= 2:
        # PDF: 'Nazwisko Imię' -> Radoskop: 'Imię Nazwisko'
        return (" ".join(parts[1:]) + " " + parts[0]).strip()
    return raw


def make_slug(name):
    repl = {'ą': 'a', 'ć': 'c', 'ę': 'e', 'ł': 'l', 'ń': 'n', 'ó': 'o', 'ś': 's', 'ź': 'z', 'ż': 'z'}
    s = name.lower()
    for pl, a in repl.items():
        s = s.replace(pl, a)
    return re.sub(r"[^a-z0-9]+", "", s)


def _norm(word):
    import unicodedata
    n = unicodedata.normalize("NFKD", word.lower())
    n = "".join(c for c in n if not unicodedata.combining(c))
    return n


def canonicalize_councilors(records):
    """Ujednolica warianty OCR nazwisk do kanonicznych radnych.

    eSesja PRINT bywa wyciągane z szumem (literówki, urwane nazwiska, '9'->'g').
    Roster = pełne nazwiska (>=2 słów) występujące >=10 razy; do niego mapujemy
    warianty przez odległość edycyjną (sim>=0.82). Jednowyrazowe fragmenty mapujemy
    tylko gdy jednoznaczne (1 radny o danym imieniu), inaczej zostają (bezpiecznie).
    """
    from difflib import SequenceMatcher
    from collections import Counter
    freq = Counter()
    for rec in records:
        for names in rec["named"].values():
            for nm in names:
                freq[nm] += 1
    roster = sorted([nm for nm, c in freq.items()
                     if c >= 10 and len(nm.split()) >= 2],
                    key=lambda x: -freq[x])
    # pierwsze słowo -> radni, jeśli jednoznaczne
    first_name_map = {}
    for nm in roster:
        fn = nm.split()[0].lower()
        first_name_map.setdefault(fn, []).append(nm)

    def canon(nm):
        for r in roster:
            if r == nm:
                return r
        # mecz pełnego nazwiska przez odległość
        best, best_sim = None, 0.0
        for r in roster:
            sim = SequenceMatcher(None, _norm(nm), _norm(r)).ratio()
            if sim > best_sim:
                best, best_sim = r, sim
        if best and best_sim >= 0.82:
            return best
        # dopasowanie po PIERWSZYM IMIENIU (urwane nazwisko / literówki)
        words = nm.strip().split()
        if words:
            cands = first_name_map.get(words[0].lower(), [])
            if len(cands) == 1:
                return cands[0]
            elif len(cands) > 1:
                # m.in. dwaj 'Robert' — wybierz najczęstszego
                return max(cands, key=lambda r: freq[r])
            # imię lekko zniekształcone ('Arm'->'Artur', 'Jwiqa'->'Jadwiga')
            bestf, bestfsim = None, 0.0
            for r in roster:
                fn = r.split()[0]
                s = SequenceMatcher(None, _norm(words[0]), _norm(fn)).ratio()
                if s > bestfsim:
                    bestf, bestfsim = r, s
            if bestf and bestfsim >= 0.6:
                return bestf
        if freq[nm] < 5:
            # szum OCR — usuń z głosowania zamiast tworzyć widmowego radnego
            return None
        return nm  # nie do mapowania (bezpieczny leftover)

    for rec in records:
        for key in list(rec["named"].keys()):
            mapped = [canon(x) for x in rec["named"][key]]
            # radny głosuje raz w głosowaniu — usuń duplikaty po kanonizacji
            seen = set()
            out = []
            for x in mapped:
                if x is None or x in seen:
                    continue
                seen.add(x)
                out.append(x)
            rec["named"][key] = out
    return records


def collect_all(sessions, cache_dir=None):
    records = []
    for sid, s in sorted(sessions.items(), key=lambda kv: _ROMAN.get(kv[1]["roman"], 99)):
        if not s["attach"]:
            print(f"  [skip {s['roman']:5s}] brak załącznika PDF")
            continue
        try:
            pdf = fetch(s["attach"], cache_dir, binary=True)
        except Exception as e:
            print(f"  [warn] {s['roman']}: {e}")
            continue
        vs = parse_pdf(pdf)
        ok = 0
        for v in vs:
            total_named = (len(v["named"]["za"]) + len(v["named"]["przeciw"]) +
                           len(v["named"]["wstrzymal_sie"]) + len(v["named"]["nieobecni"]) +
                           len(v["named"]["brak_glosu"]))
            agg_total = 0
            for k in ("za", "przeciw", "wstrzym", "nieobecni", "nieoddane"):
                if v["agg"].get(k) is not None:
                    agg_total += v["agg"][k]
            if total_named == agg_total and total_named > 0:
                ok += 1
                rec = dict(v)
                rec["session_date"] = s["date"]
                rec["session_num"] = s["roman"] or s["date"]
                records.append(rec)
        print(f"  {s['roman']:5s} {s['date']} votes_ok={ok}/{len(vs)}")
    return records


NAME_AGG = {}
_all_session_dates = []


def _club(name):
    return ""


def _compute_consensus(all_votes):
    stats = defaultdict(lambda: {"za": 0, "przeciw": 0, "wstrzymal": 0, "brak": 0,
                                 "nieobecny": 0, "sess": set(), "with": 0, "against": 0})
    for v in all_votes:
        for cat, names in v["named_votes"].items():
            for name in names:
                key = ("za" if cat == "za" else "przeciw" if cat == "przeciw"
                       else "wstrzymal" if cat == "wstrzymal_sie"
                       else "nieobecny" if cat == "nieobecni" else "brak")
                stats[name][key] += 1
                if key != "nieobecny":
                    stats[name]["sess"].add(v["session_date"])
    return stats


def build_output(records):
    all_votes, vid = [], 0
    sessions_by_date = {}
    for rec in records:
        d = rec.get("session_date")
        if not d or d < KAD_START:
            continue
        if d not in sessions_by_date:
            sessions_by_date[d] = {"date": d, "number": rec.get("session_num", ""),
                                   "vote_count": 0, "attendees": set()}
        named = {k: list(v) for k, v in rec["named"].items()}
        vid += 1
        sessions_by_date[d]["vote_count"] += 1
        for cat in ("za", "przeciw", "wstrzymal_sie", "nieobecni", "brak_glosu"):
            sessions_by_date[d]["attendees"].update(named.get(cat, []))
        all_votes.append({
            "id": str(vid), "session_date": d, "session_number": rec.get("session_num", ""),
            "topic": rec.get("title") or "", "named_votes": named,
            "counts": {k: len(named.get(k, [])) for k in ("za", "przeciw", "wstrzymal_sie")},
        })

    sessions_data = []
    for d in sorted(sessions_by_date.keys()):
        s = sessions_by_date[d]
        sessions_data.append({
            "date": d, "number": s["number"], "vote_count": s["vote_count"],
            "attendee_count": len(s["attendees"]), "attendees": sorted(s["attendees"]),
            "speakers": [],
        })

    all_names = set()
    for v in all_votes:
        for names in v["named_votes"].values():
            all_names.update(names)

    councilors = {n: {"name": n, "club": _club(n), "district": None, "votes_za": 0,
                      "votes_przeciw": 0, "votes_wstrzymal": 0, "votes_brak": 0,
                      "votes_nieobecny": 0, "rebellions": []} for n in all_names}
    for v in all_votes:
        for cat, names in v["named_votes"].items():
            for name in names:
                if name not in councilors:
                    continue
                c = councilors[name]
                if cat == "za":
                    c["votes_za"] += 1
                elif cat == "przeciw":
                    c["votes_przeciw"] += 1
                elif cat == "wstrzymal_sie":
                    c["votes_wstrzymal"] += 1
                elif cat == "nieobecni":
                    c["votes_nieobecny"] += 1
                else:
                    c["votes_brak"] += 1

    total_votes = len(all_votes)
    total_sessions = len(sessions_data)
    stats = _compute_consensus(all_votes)

    councilors_list = []
    for name in sorted(councilors.keys()):
        c = councilors[name]
        st = stats[name]
        present = c["votes_za"] + c["votes_przeciw"] + c["votes_wstrzymal"] + c["votes_brak"]
        aktywnosc = (present / total_votes * 100) if total_votes else 0
        frekwencja = (len(st["sess"]) / total_sessions * 100) if total_sessions else 0
        councilors_list.append({
            "name": name, "club": c["club"], "district": None,
            "frekwencja": round(frekwencja, 1), "aktywnosc": round(aktywnosc, 1),
            "zgodnosc_z_klubem": 0.0,
            "votes_za": c["votes_za"], "votes_przeciw": c["votes_przeciw"],
            "votes_wstrzymal": c["votes_wstrzymal"], "votes_brak": c["votes_brak"],
            "votes_nieobecny": c["votes_nieobecny"], "votes_total": total_votes,
            "rebellion_count": 0, "rebellions": [],
            "has_activity_data": False, "activity": None,
        })

    vectors = defaultdict(dict)
    for v in all_votes:
        for cat in ("za", "przeciw", "wstrzymal_sie"):
            for name in v["named_votes"].get(cat, []):
                vectors[name][v["id"]] = cat
    pairs = []
    names_sorted = sorted(vectors.keys())
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
    cv = defaultdict(lambda: {"za": 0, "przeciw": 0, "wstrzymal_sie": 0,
                              "nieobecny": 0, "brak": 0, "sess": set()})
    for rec in records:
        d = rec.get("session_date")
        if not d or d < KAD_START:
            continue
        for cat, names in rec["named"].items():
            for name in names:
                key = ("za" if cat == "za" else "przeciw" if cat == "przeciw"
                       else "wstrzymal_sie" if cat == "wstrzymal_sie"
                       else "nieobecny" if cat == "nieobecni" else "brak")
                cv[name][key] += 1
                if key != "nieobecny":
                    cv[name]["sess"].add(d)
    profiles = []
    n_sessions = len(set(r["session_date"] for r in records)) or 1
    for name in sorted(cv.keys()):
        vd = cv[name]
        total = sum(vd[k] for k in ("za", "przeciw", "wstrzymal_sie", "nieobecny", "brak")) or 1
        frekw = len(vd["sess"]) / n_sessions * 100
        profiles.append({
            "name": name, "slug": make_slug(name),
            "kadencje": {KADENCJA_ID: {
                "club": "", "has_voting_data": True, "has_activity_data": False,
                "frekwencja": round(frekw, 1),
                "aktywnosc": round((vd["za"] + vd["przeciw"] + vd["wstrzymal_sie"]) / total * 100, 1),
                "zgodnosc_z_klubem": 0.0,
                "votes_za": vd["za"], "votes_przeciw": vd["przeciw"],
                "votes_wstrzymal": vd["wstrzymal_sie"], "votes_brak": vd["brak"],
                "votes_nieobecny": vd["nieobecny"], "votes_total": total,
                "rebellion_count": 0, "rebellions": [], "roles": [], "notes": "",
                "former": False, "mid_term": False, "mid_term_pdf": False,
            }}
        })
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
    out_path.write_text(json.dumps(
        {"generated": output.get("generated", ""), "default_kadencja": output.get("default_kadencja", ""),
         "kadencje": stubs}, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    (out_path.parent / "profiles.json").write_text(
        json.dumps(profiles, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", required=True, help="docs/data.json")
    ap.add_argument("--profiles", required=True, help="docs/profiles.json")
    ap.add_argument("--cache-dir", default=None)
    args = ap.parse_args()
    cache_dir = Path(args.cache_dir) if args.cache_dir else None

    print("=== Scraper Rada Miejska w Sępólnie Krajeńskim (bip.gmina-sepolno.pl, idcom Waw) ===")
    sessions = collect_sessions_with_attach(cache_dir)
    print(f"  Sesji z załącznikiem PDF: {len(sessions)}")
    if not sessions:
        print("  BRAK SESJI."); return 1
    records = collect_all(sessions, cache_dir)
    print(f"  Razem zwalidowanych głosowań: {len(records)}")
    if not records:
        print("  BRAK DANYCH."); return 1
    canonicalize_councilors(records)
    output = build_output(records)
    profiles = build_profiles(records)
    save_split(output, args.output, profiles)
    k = output["kadencje"][0]
    print(f"  Sesji: {k['total_sessions']}, głosowań: {k['total_votes']}, radnych: {k['total_councilors']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
