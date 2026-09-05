#!/usr/bin/env python3
"""Radoskop Płońsk — scraper głosowań imiennych z BIP umplonsk.bip.org.pl (custom CMS).

Źródło: kategoria "Rada Miejska w Płońsku IX Kadencja 2024-2029 -> Głosowania"
(strony /id/2589 = 2026, /id/2487 = 2025, /id/2401 = 2024). Jeden DOCX = jedna
sesja; w nim bloki:
  GŁOSOWANIE NR N | Stan osobowy – X radnych | Ad pkt ... |
  [Nazwisko Imię][Za|Przeciw|Wstrzymał się]... |
  ZA|<n>|PRZECIW|<n>|WSTRZYMAŁ SIĘ|<n> |
  [NIE WZIĄŁ UDZIAŁU W GŁOSOWANIU][listy nazwisk] | [NIEOBECNY][-|listy] |
  Rada Miejska w Płońsku <rozstrzygnięcie>.
Format tokenów: każda komórka tabeli = osobny token (docx_rows).

Parser: strumień tokenów + maszyna stanów; walidacja — liczba nazwisk w
ZA/PRZECIW/WSTRZYMUJE = licznik z bloku, inaczej głosowanie odrzucone.
"""

import argparse
import io
import json
import re
import sys
import time
import urllib.parse
import urllib.request
import zipfile
from collections import OrderedDict
from datetime import datetime
from pathlib import Path

BASE = "https://umplonsk.bip.org.pl"
VOTE_PAGES = {2026: 2589, 2025: 2487, 2024: 2401}
KAD_ID = "2024-2029"
UA = {"User-Agent": "Radoskop/1.0 (info@radoskop.eu)"}

ROMAN = r"M{0,4}(?:CM|CD|D?C{0,3})(?:XC|XL|L?X{0,3})(?:IX|IV|V?I{0,3})"
NAME_RE = re.compile(
    r"^[A-ZŁŚŻŹĆĄĘŃÓ][\wŁŚŻŹĆĄĘŃóśłżźćądąęń\-\.]*\s+[A-ZŁŚŻŹĆĄĘŃ][\wŁŚŻŹĆĄĘŃóśłżźćądąęń\-\. ]+$"
)
VOTE_WORDS = {
    "za": "za",
    "przeciw": "przeciw",
    "wstrzymał się": "wstrzymal_sie",
    "wstrzymał": "wstrzymal_sie",
    "wstrzymała się": "wstrzymal_sie",
    "wstrzymała": "wstrzymal_sie",
    "wstrzymał/a się": "wstrzymal_sie",
}


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").replace("\u00a0", " ")).strip()


TITLE_WORDS = {
    "przewodniczący", "wiceprzewodniczący", "burmistrz", "sekretarz", "skarbnik",
    "rady", "miejskiej", "miejski", "miasta", "w", "pani", "pan",
}


def is_person(name: str) -> bool:
    if not NAME_RE.match(name):
        return False
    return not any(tok in name.lower().split() for tok in TITLE_WORDS)


def canon_key(name: str) -> frozenset:
    """kanoniczny klucz osoby = zbiór tokenów nazwiska (kolejność bez znaczenia)."""
    return frozenset(t.lower() for t in re.split(r"[\s\-]+", name) if t)


def fetch(url: str, timeout: int = 40) -> bytes:
    last = None
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"fetch failed {url}: {last}")


def docx_tokens(raw: bytes) -> list[str]:
    """word/document.xml -> strumień tokenów (każda komórka/akapit = token)."""
    xml = zipfile.ZipFile(io.BytesIO(raw)).read("word/document.xml").decode("utf-8")
    xml = re.sub(r"</w:tc>|</w:tr>|</w:p>", "\x02", xml)
    xml = re.sub(r"<[^>]+>", "", xml)
    return [t for t in (norm(x) for x in xml.split("\x02")) if t]


def _new_block() -> dict:
    return {"title": "", "za": [], "przeciw": [], "wstrzymal_sie": [], "brak_glosu": [], "nieobecni": []}


