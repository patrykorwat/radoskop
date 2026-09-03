#!/usr/bin/env python3
"""Radoskop Świdnik — scraper eSesja WCAG-ASP (portal `/wcag/posiedzenie.asp?k=...`).

Wariant eSesja "Portal Mieszkańca" serwowany przez stare strony ASP pod /wcag/:
  /wcag/            — archiwum posiedzeń (linki /wcag/posiedzenie.asp?k=UUID)
  /wcag/posiedzenie.asp?k=... — porządek + linki /wcag/glosowanie.asp?id=N&k=HASH
  /wcag/glosowanie.asp?id=N&k=HASH — "Wyniki głosowania imiennego w sprawie: X"
      + "ZA: n, PRZECIW: n, WSTRZYMUJĘ SIĘ: n, BRAK GŁOSU: n, NIEOBECNI: n"
      + "Lista imienna" z blokami ZA/PRZECIW/WSTRZYMUJĘ SIĘ/NIEOBECNI i listami nazwisk.

Output: kadencja-2024-2029.json (named_votes za/przeciw/wstrzymal_sie po IMIENIACH),
profiles.json, data.json — format Radoskop (jak lib_esesja).
IX kadencja start 2024-05-07. Dodane 2026-09-03 (cron do 500 miast).
"""
import argparse
import datetime as _dt
import hashlib
import json
import re
import ssl
import sys
import time
import unicodedata
from pathlib import Path

BASE = "https://swidnik.esesja.pl"
KAD_ID = "2024-2029"
KAD_LABEL = "IX kadencja (2024–2029)"
KAD_START = "2024-05-07"

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36"}

MONTHS = {m: i for i, m in enumerate(
    ["stycznia", "lutego", "marca", "kwietnia", "maja", "czerwca", "lipca",
     "sierpnia", "września", "października", "listopada", "grudnia"], 1)}

_cache_dir: Path | None = None


def _cache_path(url: str) -> Path | None:
    if _cache_dir is None:
        return None
    return _cache_dir / (hashlib.md5(url.encode()).hexdigest() + ".html")


def fetch(url: str, use_cache: bool = True) -> str:
    cp = _cache_path(url)
    if cp is not None and cp.is_file() and use_cache:
        return cp.read_text(encoding="utf-8", errors="replace")
    raw = b""
    for attempt in range(3):
        try:
            import urllib.request
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=30, context=CTX) as r:
                raw = r.read()
            break
        except Exception:
            if attempt == 2:
                raise
            time.sleep(1.5 * (attempt + 1))
    text = None
    m = re.search(rb"charset=([\w-]+)", raw[:2000], re.I)
    encs = ([m.group(1).decode()] if m else []) + ["utf-8", "windows-1250"]
    for enc in encs:
        try:
            text = raw.decode(enc)
            break
        except Exception:
            continue
    if text is None:
        text = raw.decode("utf-8", "replace")
    if cp is not None:
        cp.parent.mkdir(parents=True, exist_ok=True)
        cp.write_text(text, encoding="utf-8")
    time.sleep(0.35)
    return text


def slugify(name: str) -> str:
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()
    return s


def strip_tags(html: str) -> str:
    t = re.sub(r"<script.*?</script>", " ", html, flags=re.S | re.I)
    t = re.sub(r"<style.*?</style>", " ", t, flags=re.S | re.I)
    t = re.sub(r"<[^>]+>", "\n", t)
    t = (t.replace("&nbsp;", " ").replace("&amp;", "&")
          .replace("&#171;", "«").replace("&#187;", "»")
          .replace("&quot;", '"').replace("&#39;", "'").replace("&oacute;", "ó"))
    return t


def scrape_sessions():
    """Return [{date, title, url, votes:[{url}]}] for IX kadencja plenary sessions."""
    t = fetch(BASE + "/wcag/")
    sessions = {}
    for m in re.finditer(r'href="(/wcag/posiedzenie\.asp\?k=[0-9a-f-]+)"[^>]*>([^<]+)</a>', t):
        url, txt = BASE + m.group(1), m.group(2).strip()
        dm = re.search(r"w dniu (\d{1,2}) (\w+) (\d{4})", txt)
        if not dm:
            continue
        mon = MONTHS.get(dm.group(2).lower())
        if not mon:
            continue
        iso = f"{dm.group(3)}-{mon:02d}-{int(dm.group(1)):02d}"
        if iso < KAD_START:
            continue
        if url not in sessions or iso > sessions[url]["date"]:
            sessions[url] = {"date": iso, "title": txt, "url": url}
    return sorted(sessions.values(), key=lambda s: s["date"], reverse=True)


