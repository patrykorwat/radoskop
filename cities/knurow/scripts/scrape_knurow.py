#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop Knurów — imienne głosowania Rady Miasta (protokoły PDF na BIP Szafr, knurow.bip.info.pl).

Źródło: BIP Urzędu Miasta Knurów (platforma Szafr Next.js, API /api/fo/articles).
Kategoria 'Protokoły z sesji Rady Miasta' (ścieżka rada-miasta-65k/kadencja-2024-2029-2130k/
sesje-2131k/protokoly-z-sesji-rady-miasta-2144k) per sesja: "Protokół Nr X/RRRR z [nadzwyczajnej]
sesji Rady Miasta Knurów w dniu DD.MM.RRRRr." z załącznikiem PDF (tekstowym).
Format bloku głosowania w protokole:
    Wyniki głosowania:
    ZA: n, PRZECIW: m, WSTRZYMUJĘ SIĘ: k, BRAK GŁOSU: b, NIEOBECNI: r
    Wyniki imienne:
    ZA (n) <nazwiska po przecinku, mogą być łamane wierszowo>
    PRZECIW (m) ...
    NIEOBECNI (r) ...
Walidacja: każdy głos reconcilowany vs agregat (listy nazwisk == liczniki); nazwiska
dopasowywane do globalnego rostera (2-pass: pass1 zbiera kandydatów, pass2 przypisuje).
Kluby: PENDING (nie fabrykujemy).
"""
import argparse, hashlib, json, re, time, unicodedata
from collections import defaultdict
from datetime import datetime
from pathlib import Path
import requests, urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE = "https://knurow.bip.info.pl"
API = BASE + "/api/fo"
VOTES_PATH = "rada-miasta-65k/kadencja-2024-2029-2130k/sesje-2131k/protokoly-z-sesji-rady-miasta-2144k"
KAD_START = "2024-05-07"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/120 Radoskop/1.0",
           "Accept": "application/vnd.api+json"}
REQ_DELAY = 0.3
_LAST = 0.0

def _rate():
    global _LAST
    now = time.time()
    if now - _LAST < REQ_DELAY:
        time.sleep(REQ_DELAY - (now - _LAST))
    _LAST = time.time()

def _get(url, cache=None, binary=False):
    if cache is not None:
        key = hashlib.md5(url.encode()).hexdigest()
        cf = Path(cache) / (key + (".bin" if binary else ".json"))
        if cf.is_file():
            return cf.read_bytes() if binary else json.loads(cf.read_text(encoding="utf-8"))
    _rate()
    resp = requests.get(url, headers=HEADERS, timeout=60, verify=False)
    resp.raise_for_status()
    if cache is not None:
        Path(cache).mkdir(parents=True, exist_ok=True)
        cf = Path(cache) / (hashlib.md5(url.encode()).hexdigest() + (".bin" if binary else ".json"))
        if binary:
            cf.write_bytes(resp.content)
        else:
            cf.write_text(resp.text, encoding="utf-8")
    return resp.content if binary else resp.json()

SESSION_TITLE_RE = re.compile(
    r'^Protokół Nr\s+(?P<num>\S+?)/(?P<yr>\d{4})\s+z\s+(?:nadzwyczajnej\s+)?sesji Rady Miasta Knurów '
    r'w dniu (?P<d>\d{1,2})\.(?P<m>\d{1,2})\.(?P<y>\d{4})', re.I)

def discover_sessions(cache):
    import urllib.parse
    f = urllib.parse.quote(json.dumps({"title": "Rady Miasta Knurów w dniu"}))
    found = {}
    page = 1
    while page <= 10:
        d = _get(API + "/articles?path=" + urllib.parse.quote(VOTES_PATH) + "&filter=" + f +
                 f"&count=100&page={page}", cache)
        for it in d["data"]:
            a = it["attributes"]
            m = SESSION_TITLE_RE.match(a["title"].strip())
            if not m:
                continue
            iso = f"{m.group('y')}-{int(m.group('m')):02d}-{int(m.group('d')):02d}"
            if iso < KAD_START:
                continue
            found[iso] = {"slug": a["slug"], "number": f"{m.group('num')}/{m.group('yr')}",
                          "title": a["title"].strip()}
        if page >= d["meta"]["pages"]:
            break
        page += 1
    return dict(sorted(found.items()))

def protocol_text(slug, cache):
    d = _get(f"{API}/articles/{slug}", cache)
    atts = d["data"]["attributes"].get("attachments") or []
    pdfs = [x for x in atts if (x["attributes"].get("extension") or "").lower() == "pdf"]
    if not pdfs:
        return None
    raw = _get(f"{API}/files/{pdfs[0]['id']}/download", cache, binary=True)
    import fitz
    doc = fitz.open(stream=raw, filetype="pdf")
    return "\n".join(p.get_text() for p in doc)

# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------
VOTE_RE = re.compile(
    r"Wyniki\s+g[łl]osowania:?\s*\n?\s*ZA:\s*(\d+)\s*,\s*PRZECIW:\s*(\d+)\s*,\s*"
    r"WSTRZYMUJ\w*\s+SIĘ:\s*(\d+)\s*,\s*BRAK\s+G[ŁL]OSU:\s*(\d+)\s*,\s*NIEOBECNI:\s*(\d+)\s*\n"
    r"Wyniki\s+imienne:?\s*\n(.*?)(?=(?:Wyniki\s+g[łl]osowania:?)|Pkt\s+\d+|\Z)",
    re.S)
_CAT_HDR = [("ZA", "za"), ("PRZECIW", "przeciw"), ("WSTRZYMUJĘ SIĘ", "wstrzymal_sie"),
            ("WSTRZYMUJE SIĘ", "wstrzymal_sie"), ("BRAK GŁOSU", "brak_glosu"),
            ("NIEOBECNI", "nieobecni")]
NAME_TOKEN_RE = re.compile(r'^[A-ZŁŚŻŹĆŃÓĄĘ][a-złśżźćńóąę]+(?:[-\s][A-ZŁŚŻŹĆŃÓĄĘa-złśżźćńóąę]+){1,3}\.?$')

def _norm(s):
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z]+", " ", s.lower().replace(".", "")).strip()

def _sections(named_text):
    """Zwraca listę (key, segment_text) z sekcji 'Wyniki imienne'."""
    positions = []
    seen = set()
    for pattern, key in _CAT_HDR:
        for m in re.finditer(re.escape(pattern) + r"\s*\(\s*\d+\s*\)", named_text):
            if key in seen:
                continue
            positions.append((m.start(), m.end(), key))
            seen.add(key)
            break
    positions.sort(key=lambda x: x[0])
    out = []
    for idx, (start, endd, key) in enumerate(positions):
        nend = positions[idx + 1][0] if idx + 1 < len(positions) else len(named_text)
        out.append((key, named_text[endd:nend]))
    return out

def extract_votes(text):
    """Pass-surowe: dla każdego bloku — zagregowane liczniki + tokeny nazwisk + topic-kontekst."""
    votes = []
    matches = list(VOTE_RE.finditer(text))
    prev_end = 0
    for i, m in enumerate(matches):
        za, pr, wz, brak, nieob = (int(m.group(k)) for k in range(1, 6))
        seg = m.group(6)
        # topic z kontekstu przed blokiem
        ctx = text[prev_end:m.start()]
        topic = ""
        um = list(re.finditer(r"Uchwała\s+nr\s+([\w/\.\-]+)\s+z dnia\s+[^.]*?w sprawie\s+(.+?)\.", ctx, re.S))
        wm = list(re.finditer(r"wniosek[^\n]*?o wprowadzenie do porządku obrad[^\n]*?w sprawie\s+(.+?)\.", ctx, re.S))
        pm = list(re.finditer(r"Pkt\s+\d+\s*\n(.+?)\s*\n", ctx, re.S))
        cands = []
        if um:
            cands.append((um[-1].start(), "Uchwała nr " + um[-1].group(1) + " w sprawie " + re.sub(r"\s+", " ", um[-1].group(2))))
        if wm:
            cands.append((wm[-1].start(), "Wniosek o wprowadzenie projektu uchwały w sprawie " + re.sub(r"\s+", " ", wm[-1].group(1))))
        if pm:
            cands.append((pm[-1].start(), re.sub(r"\s+", " ", pm[-1].group(1)).strip()))
        if cands:
            topic = max(cands, key=lambda x: x[0])[1]
        cats = {}
        for key, seg_text in _sections(seg):
            # odłamuś PDF-owe łamanie wyrazów: "Ma-\nrian" -> "Marian"
            fixed = re.sub(r"-\n(?=[a-złśżźćńóąę])", "", seg_text)
            toks = []
            buf = ""
            for tok in fixed.split(","):
                tok = re.sub(r"\s+", " ", tok).strip().rstrip(".")
                if not tok:
                    continue
                cand = (buf + " " + tok) if buf else tok
                if not NAME_TOKEN_RE.match(cand):
                    # sklejka z ogonem narracyjnym: "Paulina Żyrkowska. Zgodnie z regulaminem..."
                    head = re.split(r"\.\s", cand)[0].strip()
                    if head and NAME_TOKEN_RE.match(head):
                        toks.append(head)
                        buf = ""
                        continue
                if NAME_TOKEN_RE.match(cand):
                    toks.append(cand)
                    buf = ""
                elif re.match(r"^[A-ZŁŚŻŹĆŃÓĄĘ][\włśżźćńóąę-]+$", cand):
                    buf = cand  # imię/nazwisko złamane przecinkiem wierszowym — doklej następny
                else:
                    # ostatnia szansa: prefiks rostera zadecyduje później; nie dodajemy śmieci
                    buf = ""
            cats[key] = toks
        votes.append({"za_n": za, "przeciw_n": pr, "wstrz_n": wz, "brak_n": brak, "nieob_n": nieob,
                      "cats_raw": cats, "topic": topic})
        prev_end = m.end()
    return votes

def _match_roster(tok, roster_norm):
    tn = _norm(tok)
    if not tn:
        return None
    if tn in roster_norm:
        return roster_norm[tn]
    best, bestlen = None, -1
    for rn, full in roster_norm.items():
        if (rn.startswith(tn) or tn.startswith(rn)) and min(len(rn), len(tn)) >= 6 and len(rn) > bestlen:
            best, bestlen = full, len(rn)
    return best

def build_roster(all_votes):
    freq = defaultdict(int)
    for votes in all_votes:
        for v in votes:
            for key in ("za", "przeciw", "wstrzymal_sie", "nieobecni", "brak_glosu"):
                for t in v["cats_raw"].get(key, []):
                    freq[_norm(t)] += 1
    # kandydat: występuje w >=3 głosowaniach, nie jest duplikatem częściowym (dedyup po prefiksie)
    cands = [n for n, c in freq.items() if c >= 3 and len(n.split()) <= 3]
    # usuń frazy-nie-nazwiska (komisje, urzędy): każde słowo musi wyglądać na imienne
    STOP = {"ochrony","środowiska","finansów","porządku","spraw","socjalnych","społecznych",
            "gospodarki","edukacji","kultury","rewizyjnej","skarg","wniosków","petycji",
            "bezpieczeństwa","publicznego","sportu","turystyki","rekreacji","promocji","zdrowia","i"}
    def _is_name(n):
        return not any(w in STOP for w in n.split())
    cands = [n for n in cands if _is_name(n)]
    cands.sort(key=lambda n: -len(n))
    kept = []
    for n in cands:
        if any(k.startswith(n) or n.startswith(k) for k in kept):
            continue
        kept.append(n)
    # zwróć mapę norm -> najlepiej zapisana forma (najczęstszy token surowy)
    best_form = {}
    for votes in all_votes:
        for v in votes:
            for key in ("za", "przeciw", "wstrzymal_sie", "nieobecni", "brak_glosu"):
                for t in v["cats_raw"].get(key, []):
                    if _norm(t) in kept:
                        prev = best_form.get(_norm(t))
                        if prev is None or len(t) > len(prev):
                            best_form[_norm(t)] = t
    return {n: best_form[n] for n in kept if n in best_form}

def finalize_votes(votes, roster_norm):
    out = []
    for v in votes:
        named = {"za": [], "przeciw": [], "wstrzymal_sie": []}
        ok = True
        for key, want in (("za", v["za_n"]), ("przeciw", v["przeciw_n"]), ("wstrzymal_sie", v["wstrz_n"])):
            got = []
            seen = set()
            for t in v["cats_raw"].get(key, []):
                full = _match_roster(t, roster_norm)
                if full and full not in seen:
                    seen.add(full)
                    got.append(full)
            if len(got) != want:
                ok = False
            named[key] = got
        if not ok:
            continue
        out.append({"topic": v["topic"], "counts": {"za": v["za_n"], "przeciw": v["przeciw_n"],
                    "wstrzymal_sie": v["wstrz_n"]}, "named": named,
                    "nieobecni": [_match_roster(t, roster_norm) or t for t in v["cats_raw"].get("nieobecni", [])]})
    return out

# ---------------------------------------------------------------------------
# Output builders (wzorzec: scrape_bystrzyca_klodzka.py)
# ---------------------------------------------------------------------------
def make_slug(name):
    repl = {'ą':'a','ć':'c','ę':'e','ł':'l','ń':'n','ó':'o','ś':'s','ź':'z','ż':'z'}
    slug = name.lower()
    for pl, a in repl.items():
        slug = slug.replace(pl, a)
    return re.sub(r"[^a-z0-9]+", "", slug)

def build_output(records, sessions_meta):
    all_votes = []
    sessions_data = []
    vid = 0
    for d, sess in sessions_meta.items():
        votes = records.get(d, [])
        if not votes:
            continue
        vid += 0
        att = set()
        for v in votes:
            for c in ("za", "przeciw", "wstrzymal_sie"):
                att.update(v["named"][c])
        sessions_data.append({"date": d, "number": sess["number"], "vote_count": len(votes),
                              "attendee_count": len(att), "attendees": sorted(att), "speakers": []})
        for v in votes:
            vid += 1
            all_votes.append({"id": str(vid), "session_date": d, "session_number": sess["number"],
                              "topic": v["topic"], "named_votes": v["named"], "counts": v["counts"]})
    all_names = set()
    for v in all_votes:
        for names in v["named_votes"].values():
            all_names.update(names)
    councilors = {n: {"name": n, "club": "", "district": None, "votes_za": 0, "votes_przeciw": 0,
                      "votes_wstrzymal": 0, "votes_brak": 0, "votes_nieobecny": 0, "rebellions": []}
                  for n in all_names}
    councillor_sess = defaultdict(set)
    for v in all_votes:
        for cat, names in v["named_votes"].items():
            attr = {"za": "votes_za", "przeciw": "votes_przeciw", "wstrzymal_sie": "votes_wstrzymal"}.get(cat)
            for nm in names:
                if nm in councilors and attr:
                    councilors[nm][attr] += 1
                councillor_sess[nm].add(v["session_date"])
    total_votes = len(all_votes)
    total_sessions = len(sessions_data)
    councilors_list = []
    for c in sorted(councilors.values(), key=lambda x: x["name"]):
        present = c["votes_za"] + c["votes_przeciw"] + c["votes_wstrzymal"]
        aktywnosc = (present / total_votes * 100) if total_votes else 0
        frekwencja = (len(councillor_sess.get(c["name"], set())) / total_sessions * 100) if total_sessions else 0
        councilors_list.append({"name": c["name"], "club": c["club"], "district": None,
            "frekwencja": round(frekwencja, 1), "aktywnosc": round(aktywnosc, 1),
            "zgodnosc_z_klubem": 0.0, "votes_za": c["votes_za"], "votes_przeciw": c["votes_przeciw"],
            "votes_wstrzymal": c["votes_wstrzymal"], "votes_brak": c["votes_brak"],
            "votes_nieobecny": c["votes_nieobecny"], "votes_total": total_votes,
            "rebellion_count": 0, "rebellions": [], "has_activity_data": False, "activity": None})
    from itertools import combinations
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
        same = sum(1 for vidx in common if vectors[a][vidx] == vectors[b][vidx])
        pairs.append({"a": a, "b": b, "club_a": "", "club_b": "",
                      "score": round(same / len(common) * 100, 1), "common_votes": len(common)})
    pairs.sort(key=lambda x: x["score"], reverse=True)
    kad = {"id": KADENCJA_ID, "label": KADENCJA_LABEL, "clubs": {},
           "sessions": sessions_data, "total_sessions": total_sessions,
           "total_votes": total_votes, "total_councilors": len(councilors_list),
           "councilors": councilors_list, "votes": all_votes,
           "similarity_top": pairs[:20], "similarity_bottom": pairs[-20:][::-1]}
    return {"generated": datetime.now().isoformat(), "default_kadencja": KADENCJA_ID, "kadencje": [kad]}

def build_profiles(records):
    cv = defaultdict(lambda: {"za": 0, "przeciw": 0, "wstrzymal_sie": 0, "votes": []})
    sess = set()
    total_votes = 0
    for d, votes in records.items():
        sess.add(d)
        for v in votes:
            total_votes += 1
            for cat, names in v["named"].items():
                for nm in names:
                    cv[nm][cat] += 1
                    cv[nm]["votes"].append({"session": d, "vote": cat})
    profiles = []
    n_sessions = len(sess) or 1
    for nm in sorted(cv.keys()):
        vd = cv[nm]
        total = sum(vd[k] for k in ("za", "przeciw", "wstrzymal_sie")) or 1
        sess_n = len({v["session"] for v in vd["votes"]})
        aktywn = (vd["za"] + vd["przeciw"] + vd["wstrzymal_sie"]) / max(1, total_votes) * 100
        profiles.append({"name": nm, "slug": make_slug(nm),
                         "kadencje": {KADENCJA_ID: {"club": "", "has_voting_data": True,
                             "has_activity_data": False,
                             "frekwencja": round(sess_n / n_sessions * 100, 1),
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
                                    "kadencje": stubs}, ensure_ascii=False, separators=(",", ":")),
                        encoding="utf-8")
    (out_path.parent / "profiles.json").write_text(
        json.dumps(profiles, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--city-dir", required=True)
    ap.add_argument("--cache-dir", default=None)
    args = ap.parse_args()
    city_dir = Path(args.city_dir)
    cache = Path(args.cache_dir) if args.cache_dir else None

    sessions = discover_sessions(cache)
    print(f"[knurow] sesje IX kad: {len(sessions)}")
    texts = {}
    for d, s in sessions.items():
        t = protocol_text(s["slug"], cache)
        if not t:
            print(f"  [{d}] brak PDF")
            continue
        if "Wyniki imienne" not in t:
            print(f"  [{d}] brak Wyniki imienne (len {len(t)})")
            continue
        texts[d] = t
    raw_votes = {d: extract_votes(t) for d, t in texts.items()}
    roster = build_roster(raw_votes.values())
    print(f"  roster ({len(roster)}): {sorted(roster.values())}")
    roster_norm = {_norm(v): v for v in roster.values()}
    records = {}
    n_ok = n_all = 0
    for d in sorted(raw_votes):
        fin = finalize_votes(raw_votes[d], roster_norm)
        records[d] = fin
        n_ok += len(fin)
        n_all += len(raw_votes[d])
        print(f"  {d}: votes ok {len(fin)}/{len(raw_votes[d])}")
    print(f"  reconciliacja: {n_ok}/{n_all}")
    output = build_output(records, sessions)
    profiles = build_profiles(records)
    save_split(output, city_dir / "docs" / "data.json", profiles)
    k = output["kadencje"][0]
    print(f"[knurow] TOTAL votes={k['total_votes']} sessions={k['total_sessions']} councilors={k['total_councilors']}")

if __name__ == "__main__":
    main()