def parse_session_docx(raw: bytes) -> tuple[list[dict], list[str]]:
    toks = docx_tokens(raw)
    votes: list[dict] = []
    roster: "OrderedDict[str, None]" = OrderedDict()
    cur: dict | None = None
    counts: dict[str, int] = {}
    mode = None          # None | 'votes' | 'after_agg'
    in_abs = False       # lista po 'NIE WZIĄŁ UDZIAŁU...' / 'NIEOBECNY'
    agg_state = 0        # pozycja w sekwencji ZA|n|PRZECIW|n|WSTRZYMAŁ SIĘ|n
    prev: str | None = None
    for t in toks:
        tl = t.lower()
        # wariant 'Nazwisko - Głos' / 'Za - 12' / 'Nieobecny - Nazwisko'
        glue = re.match(r"^(.+?)\s+-\s+(.+)$", t)
        if glue and not tl.startswith("głosowanie nr"):
            left, right = norm(glue.group(1)), norm(glue.group(2))
            rl = right.lower()
            if left.lower() in VOTE_WORDS and re.fullmatch(r"\d+", right):
                counts[VOTE_WORDS[left.lower()]] = int(right)
                agg_state = 6
                mode = "after_agg"
                prev = None
                continue
            if rl.startswith("nieobecny") and is_person(right):
                if right not in cur["nieobecni"]:
                    cur["nieobecni"].append(right)
                roster.setdefault(right, None)
                prev = None
                continue
            if rl in VOTE_WORDS and is_person(left):
                key = VOTE_WORDS[rl]
                if left not in cur[key]:
                    cur[key].append(left)
                roster.setdefault(left, None)
                prev = None
                continue
        # wariant 'Nie głosował/a: Nazwisko, Nazwisko' / 'Nieobecna: Nazwisko'
        lab = re.match(r"^(?:Nie\s+głosował/a|Nieobecny|Nieobecna)\s*:\s*(.+)$", t)
        if lab and cur is not None:
            for name in re.split(r",\s*;?\s+", lab.group(1)):
                name = norm(name)
                if is_person(name):
                    if name not in cur["brak_glosu"]:
                        cur["brak_glosu"].append(name)
                    roster.setdefault(name, None)
            prev = None
            continue
        # wariant 'Nazwisko Głos' (spacja, bez myślnika) / 'Za 12' / 'NazwiskoZa'
        aggn = re.match(r"^(Za|Przeciw|Wstrzymał/a się|Wstrzymał się|Wstrzymała się)\s+(\d+)$", t)
        if aggn and cur is not None:
            key = VOTE_WORDS.get(aggn.group(1).lower())
            if key:
                counts[key] = int(aggn.group(2))
                agg_state = 6
                mode = "after_agg"
                prev = None
                continue
        m3 = re.match(r"^(.+?)\s+(Za|Przeciw|Wstrzymał się|Wstrzymała się|Wstrzymał/a się|Nie zagłosował|Nieobecny|Nieobecna)$", t)
        if m3 and not tl.startswith("głosowanie nr"):
            left, right = norm(m3.group(1)), norm(m3.group(2))
            rl = right.lower()
            if rl in ("nie zagłosował", "nieobecny", "nieobecna") and is_person(left):
                if left not in cur["brak_glosu"] and left not in cur["nieobecni"]:
                    cur["brak_glosu"].append(left)
                roster.setdefault(left, None)
                prev = None
                continue
            if rl in VOTE_WORDS and is_person(left) and agg_state < 6:
                key = VOTE_WORDS[rl]
                if left not in cur[key]:
                    cur[key].append(left)
                roster.setdefault(left, None)
                prev = None
                continue
            if re.fullmatch(r"\d+", right) and left.lower() in VOTE_WORDS:
                counts[VOTE_WORDS[left.lower()]] = int(right)
                agg_state = 6
                mode = "after_agg"
                prev = None
                continue
        mgl = re.match(r"^(.+?[a-ząćęłńóśźż])\s*(Za|Przeciw|Wstrzymał się|Wstrzymała się)$", t)
        if mgl and is_person(norm(mgl.group(1))) and agg_state < 6:
            left, right = norm(mgl.group(1)), norm(mgl.group(2))
            key = VOTE_WORDS[right.lower()]
            if left not in cur[key]:
                cur[key].append(left)
            roster.setdefault(left, None)
            prev = None
            continue
        if re.match(r"^głosowanie\s+nr\b", tl):
            if cur is not None and cur["title"]:
                if _validate(cur, counts):
                    votes.append(cur)
            cur, counts, mode, in_abs, agg_state, prev = _new_block(), {}, "votes", False, 0, None
            continue
        if cur is None:
            continue
        if tl.startswith("stan osobowy"):
            continue
        if re.match(r"^ad\s+pkt", tl) and not cur["title"]:
            cur["title"] = norm(re.sub(r"^ad\s+pkt\s*\S*\.?\s*", "", t, flags=re.I))
            continue
        if tl.startswith("rada miejska"):
            if _validate(cur, counts):
                votes.append(cur)
            cur, counts, mode, in_abs, agg_state, prev = None, {}, None, False, 0, None
            continue
        # sekwencja agregatów: ZA | n | PRZECIW | n | WSTRZYMAŁ SIĘ | n
        if tl == "za" and prev_is_num_or_none(prev, cur, agg_state, counts):
            agg_state = 1
            prev = t
            continue
        if agg_state == 1 and re.fullmatch(r"\d+", t):
            counts["za"] = int(t); agg_state = 2; prev = t; continue
        if agg_state == 2 and tl == "przeciw":
            agg_state = 3; prev = t; continue
        if agg_state == 3 and re.fullmatch(r"\d+", t):
            counts["przeciw"] = int(t); agg_state = 4; prev = t; continue
        if agg_state == 4 and tl.startswith("wstrzyma"):
            agg_state = 5; prev = t; continue
        if agg_state == 5 and re.fullmatch(r"\d+", t):
            counts["wstrzymal_sie"] = int(t); agg_state = 6; mode = "after_agg"
            in_abs = False; prev = t
            continue
        if re.fullmatch(r"[A-ZŁŚŻŹĆĄĘŃÓ ]*(W GŁOSOWANIU|-)", tl) or tl == "-":
            prev = t
            continue
        if "nie wzi" in tl and "głosowan" in tl:
            in_abs = True; prev = t; continue
        if tl == "nieobecny":
            in_abs = True
            # 'NIEOBECNY' może być etykietą kolumny dla listy NIE WZIĄŁ — nie
            # przełączaj celu; nazwiska po nim też idą do brak_glosu jeśli
            # agg już zebrany. Jeśli agg NIE zebrany (starszy układ) -> nieobecni.
            prev = t
            continue
        if re.fullmatch(r"\d+", t):
            prev = t
            continue
        # voice word after a name
        if tl in VOTE_WORDS and prev and is_person(prev):
            key = VOTE_WORDS[tl]
            if prev not in cur[key]:
                cur[key].append(prev)
            roster.setdefault(prev, None)
            prev = None
            continue
        # name token
        if is_person(t):
            if in_abs and agg_state >= 6:
                if t not in cur["brak_glosu"]:
                    cur["brak_glosu"].append(t)
            elif in_abs:
                if t not in cur["nieobecni"]:
                    cur["nieobecni"].append(t)
            prev = t
            roster.setdefault(t, None)
            continue
        prev = t
    if cur is not None and cur["title"] and _validate(cur, counts):
        votes.append(cur)
    return votes, list(roster.keys())


