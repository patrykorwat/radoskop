#!/usr/bin/env python3
"""Radoskop Hajnówka — scraper głosowań imiennych z BIP bip.hajnowka.pl (platforma siteor).

Źródło: kategoria "Wyniki głosowania" -> strony roczne /wyniki-glosowania---{rok}.
Jeden artykuł = jedna sesja ("Wykaz/Wyniki głosowań Radnych na {RZYMSKA} sesji ...
z dnia|w dniu D miesiąca RRR r."); w artykule załącznik PDF na fs.siteor.com
("Wykaz głosowań Radnych na ... sesji ... .pdf").

PDF (eksport posiedzenia.pl): per głosowanie blok
  <tytuł punktu...>
  głosowanie <tytuł>
  jednostka Rada Miasta Hajnówka
  wynik Głosowanie zakończone wynikiem: ...
  data D miesiąca RRR r. czas ...
  typ głosowanie jawne imienne ...
  Podsumowanie
  ZA <n> <proc> % pula głosów <N> -
  PRZECIW <n> ...
  WSTRZYMAŁO SIĘ <n> ...
  Wyniki imienne
  lp nazwisko imię głos
  1 Bołtryk Marcin ZA
  ...
  12 Siegień Emilia nieobecna
Parser: linie; walidacja — liczby ZA/PRZECIW/WSTRZ = licznik z Podsumowanie,
inaczej głosowanie odrzucone.
"""

import argparse
import json
import re
import ssl
import sys
import time
import urllib.parse
import urllib.request
from collections import OrderedDict
from datetime import datetime
from pathlib import Path

BASE = "https://bip.hajnowka.pl"
YEAR_PAGES = {2024: "wyniki-glosowania---2024", 2025: "wyniki-glosowania---2025",
              2026: "wyniki-glosowania---2026"}
KAD_ID = "2024-2029"
IX_START = "2024-05-07"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Radoskop/1.0 (info@radoskop.eu)"}

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

MONTHS = {
    "stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5, "czerwca": 6,
    "lipca": 7, "sierpnia": 8, "września": 9, "pazdziernika": 10, "października": 10,
    "listopada": 11, "grudnia": 12,
}
ROMAN_MAP = [("LXXX", 80), ("LXX", 70), ("LX", 60), ("L", 50), ("XL", 40), ("XXX", 30),
             ("XX", 20), ("IX", 9), ("VIII", 8), ("VII", 7), ("V", 5), ("IV", 4),
             ("III", 3), ("II", 2), ("I", 1)]

VOTE_TOKENS = {
    "ZA": "za",
    "PRZECIW": "przeciw",
    "WSTRZYMAŁ SIĘ": "wstrzymal_sie",
    "WSTRZYMAŁA SIĘ": "wstrzymal_sie",
    "WSTRZYMAŁO SIĘ": "wstrzymal_sie",
}
ABSENT_TOKENS = {"nieobecna", "nieobecny", "nieobecni"}

