#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop Słubice — głosowania imienne (BIP Next.js Nefeni bip.slubice.pl + OCR).

BIP = Nefeni Next.js ("Nowoczesna Gmina"): API https://bip-api.slubice.pl/api/page-content/<slug>.
Kategoria 'Uchwały Rady Miejskiej' (29) zawiera per sesję:
  - artykuł z uchwałami (załączniki U*.pdf — pomijane),
  - artykuł 'Raport z głosowań' / 'NN Sesja ...' z JEDNYM załącznikiem =
    eSesja-print raport (app.esesja.pl) — SKANOWANY (brak warstwy tekstu)
    → OCR: pymupdf render dpi=150 + tesseract -l pol.
Format per głosowanie: 'Głosowano w sprawie: ...' / 'ZA: n, PRZECIW: n,
WSTRZYMUJĘ SIĘ: n, BRAK GŁOSU: n, NIEOBECNI: n' / 'Wyniki imienne:' /
'ZA(n)' listy po przecinkach / 'czas głosowania: 23 lipca 2026, 12:06'
(data miesięczna — wariant不同于 Zielonka 'Głosowanie z dnia: DD.MM.RRRR').
Walidacja: liczba nazwisk per kategoria == licznik nagłówka.
eSesja {slug}.esesja.pl działa (rid) ale sessions-list pusty (PM-dead) — raporty
są jedynym źródłem imiennym. Skład 15 radnych: artykuł 142 (page-content API).