VOTE_HDR = re.compile(
    r"Wyniki głosowania imiennego w sprawie:\s*\n?(.{0,240}?)\s*\nZA:\s*\|?(\d+)\s*,?\s*PRZECIW:\s*\|?(\d+)\s*,?\s*WSTRZYMUJ[ĘE] SI[ĘE]:\s*\|?(\d+)\s*,?\s*BRAK G\u0141OSU:\s*\|?(\d+)\s*,?\s*NIEOBECNI:\s*\|?(\d+)",
    re.S)

BLOCKS = [("ZA", "za"), ("PRZECIW", "przeciw"), ("WSTRZYMUJĘ SIĘ", "wstrzymal_sie"),
          ("WSTRZYMUJE SIĘ", "wstrzymal_sie"), ("BRAK GŁOSU", "brak_glosu"),
          ("NIEOBECNI", "nieobecnI")]
NAME_RE = re.compile(r"^[A-ZŁŚŻŹĆŃÓĄĘ][\wŁŚŻŹĆŃÓĄĘłśżźćńóąę-]+( [A-ZŁŚŻŹĆŃÓĄĘłśżźćńóąę'\-]+)+$")


def parse_vote_page(html: str) -> dict | None:
    txt = strip_tags(html)
    txt = re.sub(r"[ \t]+", " ", txt)
    m = VOTE_HDR.search(txt)
    if not m:
        return None
    title = re.sub(r"\s+", " ", m.group(1)).strip()
    za_n, pr_n, ws_n, br_n, nb_n = (int(m.group(i)) for i in range(2, 7))
    # Lista imienna blocks: after "Lista imienna"
    li = txt.find("Lista imienna")
    body = txt[li:] if li >= 0 else txt
    # split into labeled blocks: label occurrences like "\nZA\n (19)\n"
    pat = re.compile(r"\n\s*(ZA|PRZECIW|WSTRZYMUJ[EĘ] SI[ĘE]|BRAK G\u0141OSU|NIEOBECNI)\s*\n?\s*\((\d+)\)")
    marks = list(pat.finditer(body))
    blocks: dict[str, list[str]] = {}
    names_re = re.compile(r"^[A-Z][\wŁŚŻŹĆŃÓĄĘłśżźćńóąę-]+( [A-Z][A-Za-zŁŚŻŹĆŃÓĄĘłśżźćńóąę'\-]+)+$")
    for i, mk in enumerate(marks):
        label = mk.group(1)
        end = marks[i + 1].start() if i + 1 < len(marks) else len(body)
        seg = body[mk.end():end]
        names = []
        for line in seg.split("\n"):
            line = line.strip()
            if not line or len(line) > 60:
                continue
            if names_re.match(line):
                names.append(line)
        key = {"ZA": "za", "PRZECIW": "przeciw", "BRAK GŁOSU": "brak_glosu",
               "NIEOBECNI": "nieobecni"}.get(label)
        if key is None:
            key = "wstrzymal_sie"
        blocks.setdefault(key, [])
        blocks[key].extend(names)
    exp = {"za": za_n, "przeciw": pr_n, "wstrzymal_sie": ws_n,
           "brak_glosu": br_n, "nieobecni": nb_n}
    ok = all(len(blocks.get(k, [])) == v for k, v in exp.items())
    if not ok:
        return {"_bad": True, "title": title, "exp": exp,
                "got": {k: len(v) for k, v in blocks.items()}}
    return {"title": title, "za": blocks["za"], "przeciw": blocks["przeciw"],
            "wstrzymal_sie": blocks["wstrzymal_sie"],
            "brak_glosu": blocks["brak_glosu"], "nieobecni": blocks["nieobecni"],
            "counts": [za_n, pr_n, ws_n]}