NAME_LINE = re.compile(
    r"^\s*(\d{1,2})\s+([A-ZŁŚŻŹĆĄĘŃÓ][\wŁŚŻŹĆĄĘŃóśłżźćądąęń\-\.]+(?:\s+[\wŁŚŻŹĆĄĘŃóśłżźćądąęń\-\.\(\)]+){1,4}?)\s+(ZA|PRZECIW|WSTRZYMA[AŁÓŁ]*\s+SI[EĘ]|nieobecna|nieobecny|nieobecni|nie\s*g[łl]osowa[łl](a)?\*?|brak głosu|nie klasyfikuje się)\s*$",
    re.IGNORECASE,
)
ART_LINK = re.compile(r'href="(/article/[^"#]+?)(?:#[^"]*)?"', re.I)
ATTACH = re.compile(r'href="(https://fs\.siteor\.com/[^"]+?\.(?:pdf|odt)\?[^"]+|https://fs\.siteor\.com/[^"]+?\.(?:pdf|odt))"', re.I)
VOTE_HDR = re.compile(r"^[Gg]\s*[łlL]osowanie\s+(?:\d+\s+)?(\S.*)$")
ODT_HDR = re.compile(r"^G[ŁŁ]OSOWANIE\s+\d+\s*$")
ODT_COUNT = re.compile(r"(Za|Przeciw|Wstrzyma[łl]\s*si[ęe]|Brak\s*udzia[łl]u)\s*\((\d+)\)", re.I)
ODT_NAME = re.compile(r"^[A-ZŁŚŻŹĆĄĘŃÓ][\wŁŚŻŹĆĄĘŃóśłżźćądąęń\-\.]+(?:\s+[A-ZŁŚŻŹĆĄĘŃ][\wŁŚŻŹĆĄĘŃóśłżźćądąęń\-\.]+){1,4}$")
DATE_IN_TITLE = re.compile(r"(?:z dnia|w dniu|z sesji w dniu)\s+(\d{1,2})\.?\s+(\p{L}+)?".replace(r"\p{L}+", r"[a-zżłóąęśńć]+") + r"?\s*,?\s*(\d{4})", re.I)
DATE_IN_TITLE2 = re.compile(r"(?:z dnia|w dniu)\s+(\d{1,2})\s+([a-zżłóąęśńć]+)\.?\s+(\d{4})", re.I)
ROMAN_RE = re.compile(r"(?<![A-Za-z])((?:M{0,4}(?:CM|CD|D?C{0,3})(?:XC|XL|L?X{0,3})(?:IX|IV|V?I{0,3}))[MDCLXVI])(?![A-Za-z])", re.I)


def roman_to_int(s: str) -> int:
    s = s.upper()
    total, i = 0, 0
    for sym, val in ROMAN_MAP:
        while s[i:i + len(sym)] == sym:
            total += val
            i += len(sym)
    return total if i == len(s) and total else 0


def fetch(url: str, timeout: int = 40) -> bytes:
    last = None
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"fetch failed {url}: {last}")


def session_date_from_slug(slug: str) -> str | None:
    parts = slug.split("-")
    # ends like: ...-z-dnia-24-czerwca-2026-r  /  -w-dniu-7-maja-2024-r / -26-listopada-2025r
    m = re.search(r"(?:z-dnia|w-dniu)-(\d{1,2})-([a-zżłóąęśńć]+)-?(\d{4})", slug)
    if m:
        dd, mon, yyyy = int(m.group(1)), m.group(2), m.group(3)
        if mon in MONTHS and 1 <= dd <= 31:
            return f"{yyyy}-{MONTHS[mon]:02d}-{dd:02d}"
    m = re.search(r"(\d{1,2})-([a-zżłóąęśńć]+)-(\d{4})", slug)
    if m and m.group(2) in MONTHS:
        return f"{m.group(3)}-{MONTHS[m.group(2)]:02d}-{int(m.group(1)):02d}"
    return None