Użycie: python scrape_slubice.py [city_dir]
"""
import json
import re
import ssl
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API = "https://bip-api.slubice.pl/api/page-content"
UA = {"User-Agent": "Mozilla/5.0 Radoskop/1.0 (info@radoskop.eu)"}
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE
CAT_UCHWALY = "kategorie/29-uchwaly-rady-miejskiej"
ROSTER_SLUG = ("kategorie/33-informacje-o-radzie-miejskiej-w-slubicach/artykuly/"
               "142-sklad-rady-miejskiej-w-slubicach-ix-kadencji-lata-20242029")
IX_START = "2024-05-07"

MONTHS = {"stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4, "maja": 5,
          "czerwca": 6, "lipca": 7, "sierpnia": 8, "wrzesnia": 9, "września": 9,
          "pazdziernika": 10, "października": 10, "listopada": 11, "grudnia": 12}


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=60, context=CTX) as r:
                return r.read()
        except Exception:
            if attempt == 3:
                raise
            time.sleep(2 + attempt * 2)


def _json(path: str):
    return json.loads(_get(path).decode("utf-8"))


def slugify(name: str) -> str:
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace("ł", "l")
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-") or "radny"


def norm_name(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace("ł", "l")
    return re.sub(r"[^a-z-]", "", s)


def cat_key(raw: str):
    raw = raw.upper()
    if raw.startswith("WSTRZYMUJ"):
        return "wstrzymal_sie"
    if raw.startswith("PRZECIW") or raw.startswith("PRZECN"):
        return "przeciw"
    if raw.startswith("BRAK"):
        return "brak_glosu"
    if raw.startswith("NIEOBECNI") or raw.startswith("NIEOECNI"):
        return "nieobecni"
    if raw == "ZA":
        return "za"
    return None


def ocr_pdf(pdf: bytes) -> str:
    import pymupdf
    doc = pymupdf.open(stream=pdf, filetype="pdf")
    pages = []
    with tempfile.TemporaryDirectory() as td:
        for i, pg in enumerate(doc):
            png = f"{td}/p{i}.png"
            pg.get_pixmap(dpi=150).save(png)
            r = subprocess.run(["tesseract", png, "-", "-l", "pol"],
                               capture_output=True, text=True)
            pages.append(r.stdout)
    doc.close()
    return "\n".join(pages)


def _vote_date(block: str, fallback: str) -> str:
    # 'Głosowanie z dnia: 23.07.2026, 12:06'
    m = re.search(r"G.{0,3}sowanie z dnia:?\s*(\d{1,2})\.(\d{2})\.(\d{4})", block)
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1).zfill(2)}"
    # 'czas głosowania: 23 lipca 2026, 12:06'
    m = re.search(r"g.{0,3}sowania:?\s*(\d{1,2})\s+([a-ząćęłńóśźż]+)\s+(\d{4})", block)
    if m and m.group(2) in MONTHS:
        return f"{m.group(3)}-{MONTHS[m.group(2)]:02d}-{m.group(1).zfill(2)}"
    return fallback


def parse_votes_text(text: str, fallback_date: str):
    """Układ raportu Słubic (app.esesja.pl):
    'N. Głosowano w sprawie: <topic> - czas głosowania: 23 lipca 2026, 12:06'
    'Wyniki głosowania (Radni)' / 'ZA: n, PRZECIW: n, ...'
    'Wyniki imienne:' / 'ZA(n)' + listy po przecinkach.
    """
    text = text.replace("\u00a0", " ")
    votes = []
    counts_re = re.compile(r"(ZA:?\s*\d+,?\s*PRZECIW:?\s*\d+[^\n]*)")
    parts = re.split(r"\n?\s*\d+\.\s*G.{0,4}sowano w sprawie:?", text)
    for b in parts[1:]:
        cm = counts_re.search(b)
        if not cm:
            continue
        topic = " ".join(b[:cm.start()].split())
        topic = re.sub(r"Wyniki g.{0,3}sowania(\s*\(Radni\))?:?", " ", topic)
        topic = re.sub(r"[-–]\s*czas g.{0,3}sowania:.*$", "", topic).strip(" .-–")
        agg_line = " ".join(cm.group(1).split())
        counts = dict(re.findall(r"(ZA|PRZECIW|WSTRZYMUJ[^:]{0,12}|BRAK\s*G.OSU|NIEOBECNI):\s*(\d+)", agg_line))
        date = _vote_date(b, fallback_date)
        im = b.find("Wyniki imienne")
        if im < 0:
            continue
        full = "\n" + b[im:]
        hdr = re.compile(r"\n\s*(WSTRZYMUJ[^(:\n]{0,12}|ZA|PRZECIW|BRAK\s*G.OSU|NIEOBECNI)\s*\((\d+)\)")
        heads = list(hdr.finditer(full))
        per = {}
        ok = True
        for j, h in enumerate(heads):
            key = cat_key(" ".join(h.group(1).upper().split()))
            if not key:
                continue
            n_decl = int(h.group(2))
            end = heads[j + 1].start() if j + 1 < len(heads) else len(full)
            names_txt = " ".join(full[h.end():end].split())
            names_txt = re.split(r"G.{0,3}sowanie z dnia|czas g.{0,3}sowania|Wygenerowano|Strona \d|Rada Miejska|O ZW|app\.esesja|Przygotowa|Drukowa|\d+\.\s*G.{0,4}sowano", names_txt)[0]
            names = [x.strip(" .") for x in names_txt.split(",") if x.strip(" .")]
            names = [re.sub(r"\s+", " ", x) for x in names
                     if re.match(r"^[A-ZŁŚŻŹĆĄŃÓ][\włżźćąóńś-]+", x)]
            # OCR: '!' '|' w nazwisku = 'l' ('Danie! Szurka'); przyklejona 1-literra = śmieć
            names = [re.sub(r"[!|]", "l", n) for n in names]
            names = [re.sub(r"\s+[a-z]$", "", n) for n in names]
            if len(names) != n_decl:
                ok = False
                break
            per.setdefault(key, []).extend(names)
        if not ok or not per or "za" not in per:
            continue
        cv = {k: int(v) for k, v in counts.items()}
        votes.append({"date": date, "topic": topic[:250], "per": per, "agg": cv})
    return votes


def validate(v):
    for cat, n in v["agg"].items():
        key = cat_key(" ".join(cat.upper().split()))
        if key is None:
            continue
        if len(v["per"].get(key, [])) != int(n):
            return False
    return True


def _walk_items(d):
    if isinstance(d, dict):
        if "title" in d and "slug" in d:
            yield d
        for v in d.values():
            yield from _walk_items(v)
    elif isinstance(d, list):
        for v in d:
            yield from _walk_items(v)


def main(city_dir: Path):
    docs = city_dir / "docs"
    docs.mkdir(exist_ok=True)
    # 1) skład rady (weryfikacja imion; roster i tak = union nazwisk z raportów)
    roster_names = []
    try:
        art = _json(f"{API}/{ROSTER_SLUG}?lang=PL").get("contentData", {}).get("item", {})
        body = art.get("content") or ""
        for line in re.split(r"<br\s*/?>|</p>", body):
            line = re.sub(r"<[^>]+>", " ", line)
            m = re.match(r"\s*([A-ZŁŚŻŹĆĄŃÓ][\włżźćąóńś-]+(?:\s+[A-ZŁŚŻŹĆĄŃÓ][\włżźćąóńś-]+){1,3})", line)
            if m and "kadencj" not in line.lower():
                roster_names.append(" ".join(m.group(1).split()))
        roster_names = list(dict.fromkeys(roster_names))
    except Exception as e:
        print(f"  [warn] roster: {e}")
    print(f"[slubice] roster z BIP: {len(roster_names)}")

    # 2) artykuły kategorii Uchwały → raporty głosowań (załącznik Raport*/S55C*)
    arts = {}
    for pg in ["", "&page=2", "&page=3", "&page=4", "&page=5", "&page=6"]:
        try:
            d = _json(f"{API}/{CAT_UCHWALY}?lang=PL{pg}")
        except Exception:
            break
        new = 0
        for it in _walk_items(d):
            if it["slug"] not in arts:
                arts[it["slug"]] = it
                new += 1
        if not new:
            break
        time.sleep(0.3)

    reports = []
    for slug, it in arts.items():
        try:
            det = _json(f"{API}/{slug}?lang=PL").get("contentData", {}).get("item", {})
        except Exception:
            continue
        title = det.get("title") or ""
        pub = (det.get("publishedDate") or "")[:10]
        atts = [a for a in (det.get("attachments") or [])
                if "aport" in (a.get("filename") or "").lower()
                or re.match(r"S55C", a.get("filename") or "")]
        if atts:
            # data sesji z tytułu '(20.08.2026 r.)'
            dm = re.search(r"\((\d{1,2})[.\s](\d{1,2})[.\s](\d{4})", title)
            sdate = f"{dm.group(3)}-{dm.group(2).zfill(2)}-{dm.group(1).zfill(2)}" if dm else pub
            reports.append({"title": title, "url": atts[0]["url"], "sdate": sdate})
        time.sleep(0.25)
    reports = [r for r in reports if r["sdate"] >= IX_START]
    reports.sort(key=lambda r: r["sdate"])
    print(f"[slubice] raportów IX: {len(reports)}")

    all_names, sessions, votes_out = [], [], []
    for rep in reports:
        try:
            pdf = _get(rep["url"])
            text = ocr_pdf(pdf)
            vs = [v for v in parse_votes_text(text, rep["sdate"]) if validate(v)]
        except Exception as e:
            print(f"  [warn] {rep['title'][:50]}: {e}")
            time.sleep(0.3)
            continue
        time.sleep(0.3)
        if not vs:
            print(f"  [skip] {rep['title'][:60]} — parser 0 zwalidowanych")
            continue
        rm = re.search(r"([IVXLCDM]+)", rep["title"])
        roman = rm.group(1) if rm else ""
        sdate = min(v["date"] for v in vs) if vs else rep["sdate"]
        idxs = {nm: n for n, nm in enumerate(all_names)}
        n_ok = 0
        for v in vs:
            nv = {k: [] for k in ("za", "przeciw", "wstrzymal_sie", "brak_glosu", "nieobecni")}
            for k, names in v["per"].items():
                for nm in names:
                    if nm not in idxs:
                        known = {norm_name(x): x for x in all_names}
                        if norm_name(nm) in known:
                            nm = known[norm_name(nm)]
                        else:
                            all_names.append(nm)
                        idxs[nm] = len(all_names) - 1
                    nv[k].append(idxs[nm])
            c = {"uprawnieni": sum(len(x) for x in nv.values()),
                 **{k: len(v["per"].get(k, [])) for k in ("za", "przeciw", "wstrzymal_sie")}}
            votes_out.append({
                "id": f"{v['date']}_{len(votes_out):03d}", "source_url": rep["url"],
                "session_date": v["date"], "session_number": roman,
                "topic": v["topic"], "druk": "None",
                "resolution": "przyjete" if c["za"] > c["przeciw"] else "odrzucone",
                "counts": c, "named_votes": nv})
            n_ok += 1
        sessions.append({"date": sdate, "number": roman,
                         "label": f"Sesja {roman} ({sdate})", "vote_count": n_ok,
                         "attendee_count": None, "attendees": [], "speakers": []})
        print(f"  [ok] {rep['title'][:55]} -> {n_ok} głosów")

    sessions.sort(key=lambda s: s["date"], reverse=True)
    # OCR/rok-fast fuzzy: raport 'Imię Nazwisko' vs roster 'Nazwisko Imię [Imię2]'
    import difflib
    def _toks(nm):
        return {norm_name(x) for x in nm.split() if len(norm_name(x)) >= 2}
    roster_tok = [(_toks(x), x) for x in roster_names]

    def canon_of(nm):
        t = _toks(nm)
        if not t:
            return nm
        # exact subset: raport-tokeny ⊆ roster-tokeny
        for rt, full in roster_tok:
            if t <= rt or rt <= t:
                return full if len(rt) < len(t) else _canon_display(full, nm)
        cand = difflib.get_close_matches(" ".join(sorted(t)),
                                         [" ".join(sorted(rt)) for rt, _ in roster_tok],
                                         n=1, cutoff=0.85)
        if cand:
            full = roster_tok[[" ".join(sorted(rt)) for rt, _ in roster_tok].index(cand[0])][1]
            return _canon_display(full, nm)
        return nm

    def _canon_display(roster_full, report_nm):
        # wyświetlaj w szyku raportu (Imię Nazwisko): posortuj tokeny rosteru wg kolejności raportu
        rp = report_nm.split()
        words = roster_full.split()
        given = [w for w in rp if norm_name(w) in {norm_name(x) for x in words}]
        surname = next((w for w in words if norm_name(w) == norm_name(rp[-1])), None)
        if surname and given:
            return " ".join(given) if len(given) > 1 else f"{given[0]} {surname}"
        return report_nm

    fixed = {}
    for i, nm in enumerate(all_names):
        c = canon_of(nm)
        if c != nm:
            fixed[i] = c
    if fixed:
        for v in votes_out:
            for k, lst in v["named_votes"].items():
                v["named_votes"][k] = [fixed.get(i, i) for i in lst]
        deduped, remap = [], {}
        for i, nm in enumerate(all_names):
            canon = fixed.get(i, nm)
            if canon in deduped:
                remap[i] = deduped.index(canon)
            else:
                remap[i] = len(deduped)
                deduped.append(canon)
        all_names = deduped
        for v in votes_out:
            for k, lst in v["named_votes"].items():
                v["named_votes"][k] = sorted({remap[i] for i in lst})
    # nazwiska spoza rosteru BIP? raport tylko
    rn = {norm_name(x) for x in roster_names}
    extra = [nm for nm in all_names if norm_name(nm) not in rn]
    if extra:
        print(f"  [info] nazwiska poza rosterem BIP: {extra}")
    councilors = []
    for i, nm in enumerate(all_names):
        z = p_ = w = b_ = nb = tot = 0
        for v in votes_out:
            nv = v["named_votes"]
            if i in nv["za"]:
                z += 1; tot += 1
            elif i in nv["przeciw"]:
                p_ += 1; tot += 1
            elif i in nv["wstrzymal_sie"]:
                w += 1; tot += 1
            elif i in nv["brak_glosu"]:
                b_ += 1; tot += 1
            elif i in nv["nieobecni"]:
                nb += 1
        uprawn = tot + nb
        role = ""
        ti = _toks(nm)
        for k, (rt, _full) in enumerate(roster_tok):
            if ti and (ti <= rt or rt <= ti):
                if k == 0:
                    role = "Przewodniczący Rady Miejskiej"
                elif k in (1, 2):
                    role = "Wiceprzewodniczący Rady Miejskiej"
                break
        councilors.append({"name": nm, "slug": slugify(nm), "club": "", "role": role,
                           "za": z, "przeciw": p_, "wstrzymal_sie": w,
                           "brak_glosu": b_, "nieobecny": nb, "glosowal": tot,
                           "frekwencja": round(100 * tot / uprawn, 1) if uprawn else 0,
                           "aktywnosc": round(100 * tot / uprawn, 1) if uprawn else 0,
                           "zgodnosc_z_klubem": None, "rebellion_count": 0})
    now = datetime.now(timezone.utc).isoformat()
    kad = {"id": "2024-2029", "label": "IX kadencja (2024–2029)",
           "sessions": sessions, "votes": votes_out,
           "councilor_index": all_names, "councilors": councilors,
           "total_councilors": len(all_names), "total_votes": len(votes_out),
           "similarity_top": [], "similarity_bottom": []}
    (docs / "kadencja-2024-2029.json").write_text(
        json.dumps(kad, ensure_ascii=False, indent=1), encoding="utf-8")
    profiles = {"scraped_at": now, "total": len(councilors), "profiles": [
        {"name": c["name"], "slug": c["slug"], "club": "", "role": c.get("role", ""),
         "photo_url": "", "bio": "", "email": "", "social_links": {},
         "voting": {"za": c["za"], "przeciw": c["przeciw"], "wstrzymal_sie": c["wstrzymal_sie"]},
         "kadencje": {"2024-2029": {
             "club": "", "has_voting_data": True, "role": c.get("role", ""),
             "frekwencja": c["frekwencja"], "aktywnosc": c["aktywnosc"],
             "zgodnosc_z_klubem": None, "rebellion_count": 0,
             "votes_total": c["glosowal"]}}} for c in councilors]}
    (docs / "profiles.json").write_text(json.dumps(profiles, ensure_ascii=False, indent=1), encoding="utf-8")
    data = {"scraped_at": now, "city": "Słubice", "bip": "https://bip.slubice.pl",
            "kadencje": [{"id": "2024-2029", "label": "IX kadencja (2024–2029)"}],
            "stats": {"sessions": len(sessions), "votes": len(votes_out),
                      "councilors": len(all_names)}}
    (docs / "data.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[slubice] DONE: {len(sessions)} sesji / {len(votes_out)} głosów / {len(all_names)} radnych")


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else Path("."))
