#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop Słupca — scraper głosowań imiennych (eurzad.finn.pl / FINN e-Urząd).

Rada Miasta Słupcy publikuje "Rejestr imiennych wykazów głosowań radnych"
(rejestr ruigr2024-2029) w eurzad.finn.pl/gmslupca. Platforma odsłania
JSON-RPC (POST /rpc/routeRejestryServiceManager{1,2}) + pliki PDF przez
/server/pobierz_rejestry_zalacznik/{id}.

W Słupcy KAŻDY załącznik = jedno głosowanie (PDF "Sesja N, Glosowanie M,
Data …") z tabelą dwukolumnową "Lp Nazwisko i imię Głos" (tokeny
ZA/NIE/NIEOBECNY/NIEOBECNA/WSTRZYMUJĘ SIĘ/OBECNY/NIEOBECNY) i agregatami
"Głosy za/przeciw/wstrzymujące się". Walidacja agregatami per głosowanie.

Dodane 2026-09-07 (cron ekspansja 500). Wzorzec: scrape_radomsko.py.
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
BASE = "https://eurzad.finn.pl/gmslupca"
HEADERS = {"User-Agent": "Mozilla/5.0 (Radoskop cron)",
           "Content-Type": "application/json",
           "X-Requested-With": "XMLHttpRequest"}

KAD = "2024-2029"
KAD_START = "2024-05-07"
REGISTRY = "ruigr2024-2029"

MONTHS = {m: i for i, m in enumerate(
    "stycznia lutego marca kwietnia maja czerwca lipca sierpnia "
    "września października listopada grudnia".split(), 1)}

TOPIC_RE = re.compile(r"Sesja\s+\S+.*?z dnia (\d{1,2}) ([a-ząćęłńóśźż]+) (\d{4})", re.I)
# tokeny z formami żeńskimi; NIEOBECN\w+ przed NIE (alternacja lewo->prawo)
ROW_RE = re.compile(
    r"(\d+)\.\s+([A-ZŁŚŻ][\wŁŚŻćęłńóśźż-]+(?:\s+[A-ZŁŚŻ][\wŁŚŻćęłńóśźż-]+){1,2}?)\s+"
    r"(ZA|NIEOBECN\w+|PRZECIW|WSTRZYMUJ\w+ SI\w+|NIE|OBECN\w+)(?![A-ZŁŚŻĆĘŁŃÓŚŹŻ])")


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
    with urllib.request.urlopen(req, timeout=45, context=CTX) as r:
        return r.read()


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c)).lower().replace("ł", "l")
    return re.sub(r"[^a-z0-9]", "", s)


def _slug(name: str) -> str:
    return _norm(name.replace("-", "")) or "radny"


def _display(nazw_imie: str) -> str:
    """'Biskupska-Sobolewska Barbara' -> 'Barbara Biskupska-Sobolewska'."""
    parts = nazw_imie.rsplit(" ", 1)
    if len(parts) == 2 and " " not in parts[0]:
        return f"{parts[1]} {parts[0]}"
    return nazw_imie


def list_ix_items() -> list[dict]:
    lst = _rpc("routeRejestryServiceManager1", [json.dumps(REGISTRY), '"200"', '"1"', '"DEFAULT"', "{}"])
    out = []
    for it in (lst or {}).get("lista", []):
        m = TOPIC_RE.search(it.get("temat") or "")
        if not m:
            continue
        mon = MONTHS.get(m.group(2).lower())
        if not mon:
            continue
        date = f"{m.group(3)}-{mon:02d}-{int(m.group(1)):02d}"
        if date >= KAD_START:
            out.append({"id": it["id"], "date": date, "temat": it["temat"]})
    out.sort(key=lambda r: r["date"])
    return out


