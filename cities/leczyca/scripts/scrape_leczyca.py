#!/usr/bin/env python3
"""Radoskop scraper — Łęczyca (platforma radni.info, JSON API per-radny).

Źródło: https://leczyca.radni.info — API:
  GET /api/session/ended                 -> wszystkie zakończone sesje (users[] = radni)
  GET /api/session/{id}/agenda           -> punkty porządku + votes[] + resolutions[] (PDF)
  GET /api/session/vote/{voteId}/result  -> imienny wynik (resultDetails: isVoted/isOpposed/isAbsteined/isAbsent)
"""
import argparse, json, re, ssl, sys, time, urllib.request
from pathlib import Path
from collections import OrderedDict

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Radoskop/1.0 (info@radoskop.eu)",
      "Accept": "application/json"}
CTX = ssl.create_default_context(); CTX.check_hostname = False; CTX.verify_mode = ssl.CERT_NONE
BASE = "https://leczyca.radni.info/"
IX_START = "2024-05-07"


def fetch(path, retries=4):
    url = BASE + path
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=25, context=CTX) as r:
                return json.loads(r.read())
        except Exception as e:
            last = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"fetch failed {url}: {last}")


def norm_topic(t):
    t = re.sub(r'\s+', ' ', (t or '').strip())
    t = re.sub(r'^Podjęcie uchwały w sprawie ', '', t)
    t = re.sub(r'^Podjęcie uchwały ', '', t)
    return t[:300]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--city-dir", required=True)
    args = ap.parse_args()
    docs = Path(args.city_dir) / "docs"
    docs.mkdir(parents=True, exist_ok=True)

    sessions_raw = fetch("api/session/ended")
    ix = [s for s in sessions_raw if (s.get("realStartTime") or s.get("startTime") or "")[:10] >= IX_START]
    # tylko sesje plnarne Rady Miejskiej (portal ma tez 'Wspólne Posiedzenie Komisji' w API)
    def is_plenary(s):
        t = (s.get("title") or "")
        if not re.search(r"Rady Miejskiej", t):
            return False
        return not re.search(r"Komisj|Wsp\u00f3lne|wsp\u00f3lne|konwent|Konwent|spotkanie|narada|zebranie", t)
    ix = [s for s in ix if is_plenary(s)]
    ix.sort(key=lambda s: s.get("realStartTime") or s.get("startTime"))
    print(f"[leczyca] sesje plnarne IX: {len(ix)} (z {len(sessions_raw)})")

    # roster zbudowany z unionu nazwisk w wynikach imiennych (users[] zawiera urzędników)
    roster = OrderedDict()  # name -> first-seen (uprawnieni are the actual councilors in resultDetails)

    votes_out = []
    sessions_out = []
    vote_seq = 0
    total_named = 0
    for s in ix:
        sid = s["id"]
        date = (s.get("realStartTime") or s.get("startTime"))[:10]
        try:
            agenda = fetch(f"api/session/{sid}/agenda")
        except Exception as e:
            print(f"  ! agenda {sid}: {e}", file=sys.stderr)
            continue
        n_votes_session = 0
        for el in agenda:
            for v in (el.get("votes") or []):
                vid = v["id"]
                vote_seq += 1
                topic = norm_topic(v.get("name") or el.get("title"))
                res = None
                try:
                    res = fetch(f"api/session/vote/{vid}/result")
                except Exception as e:
                    print(f"  ! vote {vid}: {e}", file=sys.stderr)
                if not res or not res.get("result") or not res.get("isDone"):
                    continue
                rd = res["result"].get("resultDetails") or []
                if not rd:
                    continue
                za, proc, wstrz, nieob = [], [], [], []
                for d in rd:
                    nm = re.sub(r"\s+", " ", d.get("councilor", "")).strip()
                    if not nm:
                        continue
                    roster[nm] = roster.get(nm, 0) + 1
                    if d.get("isVoted"): za.append(nm)
                    elif d.get("isOpposed"): proc.append(nm)
                    elif d.get("isAbsteined"): wstrz.append(nm)
                    elif d.get("isAbsent"): nieob.append(nm)
                # walidacja vs agregaty
                agg = res["result"]
                if len(za) != agg.get("votedCount", len(za)) or len(proc) != agg.get("opposedCount", len(proc)):
                    print(f"  ! MISMATCH vote {vid}: za={len(za)} vs {agg.get('votedCount')}, przeciw={len(proc)} vs {agg.get('opposedCount')}")
                    continue
                if not za and not proc and not wstrz:
                    continue
                # numer uchwały + URL PDF
                resno = ""
                pdf_url = ""
                for rr in (el.get("resolutions") or []):
                    resno = re.sub(r"\s*", "", rr.get("number") or "")
                    for a in (rr.get("attachments") or []):
                        if a.get("isResolutionMain") and a.get("attachmentId"):
                            pdf_url = f"https://login.radni.info/{a.get('serverPath','')}/{a['attachmentId']}"
                if not resno:
                    resno = re.sub(r"\s*", "", v.get("number") or "")
                vote_seq_id = f"L-{date}-{vote_seq:04d}"
                votes_out.append({
                    "id": vote_seq_id,
                    "session_date": date,
                    "session_number": (s.get("number") or ""),
                    "topic": topic,
                    "resolution_number": resno,
                    "source_url": pdf_url or "https://leczyca.radni.info/posiedzenia",
                    "named_votes": {"za": za, "przeciw": proc, "wstrzymal_sie": wstrz},
                    "counts": {"za": len(za), "przeciw": len(proc), "wstrzymal_sie": len(wstrz),
                               "nieobecni": len(nieob), "uprawnieni": agg.get("allowedCount")},
                })
                total_named += len(za) + len(proc) + len(wstrz)
                n_votes_session += 1
        sessions_out.append({
            "date": date,
            "number": s.get("number") or str(sid),
            "label": re.sub(r"\s+", " ", (s.get("title") or f"Sesja {sid}")).strip(),
            "vote_count": n_votes_session,
        })
        print(f"  {date} {s.get('number')}: {n_votes_session} glosowan imiennych")
        time.sleep(0.25)

    # councilors list z frekwencją
    def slugify(nm):
        import unicodedata
        s = unicodedata.normalize("NFKD", nm)
        s = "".join(c for c in s if not unicodedata.combining(c))
        return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")

    per_c = {n: {"za": 0, "przeciw": 0, "wstrzymal_sie": 0, "votes": 0} for n in roster}
    for v in votes_out:
        for k in ("za", "przeciw", "wstrzymal_sie"):
            for nm in v["named_votes"][k]:
                if nm in per_c:
                    per_c[nm][k] += 1
                    per_c[nm]["votes"] += 1
    councilors = []
    for nm in roster:
        st = per_c[nm]
        particip = st["votes"]
        councilors.append({
            "id": nm, "name": nm, "slug": slugify(nm), "club": "",
            "frekwencja": round(100.0 * particip / max(len(votes_out), 1), 1),
            "za": st["za"], "przeciw": st["przeciw"], "wstrzymal_sie": st["wstrzymal_sie"],
        })
    councilor_index = [c["name"] for c in councilors]

    kad = {
        "id": "2024-2029", "label": "IX kadencja (2024–2029)",
        "sessions": sorted(sessions_out, key=lambda x: x["date"], reverse=True),
        "votes": votes_out,
        "councilor_index": councilor_index,
        "councilors": councilors,
        "total_councilors": len(councilors),
        "total_votes": len(votes_out),
        "similarity_top": [], "similarity_bottom": [],
    }
    (docs / "kadencja-2024-2029.json").write_text(json.dumps(kad, ensure_ascii=False, indent=1), encoding="utf-8")

    profiles = []
    for c in councilors:
        profiles.append({
            "name": c["name"], "slug": c["slug"], "club": "", "role": "",
            "photo_url": "", "bio": "", "email": "", "social_links": {},
            "voting": None,
            "kadencje": {"2024-2029": {
                "club": "", "has_voting_data": True, "role": "",
                "frekwencja": c["frekwencja"], "aktywnosc": c["frekwencja"],
                "zgodnosc_z_klubem": None, "rebellion_count": 0,
                "votes": per_c[c["name"]]["votes"],
            }},
        })
    from datetime import datetime, timezone
    (docs / "profiles.json").write_text(json.dumps(
        {"scraped_at": datetime.now(timezone.utc).isoformat(), "profiles": profiles, "total": len(profiles)},
        ensure_ascii=False, indent=1), encoding="utf-8")

    data = {"generated": datetime.now(timezone.utc).isoformat(),
            "default_kadencja": "2024-2029",
            "kadencje": [{"id": "2024-2029", "label": "IX kadencja (2024–2029)"}]}
    (docs / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[leczyca] GOTOWE: sesji={len(sessions_out)} glosowan={len(votes_out)} radnych={len(councilors)} nazw-glosow={total_named}")


if __name__ == "__main__":
    main()
