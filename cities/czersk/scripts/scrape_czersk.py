#!/usr/bin/env python3
"""Radoskop Czersk — custom scraper for the sesjaradygminy.pl "Portal Radnego" platform.

Backend: https://czersk.sesjaradygminy.pl (Rada Miejska w Czersku, IX kadencja 2024-2029).
Czersk uses the sesjaradygminy.pl (Nexa "Portal Radnego") platform, structurally similar
to eSesja but a separate ASP.NET app. Key endpoints:
  - /Voting/All          → page with the full voting list embedded as server-side JSON
                           in `vm.List.Items([...])` (sessions are IsGroup, votes are
                           children with ParentId → session id, Date "DD.MM.YYYY").
  - /Voting/Details/{id} → server-rendered HTML with per-councilor imienne votes grouped
                           under "Za" / "Przeciw" / "Wstrzymuje" / "Nie oddali głosu".

This scraper:
  1. Pulls /Voting/All JSON, keeps IX-kadencja sessions (date >= 2024-05-07).
  2. For each vote fetches /Voting/Details/{id}, extracts per-councilor vote (name → za/przeciw/wstrzymal_sie/brak_glosu/nieobecny).
  3. Builds the standard Radoskop outputs: kadencja-{kid}.json (with councilor_index raadslid-style
     IDs + named_votes), data.json index, profiles.json.
"""

import argparse
import json
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE = "https://czersk.sesjaradygminy.pl"
HDRS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
    "Accept-Language": "pl-PL,pl;q=0.9",
}
KADENCJE = {"2024-2029": {"label": "IX kadencja (2024–2029)", "start": "2024-05-07"}}
DEFAULT_KAD = "2024-2029"
DELAY = 0.4


def _fetch(url: str, retries: int = 5) -> str:
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=HDRS)
            with urllib.request.urlopen(req, timeout=30) as r:
                data = r.read()
                enc = r.headers.get_content_charset() or "utf-8"
                return data.decode(enc, "replace")
        except Exception as e:
            if attempt == retries - 1:
                raise
            time.sleep(1.0 * (attempt + 1))
    raise RuntimeError("unreachable")


def _parse_date(ddmmmyy: str) -> str:
    """'19.11.2018' → '2018-11-19'."""
    dd, mm, yy = ddmmmyy.split(".")
    return f"{yy}-{mm}-{dd}"


def _voting_all() -> list[dict]:
    html = _fetch(f"{BASE}/Voting/All")
    m = re.search(r"vm\.List\.Items\((\[.*?\])\s*\);", html, re.S)
    if not m:
        raise RuntimeError("Voting/All: Items JSON not found")
    return json.loads(m.group(1))


def _reorder_name(name: str) -> str:
    """sesjaradygminy gives names as 'Nazwisko Imię' (surname first, possibly hyphenated).
    Radoskop expects 'Imię Nazwisko'. All councilor names here are exactly two tokens."""
    parts = name.split()
    if len(parts) == 2:
        return f"{parts[1]} {parts[0]}"
    return name


def _voting_details(vote_id: str) -> dict[str, list[str]]:
    """Return {'za': [...], 'przeciw': [...], 'wstrzymal_sie': [...], 'brak_glosu': [...]} by name."""
    html = _fetch(f"{BASE}/Voting/Details/{vote_id}")
    out: dict[str, list[str]] = {"za": [], "przeciw": [], "wstrzymal_sie": [], "brak_glosu": []}
    label_map = {"Za": "za", "Przeciw": "przeciw", "Wstrzymuje": "wstrzymal_sie", "Nie oddali głosu": "brak_glosu"}
    # Walk each votesGroup block: header div (+ content), then vote spans.
    # Precisely match `<div class="header">` and `<span class="vote">…</span>` so
    # `votesGroup` is NOT mistaken for a `vote` element.
    pat = re.compile(
        r'<div class="header">\s*(?:<[^>]+>)*\s*([^<]{1,30}?)\s*</div>'
        r'|<span class="vote">\s*([^<]+?)\s*</span>',
        re.S,
    )
    cur = None
    for m in pat.finditer(html):
        if m.group(1) is not None:
            label = m.group(1).strip()
            cur = label_map.get(label)
        elif m.group(2) is not None and cur:
            out[cur].append(_reorder_name(m.group(2).strip()))
    return out