def prev_is_num_or_none(prev, cur, agg_state, counts) -> bool:
    """'ZA' licznikowe rozpoznajemy tylko poza sekwencją nazwisk-głosów:
    dopuszczamy je gdy agg_state==0 i ostatni token NIE jest imieniem i nazwiskiem
    (w głosowaniach samo 'Za' następuje PO nazwisku)."""
    if agg_state != 0 or not cur:
        return False
    return prev is None or not NAME_RE.match(prev)


def _validate(cur: dict, counts: dict) -> bool:
    if not counts or len(counts) < 3:
        print(f"  [skip] brak liczników: {cur['title'][:60]}")
        return False
    for k in ("za", "przeciw", "wstrzymal_sie"):
        if len(cur[k]) != counts.get(k, -1):
            print(f"  [skip] {k}: nazwisk={len(cur[k])} != licznik={counts.get(k)} | {cur['title'][:60]}")
            return False
    if not (cur["za"] or cur["przeciw"] or cur["wstrzymal_sie"]):
        return False
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="Radoskop Płońsk (umplonsk.bip.org.pl) scraper")
    ap.add_argument("--city-dir", required=True)
    ap.add_argument("--cache-dir", default="/cache/plonsk")
    args = ap.parse_args()
    city_dir = Path(args.city_dir)
    cache = Path(args.cache_dir)
    cache.mkdir(parents=True, exist_ok=True)
    out_dir = city_dir / "docs"
    out_dir.mkdir(parents=True, exist_ok=True)

    docx_re = re.compile(r'<a href="(/pliki/[^"]+\.docx[^"]*)"[^>]*>.*?</span>\s*([^<]+?)\s*</a>', re.S | re.I)
    seen_dates: dict[str, dict] = {}
    all_names: "OrderedDict[str, None]" = OrderedDict()
    for year, pid in sorted(VOTE_PAGES.items()):
        html = fetch(f"{BASE}/id/{pid}").decode("utf-8", "replace")
        items = OrderedDict()
        for url, text in docx_re.findall(html):
            text = norm(text)
            m = re.search(r"z\s+(" + ROMAN + r")\s+sesji.*?(\d{1,2})\.(\d{1,2})\.(\d{4})", text, re.I)
            if not m:
                continue
            dd, mm, yyyy = int(m.group(2)), int(m.group(3)), m.group(4)
            if not (1 <= mm <= 12 and 1 <= dd <= 31):
                continue
            date = f"{yyyy}-{mm:02d}-{dd:02d}"
            if date < "2024-05-07":
                continue
            items.setdefault(url, {"date": date, "title": text, "num": m.group(1).upper()})
        print(f"[{year}] sesji do pobrania: {len(items)}")
        for url, meta in items.items():
            fn = cache / re.sub(r"[^\w.-]", "_", url.split("?")[0].split("/")[-1])
            raw = fn.read_bytes() if fn.is_file() else None
            if raw is None:
                raw = fetch(BASE + urllib.parse.quote(url, safe="/?&="))
                fn.write_bytes(raw)
                time.sleep(0.8)
            votes, names = parse_session_docx(raw)
            for nm in names:
                all_names.setdefault(nm, None)
            if not votes:
                print(f"  [warn] brak zwalidowanych głosowań: {meta['title'][:60]}")
                continue
            prev = seen_dates.get(meta["date"])
            if prev and len(prev["votes"]) >= len(votes):
                continue
            seen_dates[meta["date"]] = {
                "date": meta["date"], "number": meta["num"],
                "label": f"Sesja {meta['num']} Rady Miejskiej w Płońsku ({meta['date']})",
                "votes": votes,
                "source_url": BASE + urllib.parse.quote(url.split("?")[0], safe="/"),
            }
            print(f"  {meta['date']} {meta['num']}: {len(votes)} głosowań")
    sessions = sorted(seen_dates.values(), key=lambda s: s["date"])

    # roster: poszerz o nazwiska z nieobecnych
    for s in sessions:
        for v in s["votes"]:
            for key in ("za", "przeciw", "wstrzymal_sie", "brak_glosu", "nieobecni"):
                for nm in v[key]:
                    all_names.setdefault(nm, None)
    # kanonizacja: ta sama osoba w szyku 'Nazwisko Imię' i 'Imię Nazwisko' -> forma pierwsza
    seen_keys: dict = {}
    canon: dict = {}
    for n in all_names:
        k = canon_key(n)
        if k in seen_keys:
            canon[n] = seen_keys[k]
        else:
            seen_keys[k] = n
            canon[n] = n
    councilor_index = [n for n in all_names if canon[n] == n and is_person(n)]
    for n in councilor_index:
        canon[canon_key(n)] = canon.get(n, n)
    cid = {n: i for i, n in enumerate(councilor_index)}

    def resolve(name_map: dict) -> dict:
        out = {}
        for key, arr in name_map.items():
            ids = []
            for nm in arr:
                std = canon.get(canon_key(nm), canon.get(nm))
                if std in cid and cid[std] not in ids:
                    ids.append(cid[std])
            out[key] = ids
        return out

    votes_out, sessions_out, vi = [], [], 0
    for s in sessions:
        n_v = 0
        for v in s["votes"]:
            vi += 1
            n_v += 1
            votes_out.append({
                "id": f"plonsk-{vi:04d}",
                "session_id": s["date"],
                "date": s["date"],
                "title": v["title"][:220],
                "source_url": s["source_url"],
                "named_votes": resolve(v),
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
        json.dumps({"kadencja": kad}, ensure_ascii=False, indent=1), encoding="utf-8")
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
