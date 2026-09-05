#!/usr/bin/env python3
"""Radoskop Płońsk — scraper imiennych głosowań z BIP umplonsk.bip.org.pl.

Źródło: kategoria "Głosowania" (Rada Miejska IX Kadencja, /id/2392) -> podstrony
roczne (/id/2589 2026, /id/2487 2025, /id/2401 2024) z załącznikami .docx
"Wyniki imiennego głosowania z <ROMAN> sesji Rady Miejskiej w Płońsku, DD.MM.RRRR r."
pod /pliki/ugplonsk1/.

Format docx (per głosowanie blok):
    Głosowanie nr N
    Stan osobowy – 21 radnych
    Ad pkt X. <topic>
    <Nazwisko Imię>  - Za|Przeciw|Wstrzymał(a) się        (per radny, Nazwisko-first!)
    Za - N / Przeciw - N / Wstrzymał/a się - N / Nie głosował/a - <nazwiska|0> / Nieobecny - N
    Rada Miejska przyjęła/odrzuciła ...

Walidacja: liczby wierszy ZA/PRZECIW/WSTRZYMAL musza rownac sie podsumowaniu;
nazwiska "Nie głosował/a"/"Nieobecny" (jesli nie cyfra) dopasowane do rejestru
po znormalizowanym "Nazwisko Imię". Nazwy kanonicznie "Imię Nazwisko".
"""

import json
import re
import ssl
import sys
import time
import unicodedata
import urllib.request
import zipfile
import io
from collections import defaultdict
from datetime import datetime
from itertools import combinations
from pathlib import Path

BASE = "https://umplonsk.bip.org.pl"
YEAR_PAGES = ["2589", "2487", "2401"]  # 2026, 2025, 2024 (IX kadencja)
KAD = "2024-2029"
KAD_LABEL = "IX kadencja (2024–2029)"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36"}
_SSL = ssl.create_default_context()
_SSL.check_hostname = False
_SSL.verify_mode = ssl.CERT_NONE

_VOTE_LINE = re.compile(r"^([A-ZŁŚŹŻĆŃÓĄĘ][\w\-ąćęłńóśźż]*(?:\s+[A-ZŁŚŹŻĆŃÓĄĘ][\w\-ąćęłńóśźż]*)*)\s+-\s*(Za|Przeciw|Wstrzyma[łl]a?\s+si[ęe])\s*$")
_HDR_ZA = re.compile(r"^Za\s*-\s*(\d+)")
_HDR_PRZ = re.compile(r"^Przeciw\s*-\s*(\d+)")
_HDR_WSTR = re.compile(r"^Wstrzyma[łl]/?a?\s*si[ęe]\s*-\s*(\d+)")
_HDR_NIEGLOS = re.compile(r"^Nie\s+g[łl]osowa[łl]/?a?\s*-\s*(.+)$")
_HDR_NIEOB = re.compile(r"^Nieobecny\s*-\s*(.+)$")
# --- old DOCX variant (2024-2025 files): name and vote token on separate lines,
# --- uppercase summary headers each followed by a count/value line ---
_OLD_HDR = re.compile(r"^(ZA|PRZECIW|WSTRZYMA[ŁL]A?\s+SI[ĘE])$")
_OLD_NIEGLOS = re.compile(r"^NIE\s+(WZI[AĄ]D|G[ŁŁ]OS)", re.I)
_OLD_NIEOB = re.compile(r"^NIEOBECN", re.I)
_LONE_VOTE = re.compile(r"^(Za|Przeciw|Wstrzyma[łl]a?\s+si[ęe])$")
_PERSON = re.compile(r"^[A-ZŁŚŹŻĆŃÓĄĘ][a-złśźżćńóąę]*(?:-[A-ZŁŚŹŻĆŃÓĄĘ][a-złśźżćńóąę]*)?(?:\s+[A-ZŁŚŹŻĆŃÓĄĘ][a-złśźżćńóąę'-]+){1,3}$")
_ROMAN = re.compile(r"\b(M?CM{0,4}|M?C?D{0,3}C{0,3}|M?(C[MD]|D?C{0,3})(XC|XL|L?X{0,3})(IX|IV|V?I{0,3}))\b")