def build(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="Radoskop Czersk (https://czersk.sesjaradygminy.pl)")
    ap.add_argument("--output", default="docs/data.json")
    ap.add_argument("--profiles", default="docs/profiles.json")
    ap.add_argument("--delay", type=float, default=DELAY)
    args = ap.parse_args(argv)

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    items = _voting_all()
    groups = [x for x in items if x.get("IsGroup")]
    votes = [x for x in items if not x.get("IsGroup")]
    v_by_parent: dict[str, list] = {}
    for v in votes:
        v_by_parent.setdefault(v.get("ParentId"), []).append(v)

    def _vdate(v):
        return _parse_date(v.get("Date", "01.01.2000"))

    ix_sessions = [g for g in groups if _vdate(g) >= KADENCJE[DEFAULT_KAD]["start"]]
    ix_sessions.sort(key=lambda g: _vdate(g))

    # Determine the roster of councilors = union of names appearing in IX-kadencja vote details.
    councilor_names: list[str] = []
    seen: set[str] = set()
    resolved_votes: list[dict] = []
    per_session: dict[str, list] = {}

    for g in ix_sessions:
        sdate = _vdate(g)
        g_votes = sorted(v_by_parent.get(g["Id"], []), key=lambda v: int(v.get("Order") or 0))
        sess_votes = []
        for v in g_votes:
            if v.get("Canceled"):
                continue
            det = _voting_details(v["Id"])
            time.sleep(args.delay)
            # counts from the JSON aggregate
            counts = {
                "za": v.get("For") or 0,
                "przeciw": v.get("Against") or 0,
                "wstrzymal_sie": v.get("Abstain") or 0,
                "brak_glosu": v.get("WithoutVote") or 0,
                "nieobecni": max(0, (v.get("Total") or 0) - (v.get("For") or 0) - (v.get("Against") or 0) - (v.get("Abstain") or 0) - (v.get("WithoutVote") or 0)),
            }
            # register names
            for name in det["za"] + det["przeciw"] + det["wstrzymal_sie"] + det["brak_glosu"]:
                if name and name not in seen:
                    seen.add(name)
                    councilor_names.append(name)
            topic = (v.get("Description") or "").strip()
            vote_rec = {
                "id": f"{sdate}_{len(resolved_votes):03d}",
                "source_url": f"{BASE}/Voting/Details/{v['Id']}",
                "session_date": sdate,
                "session_number": "",
                "topic": topic,
                "druk": None,
                "resolution": None,
                "counts": counts,
                "named_votes": {
                    "za": det["za"],
                    "przeciw": det["przeciw"],
                    "wstrzymal_sie": det["wstrzymal_sie"],
                    "brak_glosu": det["brak_glosu"],
                    "nieobecni": [],
                },
            }
            resolved_votes.append(vote_rec)
            sess_votes.append(vote_rec)
        per_session[sdate] = sess_votes

    # councilor_index: raadslid-style integers assigned by sorted name order
    councilor_names.sort(key=lambda n: n.lower())
    id_of_name = {n: i for i, n in enumerate(councilor_names)}
    # rebuild votes with index-based named_votes
    for vrec in resolved_votes:
        nv = vrec["named_votes"]
        vrec["named_votes"] = {
            "za": [id_of_name[n] for n in nv["za"] if n in id_of_name],
            "przeciw": [id_of_name[n] for n in nv["przeciw"] if n in id_of_name],
            "wstrzymal_sie": [id_of_name[n] for n in nv["wstrzymal_sie"] if n in id_of_name],
            "brak_glosu": [id_of_name[n] for n in nv["brak_glosu"] if n in id_of_name],
            "nieobecni": [],
        }

    sessions = []
    for sdate in sorted(per_session.keys()):
        n = len(per_session[sdate])
        sessions.append({"date": sdate, "number": "", "label": f"Sesja {n} ({sdate})", "vote_count": n})

    # per-councilor stats
    councilor_votes = {i: {"za": 0, "przeciw": 0, "wstrzymal_sie": 0, "brak_glosu": 0, "nieobecny": 0} for i in range(len(councilor_names))}
    for vrec in resolved_votes:
        nv = vrec["named_votes"]
        for i in nv["za"]:
            councilor_votes[i]["za"] += 1
        for i in nv["przeciw"]:
            councilor_votes[i]["przeciw"] += 1
        for i in nv["wstrzymal_sie"]:
            councilor_votes[i]["wstrzymal_sie"] += 1
        for i in nv["brak_glosu"]:
            councilor_votes[i]["brak_glosu"] += 1

    total_votes = len(resolved_votes)
    councilors_out = []
    for i, name in enumerate(councilor_names):
        cv = councilor_votes[i]
        tot = cv["za"] + cv["przeciw"] + cv["wstrzymal_sie"] + cv["brak_glosu"]
        frekw = round((tot / max(total_votes * 1, 1)) * 100, 1)
        councilors_out.append({
            "name": name, "club": "", "district": None,
            "votes_za": cv["za"], "votes_przeciw": cv["przeciw"],
            "votes_wstrzymal": cv["wstrzymal_sie"], "votes_brak": cv["brak_glosu"],
            "votes_nieobecny": cv["nieobecny"], "votes_total": tot,
            "frekwencja": frekw, "aktywnosc": 0.0, "zgodnosc_z_klubem": 0.0,
            "rebellion_count": 0, "rebellions": [],
        })

    kad = {
        "id": DEFAULT_KAD, "label": KADENCJE[DEFAULT_KAD]["label"],
        "names_normalized": False, "clubs": {},
        "sessions": sessions, "total_sessions": len(sessions),
        "total_votes": total_votes, "total_councilors": len(councilor_names),
        "councilors": councilors_out, "votes": resolved_votes,
        "similarity_top": [], "similarity_bottom": [],
        "councilor_index": councilor_names,  # names (PL-style) — resolve_named_votes + councilors list
        "raadslid_ids": list(range(len(councilor_names))),
    }
    kad_file = out_path.parent / f"kadencja-{DEFAULT_KAD}.json"
    kad_file.write_text(json.dumps(kad, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    data = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "default_kadencja": DEFAULT_KAD,
        "kadencje": [{"id": DEFAULT_KAD, "label": KADENCJE[DEFAULT_KAD]["label"]}],
    }
    out_path.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    profiles = {
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "profiles": [{
            "name": n,
            "slug": re.sub(r"[^a-z0-9]+", "-", n.lower()).strip("-"),
            "club": "", "role": "", "photo_url": "", "bio": "", "email": "",
            "social_links": {},
            "voting": None,
            "kadencje": {DEFAULT_KAD: {
                "club": "", "has_voting_data": True,
                "votes_za": councilor_votes[i]["za"], "votes_przeciw": councilor_votes[i]["przeciw"],
                "votes_wstrzymal": councilor_votes[i]["wstrzymal_sie"], "votes_brak": councilor_votes[i]["brak_glosu"],
                "frekwencja": councilors_out[i]["frekwencja"], "aktywnosc": 0.0, "zgodnosc_z_klubem": 0.0,
                "rebellion_count": 0, "has_activity_data": False,
            }},
        } for i, n in enumerate(councilor_names)],
        "total": len(councilor_names),
    }
    Path(args.profiles).write_text(json.dumps(profiles, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"OK: {len(ix_sessions)} sesji, {total_votes} głosowań, {len(councilor_names)} radnych")
    print(f"  zapisano {kad_file} + {out_path} + {args.profiles}")
    return 0


if __name__ == "__main__":
    raise SystemExit(build())
