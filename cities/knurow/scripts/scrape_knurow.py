#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Radoskop Knurów — imienne głosowania Rady Miasta Knurowa (IX kadencja).

Źródło: BIP knurow.bip.info.pl (platforma bip.info.pl, Angular SPA + JSON API).
API: GET /api/fo/search/getResult?page&count&contains=<q> (pełnotekstowa, wolna ~10-20s,
     cache dyskowy), GET /api/fo/articles/{slug} (JSON:API, atrybut attachments[] z id
     plików), GET /api/fo/files/{id}/download.
Kategoria 'Rada Miasta > Kadencja 2024-2029 > Sesje > Protokoły z sesji Rady Miasta':
per sesji artykuł "Protokół Nr <RZYMSKA>/<RRRR> z sesji Rady Miasta Knurowa w dniu
DD.MM.RRRR r." z załącznikiem PDF = protokół narracyjny zawierający dla KAŻDEGO
głosowania blok w formacie eSesja tekstowym (bez linii 'Głosowano w sprawie'):
    Wyniki głosowania:
    ZA: n, PRZECIW: n, WSTRZYMUJĘ SIĘ: n, BRAK GŁOSU: n, NIEOBECNI: n
    Wyniki imienne:
    ZA (n) Nazwisko Imię, ...
    PRZECIW (n) ... / NIEOBECNI (n) ...
Temat głosowania = najbliższe zdanie przed blokiem zawierające 'w sprawie' / 'wniosk' /
'uchwał' (protokół narracyjny; po blokach następuje narracja 'Przewodniczący...', 'Pkt',
'Głos zabrali' — te tokeny kończą sekcję imienną).
Walidacja: liczby nazwisk per kategoria == agregaty z linii ZA:/PRZECIW:.

eSesja knurow.esesja.pl = wildcard (brak BIP), AlfaTV brak, bip.net.pl brak.

Użycie:
    python scrape_knurow.py --city-dir <cities/knurow> [--cache-dir dir]
