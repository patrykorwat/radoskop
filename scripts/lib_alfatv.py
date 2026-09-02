#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
lib_alfatv.py — scraper platformy AlfaTV "System Rada" (rada.<miasto>.pl).

Uogólnienie scrape_turek.py (drugie miasto: Tomaszów Mazowiecki).
Struktura (server-renderowany HTML, UTF-8, bez JS):
  /glosowania                     -> lista sesji (linki /glosowania/posiedzenie/{id})
  /glosowania/posiedzenie/{id}    -> bloki div.accordion-item; każdy ma tabelę
       "Imienny wykaz głosowania" (Imię i nazwisko | Głos) + decyzję Przed/Odrzucono
  /sklad-rady                     -> roster (linki /sklad-rady/radny/{id})
  /sklad-rady/radny/{id}          -> <title>Nazwa | ... | System Rada (Klub opcjonalny)

Użycie w scraperze miasta:
    from lib_alfatv import AlfTVScraper
"""
import hashlib
import html as _html
import json
import re
import time
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime
from itertools import combinations
from pathlib import Path

import requests
import urllib3
from bs4 import BeautifulSoup

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

REQ_DELAY = 0.35
_LAST_REQ = 0.0


def _rate():
    global _LAST_REQ
    now = time.time()
    d = now - _LAST_REQ
    if d < REQ_DELAY:
        time.sleep(REQ_DELAY - d)
    _LAST_REQ = time.time()


def fetch(url: str, cache_dir: Path | None = None) -> str:
    if cache_dir is not None:
        key = hashlib.md5(url.encode()).hexdigest()
        cf = cache_dir / (key + ".html")
        if cf.is_file():
            return cf.read_text(encoding="utf-8", errors="ignore")
    _rate()
    resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"},
                        timeout=60, verify=False)
    resp.raise_for_status()
    if cache_dir is not None:
        cf = cache_dir / (hashlib.md5(url.encode()).hexdigest() + ".html")
        cf.parent.mkdir(parents=True, exist_ok=True)
        cf.write_text(resp.text, encoding="utf-8", errors="ignore")
    return resp.text


def make_slug(name: str) -> str:
    repl = {'ą': 'a', 'ć': 'c', 'ę': 'e', 'ł': 'l', 'ń': 'n', 'ó': 'o',
            'ś': 's', 'ź': 'z', 'ż': 'z'}
    slug = name.lower()
    for pl, a in repl.items():
        slug = slug.replace(pl, a)
    return re.sub(r"[^a-z0-9]+", "", slug)


class AlfTVScraper:
    """base_url = https://rada.<miasto>.pl ; kadencja 2024-2029 IX start 2024-05-07."""

    def __init__(self, base_url: str, kad_start: str = "2024-05-07",
                 kadencja_id: str = "2024-2029",
                 kadencja_label: str = "IX kadencja (2024\u20132029)",
                 city_label: str = "", club_fn=None):
        self.base = base_url.rstrip("/")
        self.kad_start = kad_start
        self.kad_id = kadencja_id
        self.kad_label = kadencja_label
        self.city = city_label
        # club_fn(name, raw_club_or_committee) -> club_key | ""
        self.club_fn = club_fn

    # ---------------- sesje ----------------
    def discover_sessions(self, cache_dir=None):
        html = fetch(f"{self.base}/glosowania", cache_dir)
        soup = BeautifulSoup(html, "html.parser")
        out = []
        for a in soup.find_all("a", href=re.compile(r"/glosowania/posiedzenie/\d+")):
            row = a.find_parent("tr") or a.find_parent("div")
            txt = re.sub(r"\s+", " ", row.get_text(" ", strip=True)) if row else ""
            dm = re.search(r"(\d{4}-\d{2}-\d{2})", txt)
            mid = int(re.search(r"(\d+)$", a["href"]).group(1))
            out.append({"id": mid, "name": txt[:80], "date": dm.group(1) if dm else ""})
        seen = set(); uniq = []
        for s in out:
            if s["id"] in seen:
                continue
            seen.add(s["id"]); uniq.append(s)
        uniq.sort(key=lambda s: s["date"])
        return [s for s in uniq if s["date"] >= self.kad_start]

    # ---------------- głosy ----------------
    def parse_session_votes(self, html):
        soup = BeautifulSoup(html, "html.parser")
        votes = []
        for item in soup.find_all("div", class_="accordion-item"):
            tables = item.find_all("table")
            big = [tb for tb in tables if len(tb.find_all("tr")) > 12]
            if not big:
                continue
            tbl = big[0]
            rows = []
            for tr in tbl.find_all("tr"):
                tds = [re.sub(r"\s+", " ", td.get_text(" ", strip=True)).strip()
                       for td in tr.find_all("td")]
                tds = [t for t in tds if t]
                if len(tds) >= 2 and not re.match(r"^\d+$", tds[0]):
                    rows.append((tds[0], tds[1].lower()))
            itext = item.get_text(" ", strip=True)
            pre, _sep, _post = itext.partition("Zakończono:")
            dm = re.findall(r"(Przyjęto|Odrzucono)", pre)
            decision = dm[-1] if dm else ""
            topic = re.sub(r"\s*(Przyjęto|Odrzucono)\s*$", "", pre).strip()
            named = defaultdict(list)
            for nm, vt in rows:
                if vt == "za":
                    named["za"].append(nm)
                elif vt == "przeciw":
                    named["przeciw"].append(nm)
                elif "wstrzym" in vt:
                    named["wstrzymal_sie"].append(nm)
            votes.append({"topic": topic, "decision": decision,
                          "named": {k: list(v) for k, v in named.items()}})
        return votes

    # ---------------- roster ----------------
    def fetch_roster(self, cache_dir=None):
        """{href: {name, club_raw}}. Nazwa z <title>; Klub z 'Przynależność...' dt/dd
        jeśli obecny (Turek); Tomaszów: brak -> ''. Zwraca też kluby surowe."""
        html = fetch(f"{self.base}/sklad-rady", cache_dir)
        links = sorted(set(re.findall(r'href="(/sklad-rady/radny/\d+)"', html)),
                       key=lambda x: int(re.search(r"(\d+)$", x).group(1)))
        roster = {}
        for l in links:
            try:
                ph = fetch(f"{self.base}{l}", cache_dir)
                tm = re.search(r"<title>(.*?)</title>", ph, re.S)
                name = _html.unescape(tm.group(1).strip().split(" | ")[0].strip()) if tm else ""
                club = ""
                soup = BeautifulSoup(ph, "html.parser")
                for dt in soup.find_all("dt"):
                    if "Przynależność" in dt.get_text(" ", strip=True):
                        dd = dt.find_next_sibling("dd")
                        if dd:
                            club = dd.get_text(" ", strip=True).strip()
                        break
                if name:
                    roster[l] = {"name": name, "club": club}
            except Exception:
                continue
        return roster

    # ---------------- build ----------------
    def _club_key(self, name, roster_by_name):
        raw = roster_by_name.get(name, {}).get("club", "")
        if self.club_fn:
            return self.club_fn(name, raw)
        return ""

    def run(self, city_dir: Path, cache_dir=None):
        city_dir = Path(city_dir)
        sessions = self.discover_sessions(cache_dir)
        print(f"[{self.city}] sesje IX: {len(sessions)}")
        roster = self.fetch_roster(cache_dir)
        roster_by_name = {v["name"]: v for v in roster.values()}
        print(f"[{self.city}] roster: {len(roster)}")

        records = []
        for s in sessions:
            try:
                html = fetch(f"{self.base}/glosowania/posiedzenie/{s['id']}", cache_dir)
                for v in self.parse_session_votes(html):
                    records.append({"session_date": s["date"], "session_num": s["name"][:24],
                                    "topic": v["topic"], "named": v["named"]})
                print(f"  {s['date']} votes={len(self.parse_session_votes(html))}")
            except Exception as e:
                print(f"  [ERR {s['id']}] {type(e).__name__}: {e}")

        all_votes = []
        vid = 0
        sessions_by_date = {}
        for rec in records:
            d = rec["session_date"]
            if d not in sessions_by_date:
                sessions_by_date[d] = {"date": d, "number": rec["session_num"],
                                       "vote_count": 0, "attendees": set()}
            vid += 1
            sessions_by_date[d]["vote_count"] += 1
            for cat in ("za", "przeciw", "wstrzymal_sie"):
                sessions_by_date[d]["attendees"].update(rec["named"].get(cat, []))
            named = {k: list(v) for k, v in rec["named"].items()}
            all_votes.append({
                "id": str(vid), "session_date": d, "session_number": rec["session_num"],
                "topic": rec["topic"], "named_votes": named,
                "counts": {k: len(named.get(k, [])) for k in ("za", "przeciw", "wstrzymal_sie")},
            })

        sessions_data = []
        for d in sorted(sessions_by_date):
            s = sessions_by_date[d]
            sessions_data.append({"date": d, "number": s["number"], "vote_count": s["vote_count"],
                                  "attendee_count": len(s["attendees"]),
                                  "attendees": sorted(s["attendees"]), "speakers": []})

        all_names = set()
        for v in all_votes:
            for names in v["named_votes"].values():
                all_names.update(names)
        all_names |= set(roster_by_name)

        councilors_data = {}
        for name in all_names:
            councilors_data[name] = {"name": name, "club": self._club_key(name, roster_by_name),
                                     "district": None, "votes_za": 0, "votes_przeciw": 0,
                                     "votes_wstrzymal": 0, "votes_brak": 0, "votes_nieobecny": 0,
                                     "rebellions": []}
        for v in all_votes:
            for cat, names in v["named_votes"].items():
                for nm in names:
                    if nm in councilors_data:
                        key = {"za": "votes_za", "przeciw": "votes_przeciw"}.get(cat, "votes_wstrzymal")
                        councilors_data[nm][key] += 1

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
            councilors_list.append({
                "name": c["name"], "club": c["club"], "district": None,
                "frekwencja": round(len(councillor_sess.get(c["name"], set())) / total_sessions * 100, 1) if total_sessions else 0,
                "aktywnosc": round(present / total_votes * 100, 1) if total_votes else 0,
                "zgodnosc_z_klubem": 0.0,
                "votes_za": c["votes_za"], "votes_przeciw": c["votes_przeciw"],
                "votes_wstrzymal": c["votes_wstrzymal"], "votes_brak": 0, "votes_nieobecny": 0,
                "votes_total": total_votes, "rebellion_count": 0, "rebellions": [],
                "has_activity_data": False, "activity": None})

        vectors = defaultdict(dict)
        for v in all_votes:
            for cat in ("za", "przeciw", "wstrzymal_sie"):
                for nm in v["named_votes"].get(cat, []):
                    vectors[nm][v["id"]] = cat
        pairs = []
        names_sorted = sorted(vectors)
        for a, b in combinations(names_sorted, 2):
            common = set(vectors[a]) & set(vectors[b])
            if len(common) < 10:
                continue
            same = sum(1 for vid_ in common if vectors[a][vid_] == vectors[b][vid_])
            pairs.append({"a": a, "b": b, "club_a": self._club_key(a, roster_by_name),
                          "club_b": self._club_key(b, roster_by_name),
                          "score": round(same / len(common) * 100, 1), "common_votes": len(common)})
        pairs.sort(key=lambda x: x["score"], reverse=True)

        club_counts = Counter(self._club_key(n, roster_by_name) for n in all_names)
        kad = {"id": self.kad_id, "label": self.kad_label, "clubs": dict(club_counts),
               "sessions": sessions_data, "total_sessions": total_sessions,
               "total_votes": total_votes, "total_councilors": len(councilors_list),
               "councilors": councilors_list, "votes": all_votes,
               "similarity_top": pairs[:20], "similarity_bottom": pairs[-20:][::-1]}
        output = {"generated": datetime.now().isoformat(), "default_kadencja": self.kad_id,
                  "kadencje": [kad]}

        # profiles
        cv = defaultdict(lambda: {"za": 0, "przeciw": 0, "wstrzymal_sie": 0, "sessions": set()})
        for rec in records:
            for cat, names in rec["named"].items():
                for nm in names:
                    cv[nm][cat] += 1
                    cv[nm]["sessions"].add(rec["session_date"])
        profiles = []
        for nm in sorted(cv):
            vd = cv[nm]
            total = vd["za"] + vd["przeciw"] + vd["wstrzymal_sie"] or 1
            profiles.append({"name": nm, "slug": make_slug(nm),
                             "kadencje": {self.kad_id: {
                                 "club": self._club_key(nm, roster_by_name),
                                 "has_voting_data": True, "has_activity_data": False,
                                 "frekwencja": round(len(vd["sessions"]) / (total_sessions or 1) * 100, 1),
                                 "aktywnosc": round(total / (total_votes or 1) * 100, 1),
                                 "zgodnosc_z_klubem": 0.0,
                                 "votes_za": vd["za"], "votes_przeciw": vd["przeciw"],
                                 "votes_wstrzymal": vd["wstrzymal_sie"], "votes_brak": 0,
                                 "votes_nieobecny": 0, "votes_total": total,
                                 "rebellion_count": 0, "rebellions": [], "roles": [],
                                 "notes": "", "former": False, "mid_term": False}}})
        profiles_out = {"profiles": profiles, "total": len(profiles)}

        docs = city_dir / "docs"
        docs.mkdir(parents=True, exist_ok=True)
        (docs / f"kadencja-{self.kad_id}.json").write_text(
            json.dumps(kad, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        (docs / "data.json").write_text(json.dumps(
            {"generated": output["generated"], "default_kadencja": self.kad_id,
             "kadencje": [{"id": self.kad_id, "label": self.kad_label}]},
            ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        (docs / "profiles.json").write_text(
            json.dumps(profiles_out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        print(f"[{self.city}] votes={total_votes} sessions={total_sessions} "
              f"councilors={len(councilors_list)}")
        return kad