def parse_pdf_text(text: str) -> tuple[list[dict], list[str]]:
    votes = []
    roster: "OrderedDict[str, None]" = OrderedDict()
    lines = text.split("\n")
    i = 0
    n = len(lines)
    while i < n:
        ln = lines[i].strip()
        vh = VOTE_HDR.match(ln)
        if vh:
            title = vh.group(1).strip()
            # find counts within the next ~16 lines (before 'Wyniki imienne')
            counts: dict[str, int] = {}
            j = i + 1
            found_hdr = False
            while j < n and j < i + 18:
                s = lines[j].strip()
                if VOTE_HDR.match(s):
                    break
                m = re.match(r"^(ZA|PRZECIW|WSTRZYMA[LÓŁ]O?)(?:\s+SI[EĘ])?\s+(\d+)\s", s, re.IGNORECASE)
                if m:
                    g = m.group(1).upper()
                    key = "za" if g == "ZA" else ("przeciw" if g == "PRZECIW" else "wstrzymal_sie")
                    counts[key] = int(m.group(2))
                if "Wyniki imienne" in s:
                    found_hdr = True
                    break
                j += 1
            if not found_hdr:
                i += 1
                continue
            k = j + 1
            # skip header 'lp nazwisko imię głos'
            if k < n and lines[k].strip().lower().startswith("lp"):
                k += 1
            vote = {"title": "", "za": [], "przeciw": [], "wstrzymal_sie": [], "nieobecni": []}
            while k < n:
                s = lines[k].strip()
                if not s:
                    k += 1
                    continue
                mm = NAME_LINE.match(s)
                if mm:
                    person = re.sub(r"\s+", " ", mm.group(2)).strip()
                    tok = mm.group(3).strip()
                    key = VOTE_TOKENS.get(tok.upper())
                    if tok.lower() in ABSENT_TOKENS:
                        vote["nieobecni"].append(person)
                    elif key:
                        vote[key].append(person)
                    else:
                        # nieznany token — traktuj jak brak głosu, nie porzucaj blok
                        pass
                    roster.setdefault(person, None)
                    k += 1
                    continue
                break
            # tytuł: z linii 'głosowanie' albo wcześniejszych (numerowany punkt)
            vote["title"] = re.sub(r"\s+", " ", title)
            ok = (
                len(counts) == 3
                and len(vote["za"]) == counts["za"]
                and len(vote["przeciw"]) == counts["przeciw"]
                and len(vote["wstrzymal_sie"]) == counts["wstrzymal_sie"]
                and (vote["za"] or vote["przeciw"] or vote["wstrzymal_sie"])
            )
            if ok:
                votes.append(vote)
            else:
                print(f"  [skip] liczniki {counts} != ZA={len(vote['za'])}/PRZ={len(vote['przeciw'])}/WSTRZ={len(vote['wstrzymal_sie'])} | {vote['title'][:60]}")
            i = k
            continue
        i += 1
    return votes, list(roster.keys())


def parse_odt_text(text: str) -> tuple[list[dict], list[str]]:
    """Format ODT (Hajnówka 2024): bloki 'GŁOSOWANIE N' / tytuł / Rodzaj.. /
    Rozkład głosów / Za (n) / Przeciw (n) / Wstrzymał się (n) / Brak udziału (n)
    potem posegregowane alfabetycznie nazwiska: ZA, potem PRZECIW, potem WSTRZ,
    potem BRAK-UDZIAŁU (wg liczników)."""
    votes = []
    roster: "OrderedDict[str, None]" = OrderedDict()
    lines = [l.strip().replace("\xa0", " ") for l in text.split("\n")]
    n = len(lines)
    i = 0
    while i < n:
        if ODT_HDR.match(lines[i]):
            k = i + 1
            # title = lines until 'Rodzaj głosowania'
            tparts = []
            while k < n and not lines[k].startswith("Rodzaj głosowania"):
                if ODT_HDR.match(lines[k]):
                    break
                tparts.append(lines[k])
                k += 1
            title = re.sub(r"\s+", " ", " ".join(tparts)).strip()
            counts: dict[str, int] = {}

            def _absorb(s: str) -> int:
                got = 0
                for cm in ODT_COUNT.finditer(s):
                    lab = cm.group(1).lower()
                    key = ("za" if lab.startswith("za") else
                           "przeciw" if lab.startswith("przeciw") else
                           "wstrzymal_sie" if lab.startswith("wstrzyma") else "brak_udzialu")
                    counts[key] = int(cm.group(2))
                    got += 1
                return got

            while k < n and not ODT_HDR.match(lines[k]):
                _absorb(lines[k])
                if lines[k] == "Rozkład głosów":
                    break
                k += 1
            k += 1  # za 'Rozkład głosów'
            # consume count lines (mogą być w jednej linii: 'Za (15) Przeciw (0) ...')
            while k < n and _absorb(lines[k]) > 0:
                k += 1
            total = sum(counts.get(x, 0) for x in ("za", "przeciw", "wstrzymal_sie", "brak_udzialu"))
            names = []
            while k < n and len(names) < total and ODT_NAME.match(lines[k]):
                names.append(re.sub(r"\s+", " ", lines[k]))
                k += 1
            za_n = counts.get("za", 0)
            pr_n = counts.get("przeciw", 0)
            ws_n = counts.get("wstrzymal_sie", 0)
            vote = {
                "title": title,
                "za": names[:za_n],
                "przeciw": names[za_n:za_n + pr_n],
                "wstrzymal_sie": names[za_n + pr_n:za_n + pr_n + ws_n],
                "nieobecni": [],
            }
            ok = (len(counts) >= 3 and len(names) == total
                  and (vote["za"] or vote["przeciw"] or vote["wstrzymal_sie"]))
            if ok:
                # roster tylko ze zwalidowanych blokow (sklejone nazwiska z blokow
                # odrzuconych nie zatruwaja skladu rady)
                for nm in vote["za"] + vote["przeciw"] + vote["wstrzymal_sie"]:
                    roster.setdefault(nm, None)
                votes.append(vote)
            elif counts:
                print(f"  [skip-odt] {counts} names={len(names)} | {title[:60]}")
            i = k
            continue
        i += 1
    return votes, list(roster.keys())


