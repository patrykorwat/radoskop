#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop Oborniki — Tier-2 (roster / "model berliński") scraper.

eSesja Portal Mieszkańca oborniki.esesja.pl: /glosowania nie działa (PM-B,
0 sesji w liście), ALE profile radnych /radny/{id}/{slug}.htm serwują realne
statystyki per radny: obecność na posiedzeniach (27/33), udział w głosowaniach
(341/452), rozkład głosów ZA/PRZECIW/WSTRZYMUJĄCE, wypowiedzi (ogólne/ad vocem
+ minuty). Skład 21 radnych z /posiedzenia. Brak kalendarza sesji (Archiwum 0).

has_voting_data:false (brak tabel per głosowanie),
has_speaker_activity:true (wypowiedzi per radny).
"""
import json
import re
import ssl
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE
HEADERS = {"User-Agent": "Mozilla/5.0 (Radoskop cron)"}

BASE = "https://oborniki.esesja.pl"
KAD = "2024-2029"
_last = 0.0


def _http(url):
    global _last
    d = time.time() - _last
    if d < 0.35:
        time.sleep(0.35 - d)
    _last = time.time()
    req = urllib.request.Request(urllib.parse.quote(url, safe=":/?&=%"), headers=HEADERS)
    with urllib.request.urlopen(req, timeout=45, context=_CTX) as r:
        raw = r.read()
    for enc in ("utf-8", "windows-1250"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def _slug(name):
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c)).lower().replace("ł", "l")
    s = re.sub(r"[^a-z0-9]+", "", s)
    return s or "radny"


def fetch_roster_links():
    t = _http(f"{BASE}/posiedzenia")
    rows = re.findall(r'href="(/radny/(\d+)/[^"]*)"><strong>([^<]+)</strong>', t)
    out = {}
    for href, rid, name in rows:
        out[int(rid)] = (href, re.sub(r"\s+", " ", name).strip())
    return out


def fetch_radny_stats(href):
    t = _http(f"{BASE}{href}")
    body = re.sub(r"<script.*?</script>|<style.*?</style>", " ", t, flags=re.S)
    body = re.sub(r"<[^>]+>", " ", body)
    body = re.sub(r"\s+", " ", body)
    stats = {}
    m = re.search(r"Obecność na posiedzeniach:\s*(\d+)/(\d+)", body)
    if m:
        stats["sess_present"], stats["sess_total"] = int(m.group(1)), int(m.group(2))
    m = re.search(r"Udział w głosowaniach:\s*(\d+)/(\d+)", body)
    if m:
        stats["votes_part"], stats["votes_total"] = int(m.group(1)), int(m.group(2))
    m = re.search(r"Głosy:\s*ZA\s*-\s*(\d+)\s*,\s*PRZECIW\s*-\s*(\d+)\s*,\s*WSTRZYMUJĄCE\s*-\s*(\d+)", body)
    if m:
        stats["za"], stats["przeciw"], stats["wstrzymal"] = (int(m.group(i)) for i in (1, 2, 3))
    m = re.search(r"Wypowiedź ogólna:\s*(\d+)x\s*,\s*minuty wypowiedzi:\s*(\d+)", body)
    if m:
        stats["wyp_ogolne"], stats["min_ogolne"] = int(m.group(1)), int(m.group(2))
    m = re.search(r"Ad vocem:\s*(\d+)x", body)
    if m:
        stats["ad_vocem"] = int(m.group(1))
    m = re.search(r"strong>(Radny|Radna|Przewodniczący|Przewodnicząca|Wiceprzewodniczący|Wiceprzewodnicząca)</strong", t)
    if not m:
        m = re.search(r">\s*(Przewodniczący|Przewodnicząca|Wiceprzewodniczący|Wiceprzewodnicząca)\s*<", t)
    stats["role"] = m.group(1) if m else ""
    return stats


def build(city_dir: Path) -> int:
    cfg = json.loads((city_dir / "config.json").read_text(encoding="utf-8"))
    docs = city_dir / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    links = fetch_roster_links()
    print(f"  oborniki roster links: {len(links)}")
    if len(links) < 10:
        print("  [ERR] roster zbyt mały — przerywam")
        return 1

    profiles = []
    councilors = []
    for rid, (href, name) in sorted(links.items()):
        try:
            st = fetch_radny_stats(href)
        except Exception as e:
            print(f"  [warn] {name}: {type(e).__name__}")
            st = {}
        frekw = round(st.get("sess_present", 0) / st.get("sess_total", 1) * 100, 1) if st.get("sess_total") else None
        aktywn = round(st.get("votes_part", 0) / st.get("votes_total", 1) * 100, 1) if st.get("votes_total") else None
        councilors.append({
            "name": name, "club": "", "district": None,
            "frekwencja": frekw, "aktywnosc": aktywn, "zgodnosc_z_klubem": None,
            "votes_za": st.get("za", 0), "votes_przeciw": st.get("przeciw", 0),
            "votes_wstrzymal": st.get("wstrzymal", 0),
            "votes_total": st.get("votes_part", 0),
            "rebellion_count": 0, "has_activity_data": True,
            "activity": {"wypowiedzi_ogolne": st.get("wyp_ogolne", 0),
                         "minuty_wypowiedzi": st.get("min_ogolne", 0),
                         "ad_vocem": st.get("ad_vocem", 0)},
        })
        profiles.append({
            "name": name, "slug": _slug(name),
            "kadencje": {KAD: {
                "club": "", "has_voting_data": False, "has_activity_data": True,
                "frekwencja": frekw, "aktywnosc": aktywn, "zgodnosc_z_klubem": None,
                "votes_za": st.get("za", 0), "votes_przeciw": st.get("przeciw", 0),
                "votes_wstrzymal": st.get("wstrzymal", 0), "votes_brak": 0,
                "votes_nieobecny": 0, "votes_total": st.get("votes_part", 0),
                "rebellion_count": 0, "rebellions": [],
                "roles": [st["role"]] if st.get("role") else [], "notes": "",
                "former": False, "mid_term": False}},
        })
        print(f"    {name:<30} {frekw}% {aktywn}% wyp={st.get('wyp_ogolne',0)}")

    # kadencja-level aggregate for sparkline: use union of sess_total (calendar unknown)
    max_sess = max((c["frekwencja"] or 0) for c in councilors) if councilors else 0
    kadencja = {
        "id": KAD, "label": cfg["kadencje"][KAD]["label"], "clubs": {},
        "sessions": [], "total_sessions": 0, "total_votes": 0,
        "total_councilors": len(councilors),
        "councilors": councilors,
        "votes": [], "similarity_top": [], "similarity_bottom": [],
    }
    (docs / f"kadencja-{KAD}.json").write_text(
        json.dumps(kadencja, ensure_ascii=False, indent=1), encoding="utf-8")
    (docs / "data.json").write_text(json.dumps(
        {"generated": datetime.now().isoformat(), "default_kadencja": KAD,
         "kadencje": [{"id": KAD, "label": cfg["kadencje"][KAD]["label"]}]},
        ensure_ascii=False), encoding="utf-8")
    (docs / "profiles.json").write_text(
        json.dumps({"profiles": profiles, "total": len(profiles)},
                   ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"  [ok] {len(profiles)} profili (kadencja-level stats)")
    return 0


if __name__ == "__main__":
    city_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]
    raise SystemExit(build(city_dir))