def main() -> int:
    global _cache_dir
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", default="docs/data.json")
    ap.add_argument("--profiles", default="docs/profiles.json")
    ap.add_argument("--cache-dir", default=None)
    ap.add_argument("--max-sessions", type=int, default=0)
    args = ap.parse_args()
    _cache_dir = Path(args.cache_dir) if args.cache_dir else None

    sessions = scrape_sessions()
    print(f"[swidnik] sesje IX kad.: {len(sessions)}")
    if args.max_sessions:
        sessions = sessions[: args.max_sessions]

    votes_out = []
    councilors: dict[str, None] = {}
    sess_out = []
    bad = 0
    for si, s in enumerate(sessions, 1):
        html = fetch(s["url"])
        vurls = list(dict.fromkeys(
            m.group(1) for m in re.finditer(
                r'href="(/wcag/glosowanie\.asp\?id=\d+&amp;k=[0-9a-f]+|/wcag/glosowanie\.asp\?id=\d+&k=[0-9a-f]+)"', html)))
        n_ok = 0
        for vi, vu in enumerate(vurls):
            vu = vu.replace("&amp;", "&")
            vh = fetch(BASE + vu)
            pv = parse_vote_page(vh)
            if pv is None:
                continue
            if pv.get("_bad"):
                bad += 1
                print(f"  [bad] {s['date']} v{vi}: exp={pv['exp']} got={pv['got']} :: {pv['title'][:60]}")
                continue
            n_ok += 1
            vid = f"{s['date']}-v{vi+1}"
            for cat in ("za", "przeciw", "wstrzymal_sie", "brak_glosu", "nieobecni"):
                for nm in pv[cat]:
                    councilors.setdefault(nm, None)
            votes_out.append({
                "id": vid, "date": s["date"], "session_date": s["date"],
                "title": pv["title"], "url": BASE + vu,
                "result": "przyjete" if pv["counts"][0] > pv["counts"][1] else "odrzucone",
                "named_votes": {"za": pv["za"], "przeciw": pv["przeciw"],
                                 "wstrzymal_sie": pv["wstrzymal_sie"]},
                "counts": {"za": pv["counts"][0], "przeciw": pv["counts"][1],
                            "wstrzymal_sie": pv["counts"][2],
                            "nieobecni": len(pv["nieobecni"])},
            })
        sess_out.append({"date": s["date"], "number": s["date"],
                          "label": s["title"][:120], "vote_count": n_ok})
        print(f"  [{si}/{len(sessions)}] {s['date']}: {len(vurls)} głosowań, {n_ok} zaparsowanych")

    names = sorted(councilors)
    cad = {
        "id": KAD_ID, "label": KAD_LABEL,
        "sessions": sess_out,
        "votes": votes_out,
        "councilor_index": names,
        "councilors": [{"name": n, "slug": slugify(n), "club": "",
                         "frekwencja": None, "aktywnosc": None,
                         "zgodnosc_z_klubem": None} for n in names],
        "total_councilors": len(names),
        "total_votes": len(votes_out),
        "similarity_top": [], "similarity_bottom": [],
    }
    prof = {"scraped_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            "profiles": [{"name": n, "slug": slugify(n), "club": "",
                           "role": "", "photo_url": "", "bio": "", "email": "",
                           "social_links": {}, "voting": None,
                           "kadencje": {KAD_LABEL: {"club": "", "has_voting_data": True}}}
                          for n in names],
            "total": len(names)}
    data = {"city": "Świdnik", "kadencje": [{"id": KAD_ID, "label": KAD_LABEL}],
            "scraped_at": prof["scraped_at"]}

    outp = Path(args.output)
    outp.parent.mkdir(parents=True, exist_ok=True)
    (outp.parent / f"kadencja-{KAD_ID}.json").write_text(
        json.dumps(cad, ensure_ascii=False, indent=1), encoding="utf-8")
    Path(args.profiles).write_text(json.dumps(prof, ensure_ascii=False, indent=1), encoding="utf-8")
    outp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[swidnik] OK: {len(sess_out)} sesji, {len(votes_out)} głosowań, {len(names)} radnych, bad={bad}")
    return 0 if votes_out else 1


if __name__ == "__main__":
    raise SystemExit(main())