Zapisuje: docs/data.json, docs/kadencja-2024-2029.json, docs/profiles.json
"""
import argparse
import io
import json
import re
import ssl
import time
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime
from itertools import combinations
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen

BIP = "https://knurow.bip.info.pl"
SEARCH = BIP + "/api/fo/search/getResult"
KAD_START = "2024-05-07"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"
QUERY = "z sesji Rady Miasta Knurów w dniu"

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE
_UA = {"User-Agent": "Mozilla/5.0 (Radoskop; info@radoskop.eu)", "Accept": "application/json"}

REQ_DELAY = 0.5
_LAST = 0.0


def _rate():
    global _LAST
    now = time.time()
    d = now - _LAST
    if d < REQ_DELAY:
        time.sleep(REQ_DELAY - d)
    _LAST = time.time()


def _get(url, cache=None, binary=False, tries=4):
    import hashlib
    cf = None
    if cache is not None:
        cf = Path(cache) / (hashlib.md5(url.encode()).hexdigest() + (".bin" if binary else ".json"))
        if cf.is_file():
            return cf.read_bytes() if binary else cf.read_text(encoding="utf-8")
    last = None
    for att in range(tries):
        _rate()
        try:
            r = urlopen(Request(url, headers=_UA), timeout=120, context=CTX)
            data = r.read()
            if cf is not None:
                cf.parent.mkdir(parents=True, exist_ok=True)
                cf.write_bytes(data) if binary else cf.write_text(data.decode("utf-8", "replace"), encoding="utf-8")
            return data if binary else data.decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 + att * 4)
    raise RuntimeError(f"failed {url}: {last}")


def _json(u, cache):
    return json.loads(_get(u, cache))


# ---------------- discovery ----------------
def article_attachment_pdf(slug, cache):
    """Zwraca bajty PDF załącznika-protokołu artykułu (pierwszy załącznik PDF)."""
    d = _json(f"{BIP}/api/fo/articles/{quote(slug, safe='')}", cache)
    att = d.get("data", {}).get("attributes", {}).get("attachments") or []
    for a in att:
        aa = a.get("attributes", {})
        if (aa.get("extension") or "").lower() == "pdf":
            return _get(f"{BIP}/api/fo/files/{a['id']}/download", cache, binary=True)
    return None


# ---------------- parsing ----------------
try:
    import pdfplumber
except ImportError:  # pragma: no cover
    pdfplumber = None

_COUNTS_RE = re.compile(
    r"ZA:\s*(\d+),?\s*PRZECIW:\s*(\d+),?\s*WSTRZYMUJĘ SIĘ:\s*(\d+),?\s*"
    r"BRAK GŁOSU:\s*(\d+),?\s*NIEOBECNI:\s*(\d+)")
_LABEL_RE = re.compile(r"\b(ZA|PRZECIW|WSTRZYMUJĘ SIĘ|BRAK GŁOSU|NIEOBECNI)\s*[:,]?\s*\((\d+)\)")
_CAT_MAP = {"ZA": "za", "PRZECIW": "przeciw", "WSTRZYMUJĘ SIĘ": "wstrzymal_sie",
            "BRAK GŁOSU": "brak", "NIEOBECNI": "nieobecni"}
# tokeny narracji protokołu, które kończą sekcję imienną (nazwiska ich nie zawierają)
_CHUNK_CUTS = ("Przewodniczący", "Wiceprzewodniczący", "Pkt ", "Głos zabrali",
               "Sporządz", "Następne punkty", "Uchwała nr", "Uchwała Nr",
               "Pan ", "Pani ", "Prezydent Miasta", "Sekretarz", "Skarbnik")
_FOOTER_TOKENS = re.compile(
    r"(zakończono|wygenerowano|za\s*pomocą|app\.esesja\.pl|strona\s*\d+\s*z\s*\d+|"
    r"\d{1,2}:\d{2}(:\d{2})?|\|)", re.I)


_NARR_TAIL = re.compile(
    r"(\.\s|\u2022|Ad\.|Ad\s*\d|Przewodniczący|Wiceprzewodniczący|Radny |Radna |"
    r"Pan\s|Pani\s|Pkt\s|G[łl]os|W związku|Zgodnie|Przystąpiono|Ogłoszenia|Zakończen)")

def _clean_name(s):
    s = re.sub(r"\s+", " ", s.strip())
    if not s or not any(c.isalpha() for c in s):
        return None
    if _FOOTER_TOKENS.search(s):
        return None
    # odetnij wklejoną narrację protokołu (nazwisko nie zawiera '.' ani tokenów narracji)
    m = _NARR_TAIL.search(s)
    if m:
        s = s[:m.start()].strip(" ,.")
    # odcinaj wklejone nagłówki stron / numery stron
    s = re.sub(r"\s*\b\d{1,3}\b\s*$", "", s).strip(" .")
    if not s or not any(c.isalpha() for c in s):
        return None
    return s


def _find_topic(pre):
    """Temat = nagłówek 'Pkt N\\n<title>' jeśli głosowaniu bezpośrednio poprzedza blok Pkt
    z tytułem uchwały; inaczej najbliższe zdanie narracyjne z 'w sprawie' / 'wniosk'."""
    pre = re.sub(r"\s+", " ", pre).strip()
    if len(pre) > 4000:
        pre = pre[-4000:]
    # 1) ostatni 'Pkt N <tytuł>' — tytuł ciągnie się do 'Uchwała nr/Nr' lub 'Przewodniczący'
    pkts = list(re.finditer(r"Pkt\s*\d+\s+(.{15,}?)\s*(?=Uchwa[ał]a\s+nr|Uchwa[ał]a\s+Nr|Przewodniczący|Wiceprzewodniczący|Prezydent|G[łl]os zabrali|Zastępca|Sekretarz|Skarbnik|Pan\s|Pani\s|$)", pre))
    generic = ("odczytał stanowisko", "odczytała stanowisko", "głos zabrali",
               "przedstawi", "zwrócił się z wnioskiem", "zwróciła się z wnioskiem",
               "informuje", "poinformowa")
    pkt_title = None
    if pkts:
        cand = pkts[-1].group(1).strip(" .")
        # tytuł Pkt jest OK jeśli nie jest samą narracją i ma sens own
        if len(cand) >= 15 and not any(g in cand.lower()[:40] for g in generic[:4]):
            pkt_title = cand[:300]
    sents = re.split(r"(?<=\.)\s+", pre)
    sents = [s.strip() for s in sents if len(s.strip()) > 15]
    low_keys = ("w sprawie", "wniosk", "uchwał")
    nearest = None
    for s in reversed(sents[-6:]):
        if any(k in s.lower() for k in low_keys):
            nearest = s
            break
    if nearest is None and sents:
        nearest = sents[-1]
    nearest = re.sub(r"^Pkt\s*\d+\s*", "", nearest or "").strip(" .")[:300]
    if nearest and any(g in nearest.lower() for g in generic) and pkt_title:
        return pkt_title
    return nearest or pkt_title or "(glosowanie)"


def parse_protocol_pdf(data):
    """Zwraca listę rekordów {topic, counts, named} z tekstu PDF lub [] przy braku warstwy tekstowej."""
    if pdfplumber is None:
        raise RuntimeError("pdfplumber required")
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            text = "\n".join((p.extract_text() or "") for p in pdf.pages)
    except Exception:
        return []
    if "Wyniki imienne" not in text:
        return []
    records = []
    # bloki: od nagłówka 'Wyniki głosowania' do następnego nagłówka. Temat bierzemy z
    # narracji POPRZEDZAJĄCEJ nagłówek, więc indexesujemy w całym tekście.
    starts = [m.start() for m in re.finditer(r"Wyniki\s+g[łl]osowania", text)]
    for i, st in enumerate(starts):
        en = starts[i + 1] if i + 1 < len(starts) else len(text)
        blk = text[st:en]
        pre = text[max(0, st - 2500):st]
        if "Wyniki imienne" not in blk:
            continue
        cm = _COUNTS_RE.search(blk)
        if not cm:
            continue
        counts = {"za": int(cm.group(1)), "przeciw": int(cm.group(2)),
                  "wstrzymal_sie": int(cm.group(3)), "brak": int(cm.group(4)),
                  "nieobecni": int(cm.group(5))}
        topic = _find_topic(pre)
        wi = blk.find("Wyniki imienne")
        remainder = blk[wi:]
        labels = list(_LABEL_RE.finditer(remainder))
        if not labels:
            continue
        named = defaultdict(list)
        for i, m in enumerate(labels):
            cat = _CAT_MAP.get(m.group(1))
            if not cat:
                continue
            start = m.end()
            end = labels[i + 1].start() if i + 1 < len(labels) else len(remainder)
            chunk = remainder[start:end]
            for cut in _CHUNK_CUTS:
                idx = chunk.find(cut)
                if idx != -1:
                    chunk = chunk[:idx]
            chunk = re.sub(r"\s+", " ", chunk)
            names = [t for t in (_clean_name(x) for x in chunk.split(",")) if t]
            named[cat] = names
        rec = {"topic": topic, "counts": counts, "named": dict(named)}
        ok = all(len(rec["named"].get(c, [])) == n for c, n in counts.items())
        rec["ok"] = ok
        records.append(rec)
    return records


# ---------------- output (naklo/glogow pattern) ----------------
def make_slug(name):
    slug = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "", slug.lower())


def canon_name(name, canon):
    if name in canon:
        return canon[name]
    key = re.sub(r"[^a-zśłążźćęóńŚŁĄŻŹĆĘÓŃ]", "", name.lower())
    if key in canon:
        return canon[key]
    canon[key] = name
    canon[name] = name
    return name


def build_output(records, roster, club_assign=None):
    club_assign = club_assign or {}
    all_votes = []
    vid = 0
    sessions_by_date = {}
    for rec in records:
        d = rec["date"]
        if not d or d < KAD_START:
            continue
        if d not in sessions_by_date:
            sessions_by_date[d] = {"date": d, "number": rec.get("num", ""),
                                   "vote_count": 0, "attendees": set()}
        sessions_by_date[d]["vote_count"] += 1
        named = {k: list(v) for k, v in rec["named"].items()}
        for cat in ("za", "przeciw", "wstrzymal_sie", "brak"):
            sessions_by_date[d]["attendees"].update(rec["named"].get(cat, []))
        vid += 1
        all_votes.append({"id": str(vid), "session_date": d, "session_number": rec.get("num", ""),
                          "topic": rec.get("topic", ""), "named_votes": named,
                          "counts": {k: len(named.get(k, [])) for k in ("za", "przeciw", "wstrzymal_sie")}})
    sessions_data = []
    for d in sorted(sessions_by_date):
        s = sessions_by_date[d]
        sessions_data.append({"date": d, "number": s["number"], "vote_count": s["vote_count"],
                              "attendee_count": len(s["attendees"]),
                              "attendees": sorted(s["attendees"]), "speakers": []})
    councilors_data = {}
    for name in roster:
        councilors_data[name] = {"name": name, "club": club_assign.get(name, "NZ"),
                                 "district": None, "votes_za": 0, "votes_przeciw": 0,
                                 "votes_wstrzymal": 0, "votes_brak": 0,
                                 "votes_nieobecny": 0, "rebellions": []}
    for v in all_votes:
        for cat, names in v["named_votes"].items():
            for nm in names:
                c = councilors_data.get(nm)
                if not c:
                    continue
                key = {"za": "votes_za", "przeciw": "votes_przeciw",
                       "wstrzymal_sie": "votes_wstrzymal", "brak": "votes_brak",
                       "nieobecni": "votes_nieobecny"}.get(cat)
                if key:
                    c[key] += 1
    total_votes = len(all_votes)
    total_sessions = len(sessions_data)
    councillor_sess = defaultdict(set)
    for v in all_votes:
        for names in v["named_votes"].values():
            for nm in names:
                councillor_sess[nm].add(v["session_date"])
    councilors_list = []
    for c in sorted(councilors_data.values(), key=lambda x: x["name"]):
        present = c["votes_za"] + c["votes_przeciw"] + c["votes_wstrzymal"] + c["votes_brak"]
        aktywn = (present / total_votes * 100) if total_votes else 0
        frekw = (len(councillor_sess.get(c["name"], set())) / total_sessions * 100) if total_sessions else 0
        councilors_list.append({"name": c["name"], "club": c["club"], "district": None,
                                "frekwencja": round(frekw, 1), "aktywnosc": round(aktywn, 1),
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
    for a, b in combinations(sorted(vectors), 2):
        common = set(vectors[a]) & set(vectors[b])
        if len(common) < 10:
            continue
        same = sum(1 for vid_ in common if vectors[a][vid_] == vectors[b][vid_])
        pairs.append({"a": a, "b": b, "club_a": "", "club_b": "",
                      "score": round(same / len(common) * 100, 1), "common_votes": len(common)})
    pairs.sort(key=lambda x: x["score"], reverse=True)
    kad = {"id": KADENCJA_ID, "label": KADENCJA_LABEL,
           "clubs": dict(Counter(club_assign.get(c["name"], "NZ") for c in councilors_list)),
           "sessions": sessions_data, "total_sessions": total_sessions,
           "total_votes": total_votes, "total_councilors": len(councilors_list),
           "councilors": councilors_list, "votes": all_votes,
           "similarity_top": pairs[:20], "similarity_bottom": pairs[-20:][::-1]}
    return {"generated": datetime.now().isoformat(), "default_kadencja": KADENCJA_ID,
            "kadencje": [kad]}, total_votes, total_sessions


def build_profiles(records, roster, club_assign=None):
    club_assign = club_assign or {}
    cv = defaultdict(lambda: {"za": 0, "przeciw": 0, "wstrzymal_sie": 0, "brak": 0,
                              "nieobecni": 0, "sessions": set()})
    for rec in records:
        d = rec["date"]
        if not d or d < KAD_START:
            continue
        for cat, names in rec["named"].items():
            for nm in names:
                if cat in cv[nm]:
                    cv[nm][cat] += 1
                cv[nm]["sessions"].add(d)
    sess_set = {r["date"] for r in records if r["date"] and r["date"] >= KAD_START}
    n_sessions = len(sess_set) or 1
    profiles = []
    for nm in sorted(roster | set(cv)):
        vd = cv[nm]
        total = sum(vd[k] for k in ("za", "przeciw", "wstrzymal_sie", "brak")) or 0
        sess = len(vd["sessions"])
        aktywn = (vd["za"] + vd["przeciw"] + vd["wstrzymal_sie"]) / n_sessions * 100
        profiles.append({"name": nm, "slug": make_slug(nm),
                         "kadencje": {KADENCJA_ID: {
                             "club": club_assign.get(nm, "NZ"), "has_voting_data": True,
                             "has_activity_data": False,
                             "frekwencja": round(sess / n_sessions * 100, 1),
                             "aktywnosc": round(aktywn, 1), "zgodnosc_z_klubem": 0.0,
                             "votes_za": vd["za"], "votes_przeciw": vd["przeciw"],
                             "votes_wstrzymal": vd["wstrzymal_sie"], "votes_brak": vd["brak"],
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
    if cache:
        cache.mkdir(parents=True, exist_ok=True)
    cfg = {}
    if (city_dir / "config.json").is_file():
        cfg = json.loads((city_dir / "config.json").read_text(encoding="utf-8"))
    club_assign = cfg.get("club_assignments", {}) or {}

    arts = discover_slugs(cache)
    arts = [a for a in arts if a["date"] >= KAD_START]
    print(f"[knurow] {len(arts)} artykulow sesji IX kad.")

    canon = {}
    roster = set()
    records = []
    bad = 0
    for a in arts:
        try:
            data = article_attachment_pdf(a["slug"], cache)
            if not data:
                print(f"  [skip {a['date']}] brak PDF")
                continue
            recs = parse_protocol_pdf(data)
            good = 0
            for r in recs:
                if not r.pop("ok"):
                    bad += 1
                    continue
                r["named"] = {c: [canon_name(nm, canon) for nm in v] for c, v in r["named"].items()}
                r["date"] = a["date"]
                r["num"] = a["roman"]
                for names in r["named"].values():
                    roster.update(names)
                records.append(r)
                good += 1
            print(f"  [{'ok' if good else 'skip'}] {a['date']} {a['roman']:>5} votes={good}")
        except Exception as e:
            print(f"  [ERR {a['date']}] {type(e).__name__}: {e}")
    if bad:
        print(f"[knurow] WARNING {bad} głoseń bez reconciliacji (pominięte)")
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
    print(f"[knurow] ZAPISANO votes={total_votes} sessions={total_sessions} councilors={len(profiles['profiles'])} bad={bad}")


def discover_slugs(cache):
    """To samo co discover_articles, ale zwraca slug URL artykułu."""
    seen = {}
    page = 1
    while True:
        d = _json(f"{SEARCH}?page={page}&count=100&contains={quote(QUERY)}", cache)
        for x in d.get("data", []):
            if x.get("type") != "CmsArticle":
                continue
            a = x["attributes"]
            t = (a.get("title") or "").strip()
            bc = [b["attributes"]["title"] for b in a.get("additionalFields", {}).get("breadcrumbs", [])]
            if "Protokoły z sesji Rady Miasta" not in " | ".join(bc):
                continue
            if "sesji Rady Miasta" not in t:
                continue
            m = re.search(r"w dniu (\d{2})\.(\d{2})\.(\d{4})", t)
            if not m:
                continue
            date = f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
            if date >= KAD_START:
                slug = (a.get("url") or "").strip("/").split("/")[-1]
                rm = re.search(r"Nr\s+([IVXL]+)", t)
                seen[slug] = {"date": date, "slug": slug, "roman": rm.group(1) if rm else "",
                              "title": t}
        if page >= d["meta"].get("pages", 1):
            break
        page += 1
    return sorted(seen.values(), key=lambda r: r["date"])


if __name__ == "__main__":
    main()