def fetch(url, timeout=30, retries=4):
    last = None
    import urllib.parse
    parts = urllib.parse.urlsplit(url)
    url2 = urllib.parse.urlunsplit((parts.scheme, parts.netloc,
                                    urllib.parse.quote(parts.path, safe="/%"),
                                    parts.query, parts.fragment))
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url2, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout, context=_SSL) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"fetch failed {url}: {last}")


def slugify(name):
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


def norm(s):
    return re.sub(r"\s+", " ", s).strip().lower()


def docx_paragraphs(raw):
    z = zipfile.ZipFile(io.BytesIO(raw))
    xml = z.read("word/document.xml").decode("utf-8", "replace")
    paras = re.findall(r"<w:p[ >].*?</w:p>", xml, re.S)
    out = []
    for p in paras:
        t = "".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", p))
        t = t.replace("&amp;", "&").replace("–", "-").replace("\u00a0", " ")
        t = re.sub(r"\s+", " ", t).strip()
        if t:
            out.append(t)
    return out


def split_sur_first(name):
    """'Nazwisko Imię' -> canonical 'Imię Nazwisko' (hyphenated surnames kept)."""
    parts = name.split()
    if len(parts) < 2:
        return None
    first = parts[-1]
    sur = " ".join(parts[:-1])
    return f"{first} {sur}"


