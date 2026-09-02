#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Radoskop Tomaszów Mazowiecki — AlfaTV "System Rada" (rada.tomaszow-maz.pl).

Zrodlo: http://rada.tomaszow-maz.pl
  /glosowania                    — lista posiedzen (wszystkie kadencje), linki
                                   /glosowania/posiedzenie/{id} + Liczba glosowan
  /glosowania/posiedzenie/{id}   — WSZYSTKIE glosowania posiedzenia w jednym HTML:
                                   per glosowanie: temat, decyzja (Przyjeto/Odrzucono),
                                   agregaty (Glosy za/wstrzymujace/przeciw/nieoddane,
                                   Nieobecni) + tabela "Imienny wykaz glosowania"
                                   (Imie i nazwisko | Glos: za/przeciw/wstrzymal sie/...)
  /sklad-rady                    — roster, linki /sklad-rady/radny/{id}
  /sklad-rady/radny/{id}         — rola + Klub radnego

Serwer-rendered UTF-8, bs4 wystarcza (bez JS). Kadencja IX start 2024-05-07.

Uzycie: python scrape_tomaszow_mazowiecki.py --output docs/data.json \
        --profiles docs/profiles.json [--cache-dir DIR]
"""
import argparse
import hashlib
import json
import re
import sys
import time
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime
from itertools import combinations
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE = "http://rada.tomaszow-maz.pl"
# I sesja IX kad. w Tomaszowie byla 2024-05-06 (dzien przed ogolnokrajowym startem)
KAD_START = "2024-05-01"
KADENCJA_ID = "2024-2029"
KADENCJA_LABEL = "IX kadencja (2024\u20132029)"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Firefox/126.0"}
REQ_DELAY = 0.6
_LAST = 0.0

VOTE_MAP = {
    "za": "za",
    "przeciw": "przeciw",
    "wstrzyma\u0142 si\u0119": "wstrzymal_sie",
    "wstrzymala sie": "wstrzymal_sie",
    "wstrzyma\u0142a si\u0119": "wstrzymal_sie",
    "wstrzymalo sie": "wstrzymal_sie",
    "wstrzyma\u0142o si\u0119": "wstrzymal_sie",
    "wstrzymuje sie": "wstrzymal_sie",
    "wstrzymuj\u0119 si\u0119": "wstrzymal_sie",
    "nieobecny": "nieobecni",
    "nieobecna": "nieobecni",
    "nieobecni": "nieobecni",
    "nie g\u0142osowa\u0142": "brak_glosu",
    "nie g\u0142osowa\u0142a": "brak_glosu",
    "brak g\u0142osu": "brak_glosu",
}


def _nk(s):
    s = str(s or "").lower().replace("ł", "l")
    s = unicodedata.normalize("NFKD", s)
    return re.sub(r"[^a-z0-9]", "", "".join(c for c in s if not unicodedata.combining(c)))


def _fix_name(s):
    """AlfaTV zwraca nazwiska WIELKIMI LITERAMI w tabelach imiennych ('Barbara KLATKA').
    Normalizujemy do Title Case, zachowując człon 'Nie' i przedimki."""
    s = re.sub(r"\s+", " ", str(s or "").strip())
    if not s or not s.isupper() and not re.search(r"\b[A-ZŁŚŹŻĆŃÓĄĘ]{3,}\b", s):
        return s
    words = []
    for w in s.split(" "):
        if not w:
            continue
        if w.isupper() and len(w) > 1:
            # nazwiska złożone z myślnikiem: WADOWSKA-GRYZEL -> Wadowska-Gryzel
            parts = w.lower().split("-")
            w = "-".join(p[:1].upper() + p[1:] for p in parts if p)
        else:
            w = w[:1].upper() + w[1:].lower() if w else w
        words.append(w)
    return " ".join(words)


def _norm_vote(txt):
    k = _nk(txt)
    for src, dst in VOTE_MAP.items():
        if _nk(src) == k:
            return dst
    if k.startswith("wstrzym"):
        return "wstrzymal_sie"
    if k.startswith("nieobecn"):
        return "nieobecni"
    if k in ("za", "przeciw"):
        return k
    return None


def _rate():
    global _LAST
    d = time.time() - _LAST
    if d < REQ_DELAY:
        time.sleep(REQ_DELAY - d)
    _LAST = time.time()


def get(url, cache_dir=None, binary=False):
    cf = None
    if cache_dir:
        cache_dir = Path(cache_dir)
        cache_dir.mkdir(parents=True, exist_ok=True)
        cf = cache_dir / (hashlib.md5(url.encode()).hexdigest() + (".bin" if binary else ".html"))
        if cf.is_file() and cf.stat().st_size > 200:
            return cf.read_bytes() if binary else cf.read_text(encoding="utf-8")
    _rate()
    r = requests.get(url, headers=UA, timeout=45)
    r.raise_for_status()
    data = r.content if binary else r.content.decode("utf-8", "replace")
    if cf:
        cf.write_bytes(data if binary else data.encode("utf-8"))
    return data


def make_slug(name):
    repl = {'\u0105': 'a', '\u0107': 'c', '\u0119': 'e', '\u0142': 'l', '\u0144': 'n',
            '\u00f3': 'o', '\u015b': 's', '\u017a': 'z', '\u017c': 'z'}
    sn = str(name or "").lower()
    for pl, a in repl.items():
        sn = sn.replace(pl, a)
    return re.sub(r"[^a-z0-9]+", "-", sn).strip("-")


DATE_RE = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4})")
ISO_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


def discover_sessions(cache_dir=None):
    """Lista posiedzen z /glosowania (tabela: Posiedzenie | Data | Liczba glosowan |
    link 'Sprawdz wyniki glosowan'). Filtr IX kad. Zwraca unikalne sesje plenarne
    (tytul 'sesja'; '<N> sesja' jako numer)."""
    html = get(BASE + "/glosowania", cache_dir)
    soup = BeautifulSoup(html, "lxml")
    out = {}
    for a in soup.find_all("a", href=True):
        m = re.search(r"/glosowania/posiedzenie/(\d+)", a["href"])
        if not m:
            continue
        pid = m.group(1)
        # date + tytul: wiersz tabeli zawierajacy ten link
        tr = a.find_parent("tr")
        date = None
        title = ""
        if tr:
            cells = [c.get_text(" ", strip=True) for c in tr.find_all("td")]
            for c in cells:
                im = ISO_RE.search(c)
                if im:
                    date = im.group(0)
                    break
                dm = DATE_RE.search(c)
                if dm:
                    d, mo, y = dm.groups()
                    date = f"{y}-{int(mo):02d}-{int(d):02d}"
                    break
            if cells:
                title = cells[0][:120]
        else:
            text = a.get_text(" ", strip=True)
            dm = DATE_RE.search(text)
            if dm:
                d, mo, y = dm.groups()
                date = f"{y}-{int(mo):02d}-{int(d):02d}"
            title = text[:120]
        if pid not in out or (date and (out[pid]["date"] is None or date > out[pid]["date"])):
            out[pid] = {"id": pid, "date": date, "title": title}
    sess = [s for s in out.values() if s["date"] and s["date"] >= KAD_START
            and re.search(r"\bsesj", s["title"], re.I)
            and not re.search(r"komisj|konwent|prezydium|wspolne|wspólne", s["title"], re.I)]
    sess.sort(key=lambda s: s["date"])
    return sess


def parse_session(html):
    """Jedna strona posiedzenia -> lista glosowan z tabelami imiennymi."""
    soup = BeautifulSoup(html, "lxml")
    votes = []
    for item in soup.select(".accordion-item"):
        # w uniesciu dwoie tabel: [0]=agregaty (Glosy za...), wlasciwa tabela
        # imienna = ta, ktorej naglowek zawiera 'Imie i nazwisko'
        tbl = None
        for t in item.find_all("table"):
            ths = " ".join(th.get_text(" ", strip=True) for th in t.find_all("th"))
            if "nazwisko" in ths.lower():
                tbl = t
                break
        if tbl is None:
            continue
        head = item.find(["h2", "h3", "h4", "button", "summary"]) or item
        topic = re.sub(r"\s+", " ", head.get_text(" ", strip=True))[:400]
        # agregaty z naglowka/komork: Glosy za N, wstrzymujace, przeciw, nieoddane, Nieobecni
        plain = re.sub(r"\s+", " ", item.get_text(" ", strip=True))
        # tabela imienna: Imie i nazwisko | Glos
        named = defaultdict(list)
        unparsed = Counter()
        rows = tbl.find_all("tr")
        for tr in rows:
            tds = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
            if len(tds) < 2:
                continue
            name, vote_txt = tds[0], tds[-1]
            name = _fix_name(name)
            if not name or _nk(name) in ("imie i nazwisko", "imienazwisko"):
                continue
            cat = _norm_vote(vote_txt)
            if cat is None:
                if vote_txt:
                    unparsed[vote_txt] += 1
                continue
            named[cat].append(name)
        # walidacja: sum(a) + nieoddane/nieobecni zgadza sie z licznikami (len table)
        total_rows = sum(len(v) for v in named.values()) + sum(unparsed.values())
        agg = {}
        # tabela agregatow: th naglowki (Glosy za...) + nastepny wiersz td z liczbami
        for t in item.find_all("table"):
            ths = [th.get_text(" ", strip=True) for th in t.find_all("th")]
            if ths and any("G\u0142osy" in x or "Nieobecni" in x for x in ths):
                tds = [td.get_text(" ", strip=True) for tr2 in t.find_all("tr")
                       for td in tr2.find_all("td")]
                if len(tds) == len(ths):
                    keymap = {"G\u0142osy za": "za", "G\u0142osy wstrzymuj\u0105ce": "wstrzym",
                              "G\u0142osy przeciw": "przeciw", "G\u0142osy nieoddane": "nieoddane",
                              "Nieobecni": "nieobecni"}
                    for k, val in zip(ths, tds):
                        kk = keymap.get(k)
                        if kk and val.isdigit():
                            agg[kk] = int(val)
                break
        ok = True
        if "za" in agg and len(named.get("za", [])) != agg["za"]:
            ok = False
        if "przeciw" in agg and len(named.get("przeciw", [])) != agg["przeciw"]:
            ok = False
        if "wstrzym" in agg and len(named.get("wstrzymal_sie", [])) != agg["wstrzym"]:
            ok = False
        adopted = "rzuci" in plain.lower()[:120] or "Odrzucono" in plain[:200]
        votes.append({
            "topic": topic,
            "named": {k: list(v) for k, v in named.items()},
            "agg": agg,
            "ok": ok,
            "n_rows": total_rows,
            "status": "odrzucone" if adopted else "przyjete",
        })
    return votes


def get_roster(cache_dir=None, club_assign=None):
    """(profiles_list_part, clubs_cfg) ze sklad-rady + radny/{id}."""
    club_assign = club_assign or {}
    html = get(BASE + "/sklad-rady", cache_dir)
    soup = BeautifulSoup(html, "lxml")
    ids = {}
    for a in soup.find_all("a", href=True):
        m = re.search(r"/sklad-rady/radny/(\d+)", a["href"])
        if m:
            name = _fix_name(re.sub(r"\s+", " ", a.get_text(" ", strip=True)))
            if name and len(name) > 3:
                ids.setdefault(m.group(1), name)
    roles = {}
    clubs_seen = {}
    for rid, name in ids.items():
        try:
            ph = get(f"{BASE}/sklad-rady/radny/{rid}", cache_dir)
        except Exception:
            continue
        plain = re.sub(r"\s+", " ", BeautifulSoup(ph, "lxml").get_text(" ", strip=True))
        km = re.search(r"Klub[^\n]{0,80}", plain)
        club_name = ""
        if km:
            cn = re.split(r"(Przewodniczc|Wiceprzewodniczc|Radny|Radna|Data urodzenia|Wyksztalcenie|Wykszta\u0142cenie|Zatrudnienie|Stan cywilny)", km.group(0))[0]
            club_name = re.sub(r"^Klub\s*(Radnych)?\s*", "", cn).strip(" :,.-")
        rm = re.search(r"(Przewodnicz\u0105cy Rady|Przewodnicz\u0105ca Rady|Wiceprzewodnicz\u0105cy Rady|Wiceprzewodnicz\u0105ca Rady)", plain)
        roles[name] = rm.group(1) if rm else ""
        if club_name:
            clubs_seen[name] = club_name
    return ids, roles, clubs_seen


def main():
    ap = argparse.ArgumentParser(prog="Radoskop Tomaszow Mazowiecki")
    ap.add_argument("--output", required=True)
    ap.add_argument("--profiles", required=True)
    ap.add_argument("--cache-dir", default=None)
    ap.add_argument("--max-sessions", type=int, default=0)
    ap.add_argument("--emit-clubs", default=None, help="zapisz rozpoznan kluby do JSON (kuratoring)")
    args = ap.parse_args()
    cache = Path(args.cache_dir) if args.cache_dir else None

    cfg_path = Path(__file__).resolve().parent.parent / "config.json"
    club_assign = {}
    clubs_cfg = {}
    if cfg_path.is_file():
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        club_assign = cfg.get("club_assignments", {}) or {}
        clubs_cfg = cfg.get("clubs", {}) or {}

    sessions = discover_sessions(cache)
    if args.max_sessions:
        sessions = sessions[-args.max_sessions:]
    print(f"[tomaszow] sesji IX kad: {len(sessions)} ({sessions[0]['date'] if sessions else '-'} .. {sessions[-1]['date'] if sessions else '-'})")

    all_votes = []
    sessions_data = []
    v_ok = v_bad = 0
    for se in sessions:
        try:
            html = get(f"{BASE}/glosowania/posiedzenie/{se['id']}", cache)
        except Exception as e:
            print(f"  [err {se['date']}] {e}")
            continue
        votes = parse_session(html)
        nok = sum(1 for v in votes if v["ok"])
        v_ok += nok
        v_bad += len(votes) - nok
        for i, v in enumerate(votes, 1):
            vid = f"{se['date']}_{i}"
            named = {k: list(v["named"].get(k, [])) for k in ("za", "przeciw", "wstrzymal_sie", "brak_glosu", "nieobecni")}
            if not any(named.values()):
                continue
            all_votes.append({"id": vid, "session_date": se["date"], "topic": v["topic"][:400],
                              "resolution": "", "status": v["status"], "named_votes": named,
                              "counts": {k: len(named[k]) for k in ("za", "przeciw", "wstrzymal_sie")}})
        att = set()
        for v in votes:
            for k in ("za", "przeciw", "wstrzymal_sie", "brak_glosu"):
                att.update(v["named"].get(k, []))
        nm = re.search(r"\b([IVXLCDM]+)\s+sesj", se["title"], re.I)
        sessions_data.append({"date": se["date"], "number": nm.group(1).upper() if nm else se["id"],
                              "vote_count": len(votes),
                              "attendee_count": len(att), "attendees": sorted(att), "speakers": []})
        print(f"  [{se['date']}] glosowan: {len(votes)} (ok {nok})")

    total_votes = len(all_votes)
    total_sessions = len(sessions_data)
    all_names = set()
    for v in all_votes:
        for names in v["named_votes"].values():
            all_names.update(names)
    councillor_sess = defaultdict(set)
    councilors_data = {}
    for name in sorted(all_names):
        councilors_data[name] = {"name": name, "club": club_assign.get(name, "NZ"), "district": None,
                                 "votes_za": 0, "votes_przeciw": 0, "votes_wstrzymal": 0,
                                 "votes_brak": 0, "votes_nieobecny": 0}
    for v in all_votes:
        for cat, names in v["named_votes"].items():
            for nm in names:
                councillor_sess[nm].add(v["session_date"])
                if nm not in councilors_data:
                    continue
                if cat == "za": councilors_data[nm]["votes_za"] += 1
                elif cat == "przeciw": councilors_data[nm]["votes_przeciw"] += 1
                elif cat == "wstrzymal_sie": councilors_data[nm]["votes_wstrzymal"] += 1
                elif cat == "brak_glosu": councilors_data[nm]["votes_brak"] += 1
                elif cat == "nieobecni": councilors_data[nm]["votes_nieobecny"] += 1
    councilors_list = []
    for c in councilors_data.values():
        present = c["votes_za"] + c["votes_przeciw"] + c["votes_wstrzymal"]
        aktywn = (present / total_votes * 100) if total_votes else 0
        frekw = (len(councillor_sess.get(c["name"], set())) / total_sessions * 100) if total_sessions else 0
        councilors_list.append({"name": c["name"], "club": c["club"], "district": None,
            "frekwencja": round(frekw, 1), "aktywnosc": round(aktywn, 1), "zgodnosc_z_klubem": 0.0,
            "votes_za": c["votes_za"], "votes_przeciw": c["votes_przeciw"],
            "votes_wstrzymal": c["votes_wstrzymal"], "votes_brak": c["votes_brak"],
            "votes_nieobecny": c["votes_nieobecny"], "votes_total": total_votes,
            "rebellion_count": 0, "rebellions": [], "has_activity_data": False, "activity": None})
    vectors = defaultdict(dict)
    for v in all_votes:
        for cat in ("za", "przeciw", "wstrzymal_sie"):
            for nm in v["named_votes"].get(cat, []):
                vectors[nm][v["id"]] = cat
    pairs = []
    for a, b in combinations(sorted(vectors.keys()), 2):
        common = set(vectors[a]) & set(vectors[b])
        if len(common) < 10:
            continue
        same = sum(1 for vid in common if vectors[a][vid] == vectors[b][vid])
        pairs.append({"a": a, "b": b, "club_a": club_assign.get(a, ""), "club_b": club_assign.get(b, ""),
                      "score": round(same / len(common) * 100, 1), "common_votes": len(common)})
    pairs.sort(key=lambda x: x["score"], reverse=True)
    club_counts = dict(Counter(c["club"] for c in councilors_list))
    kad = {"id": KADENCJA_ID, "label": KADENCJA_LABEL, "clubs": club_counts,
           "sessions": sessions_data, "total_sessions": total_sessions,
           "total_votes": total_votes, "total_councilors": len(councilors_list),
           "councilors": councilors_list, "votes": all_votes,
           "similarity_top": pairs[:20], "similarity_bottom": pairs[-20:][::-1]}
    out_dir = Path(args.output).parent
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"kadencja-{KADENCJA_ID}.json").write_text(json.dumps(kad, ensure_ascii=False, indent=1), encoding="utf-8")
    data = {"generated": datetime.now().isoformat(), "default_kadencja": KADENCJA_ID,
            "kadencje": [{"id": KADENCJA_ID, "label": KADENCJA_LABEL}]}
    Path(args.output).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    profiles = []
    for c in councilors_list:
        nm = c["name"]
        profiles.append({"name": nm, "slug": make_slug(nm),
            "kadencje": {KADENCJA_ID: {"club": c["club"], "has_voting_data": True,
                "has_activity_data": False, "frekwencja": c["frekwencja"], "aktywnosc": c["aktywnosc"],
                "zgodnosc_z_klubem": 0.0, "votes_za": c["votes_za"], "votes_przeciw": c["votes_przeciw"],
                "votes_wstrzymal": c["votes_wstrzymal"], "votes_brak": c["votes_brak"],
                "votes_nieobecny": c["votes_nieobecny"], "votes_total": total_votes,
                "rebellion_count": 0, "rebellions": [], "roles": [], "notes": "",
                "former": False, "mid_term": False}}})
    Path(args.profiles).write_text(json.dumps({"profiles": profiles, "total": len(profiles)}, ensure_ascii=False, indent=1), encoding="utf-8")

    if args.emit_clubs:
        try:
            ids, roles, clubs_seen = get_roster(cache, club_assign)
            Path(args.emit_clubs).write_text(json.dumps(
                {"roster": {i: n for i, n in ids.items()}, "roles": roles, "clubs": clubs_seen},
                ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"[tomaszow] roster={len(ids)} clubs na radnych: {len(clubs_seen)}")
        except Exception as e:
            print(f"[tomaszow] emit-clubs err: {e}")

    print(f"[tomaszow] DONE votes={total_votes} sessions={total_sessions} "
          f"councilors={len(profiles)} validated={v_ok}/{v_ok+v_bad}")


if __name__ == "__main__":
    main()