def main() -> int:
    ap = argparse.ArgumentParser(description="Radoskop Hajnówka (bip.hajnowka.pl) scraper")
    ap.add_argument("--city-dir", required=True)
    ap.add_argument("--cache-dir", default="/cache/hajnowka")
    args = ap.parse_args()
    city_dir = Path(args.city_dir)
    cache = Path(args.cache_dir)
    cache.mkdir(parents=True, exist_ok=True)
    out_dir = city_dir / "docs"
    out_dir.mkdir(parents=True, exist_ok=True)

    import pdfplumber

    seen_dates: dict[str, dict] = {}
    all_names: "OrderedDict[str, None]" = OrderedDict()
    for year, page in sorted(YEAR_PAGES.items()):
        html = fetch(f"{BASE}/{page}").decode("utf-8", "replace")
        slugs = []
        seen_s = set()
        for m in ART_LINK.finditer(html):
            s = m.group(1).strip("/")
            if s in seen_s or "glosowan" not in s:
                continue
            seen_s.add(s)
            slugs.append(s)
        print(f"[{year}] artykułów: {len(slugs)}")
        for slug in slugs:
            date = session_date_from_slug(slug)
            if not date or date < IX_START:
                continue
            num = 0
            rom = None
            head = slug.split("-z-dnia")[0].split("-w-dniu")[0]
            head = head.replace("sesja", " sesja ").replace("sesji", " sesji ").replace("rady", " rady")
            m = ROMAN_RE.search(head)
            if m:
                num = roman_to_int(m.group(1))
                rom = m.group(1).upper()
            art_html = fetch(f"{BASE}/{slug}").decode("utf-8", "replace")
            am = ATTACH.search(art_html)
            if not am:
                print(f"  [warn] brak PDF: {slug[:70]}")
                continue
            pdf_url = am.group(1)
            fn = cache / (re.sub(r"[^\w]", "_", pdf_url.split("?")[0].split("/")[-1])[:120] + ".bin")
            raw = fn.read_bytes() if fn.is_file() and fn.stat().st_size > 1000 else None
            if raw is None:
                raw = fetch(pdf_url)
                fn.write_bytes(raw)
                time.sleep(0.6)
            if pdf_url.split("?")[0].lower().endswith(".odt"):
                xml = __import__("zipfile").ZipFile(__import__("io").BytesIO(raw)).read("content.xml").decode("utf-8")
                text = re.sub(r"\n+", "\n", re.sub(r"<[^>]+>", "\n", xml))
                votes, names = parse_odt_text(text)
            else:
                with pdfplumber.open(__import__("io").BytesIO(raw)) as pdf:
                    text = "\n".join((p.extract_text() or "") for p in pdf.pages)
                votes, names = parse_pdf_text(text)
                if not votes and "Rozkład głosów" in text:
                    # PDF w układzie 'GŁOSOWANIE N' (jak ODT) — spróbuj parsera blokowego
                    votes, names = parse_odt_text(text)
            for nm in names:
                all_names.setdefault(nm, None)
            if not votes:
                print(f"  [warn] 0 zwalidowanych głosowań: {date} {slug[:60]}")
                continue
            prev = seen_dates.get(date)
            if prev and len(prev["votes"]) >= len(votes):
                continue
            seen_dates[date] = {
                "date": date,
                "number": rom if rom else date,
                "label": f"Sesja {rom if rom else date} Rady Miasta w Hajnówce ({date})",
                "votes": votes,
                "source_url": f"{BASE}/{slug}",
            }
            print(f"  {date}: {len(votes)} głosowań")

    sessions = sorted(seen_dates.values(), key=lambda s: s["date"])
    for s in sessions:
        for v in s["votes"]:
            for nm in v["za"] + v["przeciw"] + v["wstrzymal_sie"] + v["nieobecni"]:
                all_names.setdefault(nm, None)
    councilor_index = list(all_names.keys())
    cid = {n: i for i, n in enumerate(councilor_index)}

    votes_out, sessions_out, vi = [], [], 0
    for s in sessions:
        n_v = 0
        for v in s["votes"]:
            vi += 1
            n_v += 1
            nv = {k: [cid[n] for n in v[k] if n in cid] for k in ("za", "przeciw", "wstrzymal_sie")}
            votes_out.append({
                "id": f"hajnowka-{vi:04d}",
                "session_id": s["date"],
                "date": s["date"],
                "title": v["title"][:220],
                "source_url": s["source_url"],
                "named_votes": nv,
            })
        sessions_out.append({"id": s["date"], "date": s["date"], "number": s["number"],
                             "label": s["label"], "vote_count": n_v})

    kad = {
        "kadencja_id": KAD_ID,
        "label": "IX kadencja (2024–2029)",
        "sessions": sessions_out,
        "votes": votes_out,
        "councilor_index": councilor_index,
        "councilors": [{"id": cid[n], "name": n, "club": ""} for n in councilor_index],
        "total_councilors": len(councilor_index),
        "total_votes": len(votes_out),
    }
    (out_dir / f"kadencja-{KAD_ID}.json").write_text(
        json.dumps(kad, ensure_ascii=False, indent=1), encoding="utf-8")
    data = {"generated": datetime.now().isoformat(), "default_kadencja": KAD_ID,
            "kadencje": [{"id": KAD_ID, "label": "IX kadencja (2024–2029)"}]}
    (out_dir / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    profiles = []
    for n in councilor_index:
        slug = re.sub(r"-+", "-", re.sub(r"[^a-z0-9-]+", "-", n.lower())).strip("-")
        voted = sum(1 for v in votes_out if cid[n] in v["named_votes"]["za"]
                    or cid[n] in v["named_votes"]["przeciw"]
                    or cid[n] in v["named_votes"]["wstrzymal_sie"])
        profiles.append({
            "name": n, "slug": slug, "club": "", "role": "", "photo_url": "",
            "bio": "", "email": "", "social_links": {},
            "voting": {"votes": voted},
            "kadencje": {KAD_ID: {
                "club": "", "has_voting_data": True, "role": "",
                "votes": voted,
                "frekwencja": round(voted / max(len(votes_out), 1) * 100),
                "aktywnosc": 0, "zgodnosc_z_klubem": 0, "rebellion_count": 0,
            }},
        })
    (out_dir / "profiles.json").write_text(json.dumps(
        {"scraped_at": datetime.now().isoformat(), "profiles": profiles, "total": len(profiles)},
        ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"\nGOTOWE: {len(sessions_out)} sesji, {len(votes_out)} głosowań, {len(councilor_index)} radnych")
    return 0


if __name__ == "__main__":
    sys.exit(main())