def parse_docx(paras, session_date, session_roman):
    records = []
    cur = None
    i = 0
    n = len(paras)
    while i < n:
        line = paras[i]
        if re.match(r"^G[łl]osowanie nr \d+", line, re.I):
            if cur:
                records.append(cur)
            cur = {"votes": {}, "topic": "", "hdr": {}, "nieglos_names": [], "nieob_names": []}
            i += 1
            continue
        if cur is None:
            i += 1
            continue
        m_h = _HDR_ZA.match(line)
        if m_h:
            cur["hdr"]["za"] = int(m_h.group(1)); i += 1; continue
        m_h = _HDR_PRZ.match(line)
        if m_h:
            cur["hdr"]["przeciw"] = int(m_h.group(1)); i += 1; continue
        m_h = _HDR_WSTR.match(line)
        if m_h:
            cur["hdr"]["wstrzymal_sie"] = int(m_h.group(1)); i += 1; continue
        m_h = _HDR_NIEGLOS.match(line)
        if m_h:
            rest = m_h.group(1).strip()
            if rest != "0" and not rest.isdigit():
                cur["nieglos_names"] = [x.strip() for x in rest.split(",") if x.strip()]
            i += 1
            continue
        m_h = _HDR_NIEOB.match(line)
        if m_h:
            rest = m_h.group(1).strip()
            if rest != "0" and not rest.isdigit():
                cur["nieob_names"] = [x.strip() for x in rest.split(",") if x.strip()]
            i += 1
            continue
        if re.match(r"^Rada Miejska (przyj|odrz|stwierdz)", line, re.I):
            i += 1
            continue
        if re.match(r"^Stan osobowy", line, re.I):
            i += 1
            continue
        m2 = _VOTE_LINE.match(line)
        if m2:
            nm_raw = m2.group(1)
            tok = m2.group(2)
            cat = ("za" if tok == "Za" else "przeciw" if tok == "Przeciw"
                   else "wstrzymal_sie")
            canon = split_sur_first(nm_raw)
            if canon:
                cur["votes"].setdefault(cat, []).append(canon)
            i += 1
            continue
        # --- old DOCX variant ---
        m_h = _OLD_HDR.match(line)
        if m_h:
            if i + 1 < n:
                val = paras[i + 1].strip()
                if val.isdigit():
                    key = ("za" if val is not None and m_h.group(1) == "ZA"
                           else "przeciw" if m_h.group(1) == "PRZECIW" else "wstrzymal_sie")
                    cur["hdr"][key] = int(val)
            i += 2
            continue
        if _OLD_NIEGLOS.match(line) or _OLD_NIEOB.match(line):
            key_bucket = "nieglos_names" if _OLD_NIEGLOS.match(line) else "nieob_names"
            if i + 1 < n:
                val = paras[i + 1].strip()
                if val and val != "-" and not val.isdigit() and _PERSON.match(val):
                    cur[key_bucket] = [x.strip() for x in val.split(",") if x.strip()]
            i += 2
            continue
        if _PERSON.match(line) and i + 1 < n and _LONE_VOTE.match(paras[i + 1]):
            tok = paras[i + 1]
            cat = ("za" if tok == "Za" else "przeciw" if tok == "Przeciw"
                   else "wstrzymal_sie")
            canon = split_sur_first(line)
            if canon:
                cur["votes"].setdefault(cat, []).append(canon)
            i += 2
            continue
        # --- variant 3 (2026-03 files): no dash; token appended to name, sometimes glued ---
        m3 = re.match(r"^(.+?)\s?(Za|Przeciw|Wstrzyma[łl]a?\s+si[ęe]|Nie\s+zag[łł]osowa[łl])$", line)
        if m3 and re.match(r"^[A-ZŁŚŹŻĆŃÓĄĘ]", m3.group(1)):
            nm_raw = m3.group(1).strip()
            tok = m3.group(2)
            if tok.lower().startswith("nie"):
                i += 1
                continue
            cat = ("za" if tok == "Za" else "przeciw" if tok == "Przeciw"
                   else "wstrzymal_sie")
            canon = split_sur_first(nm_raw)
            if canon:
                cur["votes"].setdefault(cat, []).append(canon)
            i += 1
            continue
        m_h = re.match(r"^(Za|Przeciw|Wstrzyma[łl]/?a?\s*si[ęe])\s+(\d+)$", line)
        if m_h:
            key = "za" if m_h.group(1) == "Za" else "przeciw" if m_h.group(1) == "Przeciw" else "wstrzymal_sie"
            cur["hdr"].setdefault(key, int(m_h.group(2)))
            i += 1
            continue
        m_h = re.match(r"^Nie\s+g[łl]osowa[łl]/?a?[:\s]+(.+)$", line)
        if m_h:
            rest = m_h.group(1).strip()
            if rest and rest != "0":
                cur["nieglos_names"] = [x.strip() for x in rest.split(",") if x.strip()]
            i += 1
            continue
        m_h = re.match(r"^Nieobecn[ay]?:?\s*(.+)$", line)
        if m_h:
            rest = m_h.group(1).strip()
            if rest and rest != "0":
                cur["nieob_names"] = [x.strip() for x in rest.split(",") if x.strip()]
            i += 1
            continue
        if not cur["topic"] and re.match(r"^(Ad pkt|Wniosek|Projekt|Informacja|Sprawozd|Uzasadn)", line, re.I):
            cur["topic"] = line
        i += 1
    if cur:
        records.append(cur)

    out = []
    for k, rec in enumerate(records):
        hdr = rec["hdr"]
        if not all(x in hdr for x in ("za", "przeciw", "wstrzymal_sie")):
            continue
        got = {c: len(rec["votes"].get(c, [])) for c in ("za", "przeciw", "wstrzymal_sie")}
        exp = {c: hdr[c] for c in ("za", "przeciw", "wstrzymal_sie")}
        if got != exp:
            continue
        named = {
            "za": rec["votes"].get("za", []),
            "przeciw": rec["votes"].get("przeciw", []),
            "wstrzymal_sie": rec["votes"].get("wstrzymal_sie", []),
            "brak_glosu": rec["votes"].get("brak_glosu", []) + [split_sur_first(x) or x for x in rec["nieglos_names"]],
            "nieobecni": [split_sur_first(x) or x for x in rec["nieob_names"]],
        }
        topic = rec["topic"] or f"Głosowanie nr {k+1}"
        out.append({"session_date": session_date, "session_num": session_roman,
                    "topic": topic, "named": named})
    return out


