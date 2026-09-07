#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop Pszów — scraper głosowań imiennych (eurzad.finn.pl / eZeto Finn).

Rada Miejska w Pszowie publikuje w rejestrze PRM6 platformy eurzad.finn.pl
(klient gmpszow) per-sesyjne "Protokół głosowania" — PDF tekstowy z blokami
"GŁOSOWANIE": nagłówek (tytuł, TYP/DATA GŁOSOWANIA, GŁOSY ZA/PRZECIW/
WSTRZYMUJĄCE SIĘ/NIEODDANE) + tabela "LP RADNY GŁOS" z tokenami
Za / Nie / Wstrzymuje się / Nieobecny / Nieoddany.

API: POST /gmpszow/rpc/routeRejestryServiceManager{1,2} + PDF przez
/gmpszow/server/pobierz_rejestry_zalacznik/{id}. (ten sam wzorzec co
scrape_radomsko.py, dodany 2026-09-03).

Dodane 2026-09-07 (cron ekspansja 500).
"""
import argparse
import io
import json
import re
import ssl
import sys
import time
import unicodedata
import urllib.request
from pathlib import Path

try:
    import pdfplumber
except ImportError:  # pragma: no cover
    print("brak pdfplumber: pip install pdfplumber", file=sys.stderr)
    raise

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE
BASE = "https://eurzad.finn.pl/gmpszow"
HEADERS = {"User-Agent": "Mozilla/5.0 (Radoskop cron)",
           "Content-Type": "application/json",
           "X-Requested-With": "XMLHttpRequest"}

KAD = "2024-2029"
KAD_START = "2024-05-07"
REGISTRY = "PRM6"

MONTHS = {m: i for i, m in enumerate(
    "stycznia lutego marca kwietnia maja czerwca lipca sierpnia "
    "września października listopada grudnia".split(), 1)}

TOPIC_RE = re.compile(r"(?:w )?z dnia (\d{1,2}) ([a-ząćęłńóśźż]+) (\d{4})", re.I)
ROW_RE = re.compile(
    r"(?m)^(\d+) (.+?) (Za|Nie|Przeciw|Wstrzymuje się|Nieobecny|Nieoddany)\s*$")
FOOTER_RE = re.compile(r"\nProtokół głosowania\nz dnia.*$", re.S)


def _rpc(method: str, params: list):
    body = json.dumps({"id": 1, "method": f"/rpc/{method}", "params": params}).encode()
    req = urllib.request.Request(f"{BASE}/rpc/{method}", headers=HEADERS, data=body)
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30, context=CTX) as r:
                d = json.loads(r.read())
            res = d.get("result")
            return json.loads(res) if isinstance(res, str) else res
        except Exception:
            if attempt == 3:
                raise
            time.sleep(2 * (attempt + 1))


def _get_pdf(zal_id: int) -> bytes:
    req = urllib.request.Request(f"{BASE}/server/pobierz_rejestry_zalacznik/{zal_id}",
                                 headers={"User-Agent": "Mozilla/5.0 (Radoskop cron)"})
    with urllib.request.urlopen(req, timeout=60, context=CTX) as r:
        return r.read()


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c)).lower().replace("ł", "l")
    return re.sub(r"[^a-z0-9]", "", s)


def _slug(name: str) -> str:
    return _norm(name.replace("-", "")) or "radny"


def _date_from_detail(det: dict, temat: str) -> str | None:
    m = TOPIC_RE.search(temat or "")
    if m:
        mon = MONTHS.get(m.group(2).lower())
        if mon:
            return f"{m.group(3)}-{mon:02d}-{int(m.group(1)):02d}"
    dw = (det or {}).get("dataW") or ""
    dm = re.search(r"(\d{4})-(\d{2})-(\d{2})", str(dw))
    if dm:
        return dm.group(0)
    dm = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", str(dw))
    if dm:
        return f"{dm.group(3)}-{dm.group(2)}-{dm.group(1)}"
    return None


def list_ix_items() -> list[dict]:
    out = []
    for page in ("1", "2", "3"):
        lst = _rpc("routeRejestryServiceManager1",
                   [json.dumps(REGISTRY), '"100"', f'"{page}"', '"DEFAULT"', "{}"])
        items = (lst or {}).get("lista", [])
        if not items:
            break
        for it in items:
            out.append({"id": it["id"], "temat": it.get("temat") or ""})
        if int((lst or {}).get("liczbaStron") or 1) <= int(page):
            break
    resolved = []
    for it in out:
        det = _rpc("routeRejestryServiceManager2", [json.dumps(str(it["id"]))])
        date = _date_from_detail(det, it["temat"])
        if date and date >= KAD_START:
            zals = [z["id"] for z in ((det or {}).get("zalaczniki") or [])]
            if zals:
                resolved.append({"id": it["id"], "date": date, "temat": it["temat"],
                                 "zals": zals})
        time.sleep(0.2)
    resolved.sort(key=lambda r: r["date"])
    return resolved


def parse_pdf(pdf_bytes: bytes) -> tuple[list[dict], list[str]]:
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        full = "\n".join((p.extract_text() or "") for p in pdf.pages)
    blocks = full.split("\nGŁOSOWANIE\n")
    votes = []
    roster: list[str] = []
    for blk in blocks[1:]:
        blk = FOOTER_RE.sub("", blk)
        rows = ROW_RE.findall(blk)
        if not rows:
            continue
        head = blk.split("\nLP RADNY GŁOS")[0]
        tm = re.split(r"TYP GŁOSOWANIA", head)
        title = re.sub(r"\s+", " ", tm[0]).strip()
        dm = re.search(r"DATA GŁOSOWANIA (\d{4})-(\d{2})-(\d{2})", head)
        vdate = f"{dm.group(1)}-{dm.group(2)}-{dm.group(3)}" if dm else None
        agg = {}
        for key, pat in (("za", r"GŁOSY ZA (\d+)"),
                         ("przeciw", r"GŁOSY PRZECIW (\d+)"),
                         ("wstrzymal_sie", r"GŁOSY WSTRZYMUJĄCE SIĘ (\d+)")):
            mm = re.search(pat, head)
            agg[key] = int(mm.group(1)) if mm else None
        nv = {"za": [], "przeciw": [], "wstrzymal_sie": [],
              "nieobecny": [], "nieoddany": []}
        for _lp, name, tok in rows:
            name = re.sub(r"\s+", " ", name).strip()
            if name not in roster:
                roster.append(name)
            if tok == "Za":
                nv["za"].append(name)
            elif tok in ("Nie", "Przeciw"):
                nv["przeciw"].append(name)
            elif tok.startswith("Wstrzym"):
                nv["wstrzymal_sie"].append(name)
            elif tok == "Nieobecny":
                nv["nieobecny"].append(name)
            else:
                nv["nieoddany"].append(name)
        ok = (agg.get("za") is None or agg["za"] == len(nv["za"])) and \
             (agg.get("przeciw") is None or agg["przeciw"] == len(nv["przeciw"])) and \
             (agg.get("wstrzymal_sie") is None or agg["wstrzymal_sie"] == len(nv["wstrzymal_sie"]))
        if not ok:
            print(f"  [warn] agregaty nie zgadzają się: {title[:60]} agg={agg} "
                  f"parsed za={len(nv['za'])} przeciw={len(nv['przeciw'])} wstrz={len(nv['wstrzymal_sie'])}",
                  file=sys.stderr)
            continue
        votes.append({"title": title or f"Głosowanie {len(votes)+1}", "date": vdate,
                      "named_votes": nv, "total_za": agg.get("za"),
                      "total_przeciw": agg.get("przeciw"),
                      "total_wstrzymal": agg.get("wstrzymal_sie")})
    return votes, roster


def main() -> int:
    ap = argparse.ArgumentParser(prog="Radoskop Pszów (eurzad.finn.pl PRM6)")
    ap.add_argument("--output", default="docs/data.json")
    ap.add_argument("--profiles", default="docs/profiles.json")
    ap.add_argument("--max-sessions", type=int, default=0)
    args = ap.parse_args()

    items = list_ix_items()
    if args.max_sessions:
        items = items[-args.max_sessions:]
    print(f"  sesji IX: {len(items)}")

    cfg_path = Path(__file__).resolve().parent.parent / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.is_file() else {}

    all_votes: list[dict] = []
    sessions: list[dict] = []
    roster: list[str] = []
    for it in items:
        votes, rr = [], []
        for z in it["zals"]:
            data = _get_pdf(z)
            v2, r2 = parse_pdf(data)
            votes.extend(v2)
            rr.extend(r2)
        for v in votes:
            v["session_date"] = it["date"]
        all_votes.extend(votes)
        for n in rr:
            if n not in roster:
                roster.append(n)
        sessions.append({"date": it["date"], "number": "", "title": it["temat"],
                         "vote_count": len(votes)})
        print(f"  {it['date']}: {len(votes)} głosowań")
        time.sleep(0.4)

    sessions.sort(key=lambda s: s["date"])
    votes_out = []
    for i, v in enumerate(all_votes):
        votes_out.append({
            "id": f"psz-{i+1}",
            "date": v.get("date") or v["session_date"],
            "title": v["title"],
            "session_date": v["session_date"],
            "named_votes": v["named_votes"],
            "total_za": v.get("total_za"), "total_przeciw": v.get("total_przeciw"),
            "total_wstrzymal": v.get("total_wstrzymal"),
        })

    councilors = [{"name": n, "slug": _slug(n), "club": cfg.get("club_assignments", {}).get(n, ""),
                   "district": None, "frekwencja": None, "aktywnosc": None,
                   "zgodnosc_z_klubem": None, "votes_total": 0, "rebellion_count": 0,
                   "has_activity_data": False} for n in roster]
    for c in councilors:
        absent = sum(1 for v in votes_out if c["name"] in v["named_votes"].get("nieobecny", []))
        if votes_out:
            c["frekwencja"] = round(100.0 * (len(votes_out) - absent) / len(votes_out))
        c["votes_total"] = sum(1 for v in votes_out
                               for k in ("za", "przeciw", "wstrzymal_sie")
                               if c["name"] in v["named_votes"].get(k, []))
    kadencja = {
        "id": KAD, "label": cfg.get("kadencje", {}).get(KAD, {}).get("label", "IX kadencja (2024–2029)"),
        "clubs": cfg.get("clubs", {}),
        "sessions": [{"date": s["date"], "number": s["number"], "vote_count": s["vote_count"],
                      "attendee_count": None, "attendees": [], "speakers": [], "title": s["title"]}
                     for s in sessions],
        "total_sessions": len(sessions), "total_votes": len(votes_out),
        "total_councilors": len(roster),
        "councilors": councilors,
        "votes": votes_out, "similarity_top": [], "similarity_bottom": [],
        "named_votes_index": {},
    }
    outp = Path(args.output)
    outp.parent.mkdir(parents=True, exist_ok=True)
    (outp.parent / f"kadencja-{KAD}.json").write_text(
        json.dumps(kadencja, ensure_ascii=False, indent=1), encoding="utf-8")
    data = {"city": "Pszów", "scraped_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "kadencje": [{"id": KAD, "label": kadencja["label"]}],
            "sessions": len(sessions), "votes": len(votes_out), "councilors": len(roster)}
    outp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    profiles = {"scraped_at": data["scraped_at"], "total": len(councilors), "profiles": [
        {"name": c["name"], "slug": c["slug"], "club": c["club"], "role": "", "photo_url": "",
         "bio": "", "email": "", "social_links": {},
         "kadencje": {KAD: {"club": c["club"], "has_voting_data": True, "role": "",
                            "frekwencja": c["frekwencja"], "aktywnosc": 0,
                            "zgodnosc_z_klubem": None, "rebellion_count": 0,
                            "votes_total": c["votes_total"]}}}
        for c in councilors]}
    Path(args.profiles).write_text(json.dumps(profiles, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"  DONE: {len(sessions)} sesji, {len(votes_out)} głosowań, {len(roster)} radnych")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