def parse_vote_pdf(pdf_bytes: bytes) -> tuple[dict | None, list[str]]:
    """Jeden PDF = jedno głosowanie. -> (vote|None, roster_display_names)."""
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        full = "\n".join((p.extract_text() or "") for p in pdf.pages)
    rows = ROW_RE.findall(full)
    if not rows:
        return None, []
    head = full[:800]
    tm = re.search(r"Głosowanie w sprawie:\s*(.{0,400}?)\s*Typ głosowania", full, re.S)
    title = re.sub(r"\s+", " ", tm.group(1)).strip() if tm else ""
    dm = re.search(r"Data głosowania:\s*(\d{2})\.(\d{2})\.(\d{4})", head)
    vdate = f"{dm.group(3)}-{dm.group(2)}-{dm.group(1)}" if dm else None
    agg = {}
    for key, pat in (("za", r"Głosy za\s*(\d+)"),
                     ("przeciw", r"Głosy przeciw\s*(\d+)"),
                     ("wstrzymal_sie", r"Głosy wstrzymujące się\s*(\d+)")):
        mm = re.search(pat, head)
        agg[key] = int(mm.group(1)) if mm else None
    nv = {"za": [], "przeciw": [], "wstrzymal_sie": [], "nieobecny": [], "obecny_nieglosujacy": []}
    roster: list[str] = []
    for _lp, nazw, tok in rows:
        disp = _display(nazw)
        if disp not in roster:
            roster.append(disp)
        if tok == "ZA":
            nv["za"].append(disp)
        elif tok in ("NIE", "PRZECIW"):
            nv["przeciw"].append(disp)
        elif tok.startswith("WSTRZYMUJ"):
            nv["wstrzymal_sie"].append(disp)
        elif tok.startswith("NIEOBECN"):
            nv["nieobecny"].append(disp)
        else:  # OBECNY / OBECNA
            nv["obecny_nieglosujacy"].append(disp)
    ok = (agg.get("za") is None or agg["za"] == len(nv["za"])) and \
         (agg.get("przeciw") is None or agg["przeciw"] == len(nv["przeciw"])) and \
         (agg.get("wstrzymal_sie") is None or agg["wstrzymal_sie"] == len(nv["wstrzymal_sie"]))
    if not ok:
        print(f"  [warn] agregaty nie zgadzają się: {title[:60]} agg={agg} "
              f"parsed za={len(nv['za'])} przeciw={len(nv['przeciw'])} wstrz={len(nv['wstrzymal_sie'])}",
              file=sys.stderr)
        return None, roster
    vote = {"title": title or "Głosowanie", "date": vdate, "named_votes": nv,
            "total_za": agg.get("za"), "total_przeciw": agg.get("przeciw"),
            "total_wstrzymal": agg.get("wstrzymal_sie")}
    return vote, roster


def main() -> int:
    ap = argparse.ArgumentParser(prog="Radoskop Słupca (eurzad.finn.pl ruigr)")
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
        det = _rpc("routeRejestryServiceManager2", [json.dumps(str(it["id"]))])
        zals = (det or {}).get("zalaczniki") or []
        if not zals:
            print(f"  {it['date']}: BRAK zalacznika ({it['temat'][:50]})")
            continue
        nv_count = 0
        for zal in zals:
            try:
                data = _get_pdf(zal["id"])
                vote, rr = parse_vote_pdf(data)
            except Exception as e:
                print(f"  [warn] {it['date']} zal {zal['id']}: {e}", file=sys.stderr)
                continue
            for n in rr:
                if n not in roster:
                    roster.append(n)
            if vote:
                vote["session_date"] = it["date"]
                vote["title"] = vote["title"] or (zal.get("nazwa") or "Głosowanie")
                all_votes.append(vote)
                nv_count += 1
            time.sleep(0.25)
        sessions.append({"date": it["date"], "number": "", "title": it["temat"],
                         "vote_count": nv_count})
        print(f"  {it['date']}: {nv_count}/{len(zals)} głosowań")

    sessions.sort(key=lambda s: s["date"])
    votes_out = []
    for i, v in enumerate(all_votes):
        votes_out.append({
            "id": f"slp-{i+1}",
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
                               if any(c["name"] in v["named_votes"].get(k, [])
                                      for k in ("za", "przeciw", "wstrzymal_sie")))
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
    data = {"city": "Słupca", "scraped_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
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