def main():
    city_dir = Path(sys.argv[sys.argv.index("--city-dir") + 1]) if "--city-dir" in sys.argv else Path(".")
    cache = Path(sys.argv[sys.argv.index("--cache-dir") + 1]) if "--cache-dir" in sys.argv else Path("./cache")
    cache.mkdir(parents=True, exist_ok=True)

    files = []  # (url, roman, date_iso)
    for yid in YEAR_PAGES:
        html = fetch(f"{BASE}/id/{yid}").decode("utf-8", "replace")
        for m in re.finditer(r'href="(/pliki/[^"]+\.(?:docx?|DOCX?)[^"]*)"[^>]*>(?:(?!</a>).)*?Wyniki imiennego[^<]*?([IVXLCDM]+)\s+sesji[^<]*?(\d{1,2})\.(\d{1,2})\.(\d{4})', html, re.S):
            url = BASE + m.group(1)
            roman = m.group(2)
            date_iso = f"{m.group(5)}-{int(m.group(4)):02d}-{int(m.group(3)):02d}"
            files.append((url, roman, date_iso))
    files = sorted(set(files), key=lambda x: x[2])
    files = [f for f in files if f[2] >= "2024-05-01"]
    print(f"[plonsk] vote-report files: {len(files)}")

    records = []
    import hashlib
    for url, roman, date_iso in files:
        cf = cache / (hashlib.md5(url.split("?")[0].encode()).hexdigest() + ".paras.txt")
        try:
            if cf.is_file():
                paras = cf.read_text(encoding="utf-8").splitlines()
            else:
                raw = fetch(url)
                paras = docx_paragraphs(raw)
                cf.write_text("\n".join(paras), encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            print(f"  [warn] {url}: {e}")
            continue
        recs = parse_docx(paras, date_iso, roman)
        total_blocks = len(re.findall(r"G[łl]osowanie nr", "\n".join(paras), re.I))
        print(f"  [{roman}] {date_iso}: votes_ok={len(recs)}/{total_blocks}")
        records.extend(recs)
        time.sleep(0.4)

    ok_sessions = len({r["session_date"] for r in records})
    print(f"[plonsk] sessions_with_votes={ok_sessions} votes_ok={len(records)}")
    if not records:
        print("[plonsk] NO RECONCILED VOTES — aborting")
        return 1

    # ---- build Radoskop output ----
    all_votes = []
    sessions_by_date = {}
    vid = 0
    for rec in records:
        d = rec["session_date"]
        if d not in sessions_by_date:
            sessions_by_date[d] = {"date": d, "number": rec["session_num"], "vote_count": 0, "attendees": set()}
        named = {k: list(v) for k, v in rec["named"].items()}
        vid += 1
        sessions_by_date[d]["vote_count"] += 1
        for cat in ("za", "przeciw", "wstrzymal_sie", "brak_glosu"):
            sessions_by_date[d]["attendees"].update(named.get(cat, []))
        all_votes.append({"id": str(vid), "session_date": d, "session_number": rec["session_num"],
                          "topic": rec["topic"], "named_votes": named,
                          "counts": {k: len(named.get(k, [])) for k in ("za", "przeciw", "wstrzymal_sie")}})
    sessions_data = []
    for d in sorted(sessions_by_date):
        s = sessions_by_date[d]
        sessions_data.append({"date": d, "number": s["number"], "vote_count": s["vote_count"],
                              "attendee_count": len(s["attendees"]), "attendees": sorted(s["attendees"]),
                              "speakers": []})
    all_names = set()
    for v in all_votes:
        for cat, names in v["named_votes"].items():
            if cat != "nieobecni":
                all_names.update(names)
    councilors_data = {n: {"name": n, "votes_za": 0, "votes_przeciw": 0, "votes_wstrzymal": 0,
                           "votes_brak": 0, "votes_nieobecny": 0} for n in all_names}
    for v in all_votes:
        keymap = {"za": "votes_za", "przeciw": "votes_przeciw", "wstrzymal_sie": "votes_wstrzymal",
                  "brak_glosu": "votes_brak", "nieobecni": "votes_nieobecny"}
        for cat, names in v["named_votes"].items():
            for nm in names:
                if nm in councilors_data:
                    councilors_data[nm][keymap[cat]] += 1
    total_votes = len(all_votes)
    total_sessions = len(sessions_data)
    csel = defaultdict(set)
    for v in all_votes:
        for cat, names in v["named_votes"].items():
            for nm in names:
                if nm in councilors_data:
                    csel[nm].add(v["session_date"])
    councilors_list = []
    for c in sorted(councilors_data.values(), key=lambda x: x["name"]):
        present = c["votes_za"] + c["votes_przeciw"] + c["votes_wstrzymal"]
        aktywnosc = present / total_votes * 100 if total_votes else 0
        frekwencja = len(csel.get(c["name"], set())) / total_sessions * 100 if total_sessions else 0
        councilors_list.append({"name": c["name"], "club": "", "district": None,
                                "frekwencja": round(frekwencja, 1), "aktywnosc": round(aktywnosc, 1),
                                "zgodnosc_z_klubem": 0.0,
                                "votes_za": c["votes_za"], "votes_przeciw": c["votes_przeciw"],
                                "votes_wstrzymal": c["votes_wstrzymal"], "votes_brak": c["votes_brak"],
                                "votes_nieobecny": c["votes_nieobecny"], "votes_total": total_votes,
                                "rebellion_count": 0, "rebellions": [],
                                "has_activity_data": False, "activity": None})
    vectors = defaultdict(dict)
    for v in all_votes:
        for cat in ("za", "przeciw", "wstrzymal_sie"):
            for nm in v["named_votes"].get(cat, []):
                vectors[nm][v["id"]] = cat
    pairs = []
    ns = sorted(vectors.keys())
    for a, b in combinations(ns, 2):
        common = set(vectors[a]) & set(vectors[b])
        if len(common) < 10:
            continue
        same = sum(1 for v2 in common if vectors[a][v2] == vectors[b][v2])
        pairs.append({"a": a, "b": b, "club_a": "", "club_b": "",
                      "score": round(same / len(common) * 100, 1), "common_votes": len(common)})
    pairs.sort(key=lambda x: x["score"], reverse=True)
    kad = {"id": KAD, "label": KAD_LABEL, "clubs": {}, "sessions": sessions_data,
           "total_sessions": total_sessions, "total_votes": total_votes,
           "total_councilors": len(councilors_list), "councilors": councilors_list,
           "votes": all_votes, "similarity_top": pairs[:20], "similarity_bottom": pairs[-20:][::-1]}

    cv = defaultdict(lambda: {"za": 0, "przeciw": 0, "wstrzymal_sie": 0, "votes": []})
    for rec in records:
        d = rec["session_date"]
        for cat in ("za", "przeciw", "wstrzymal_sie"):
            for nm in rec["named"].get(cat, []):
                cv[nm][cat] += 1
                cv[nm]["votes"].append({"session": d, "vote": cat})
    profiles = []
    for nm in sorted(all_names):
        vd = cv.get(nm, {"za": 0, "przeciw": 0, "wstrzymal_sie": 0, "votes": []})
        total = sum(vd[k] for k in ("za", "przeciw", "wstrzymal_sie")) or 1
        sess = len({v["session"] for v in vd["votes"]})
        profiles.append({"name": nm, "slug": slugify(nm),
                         "kadencje": {KAD: {
                             "club": "", "has_voting_data": True, "has_activity_data": False,
                             "frekwencja": round(sess / max(1, total_sessions) * 100, 1),
                             "aktywnosc": round((vd["za"] + vd["przeciw"] + vd["wstrzymal_sie"]) / max(1, len(records)) * 100, 1),
                             "zgodnosc_z_klubem": 0.0,
                             "votes_za": vd["za"], "votes_przeciw": vd["przeciw"],
                             "votes_wstrzymal": vd["wstrzymal_sie"],
                             "votes_brak": councilors_data[nm]["votes_brak"],
                             "votes_nieobecny": councilors_data[nm]["votes_nieobecny"],
                             "votes_total": total,
                             "rebellion_count": 0, "rebellions": [],
                             "roles": [], "notes": "",
                             "former": False, "mid_term": False}}})
    profiles = {"profiles": profiles, "total": len(profiles)}

    out_path = city_dir / "docs" / "data.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path.parent / f"kadencja-{KAD}.json", "w", encoding="utf-8") as f:
        json.dump(kad, f, ensure_ascii=False, separators=(",", ":"))
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"generated": datetime.now().isoformat(), "default_kadencja": KAD,
                   "kadencje": [{"id": KAD, "label": KAD_LABEL}]},
                  f, ensure_ascii=False, separators=(",", ":"))
    with open(out_path.parent / "profiles.json", "w", encoding="utf-8") as f:
        json.dump(profiles, f, ensure_ascii=False, separators=(",", ":"))
    print(f"[plonsk] FINAL: votes={total_votes} sessions={total_sessions} councilors={len(councilors_list)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
