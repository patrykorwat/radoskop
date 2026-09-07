#!/usr/bin/env python3
"""Korpus uchwał JST z wojewódzkich e-dzienników (platforma Asseco).

Źródło: publiczne API e-dzienników (bez klucza), zweryfikowane 2026-09-06:
  GET /api/eli/acts                    -> dziennik: kod ELI, lata, deedsCount
  GET /api/eli/acts/<kod>/<rok>        -> WSZYSTKIE akty roku (title/type/status/eli/pos/volume)
  GET /api/legalact?year=&journal=&position= -> detale: Oid, Attachments, Publishers
  GET /api/LegalActHtml/<oid>          -> pełny tekst aktu (HTML)
  GET /GetFileXml.ashx?signature=true&id=<oidZalacznika> -> PDF ogloszony (pomijamy; zapisujemy URL)

Strumień zawiera: Uchwała, Zarządzenie, Obwieszczenie, Rozstrzygnięcie
nadzorcze, Wyrok (WSA, art. 13 pkt 5 ustawy o ogłaszaniu aktów) itp.
Akty niepodlegające ogłoszeniu (wewnętrzne) NIE trafiają tu — to domena
scrape_druki.py.

Dopasowanie do miast Radoskop: KURATOROWANE — słownik form z config.json
(rada_name, rada_name_genitive, city_name, city_genitive), bez regexa po
tytule. Pobranie pełnego tekstu: tylko akty pasujące do portfela +
wszystkie Rozstrzygnięcia nadzorcze i Wyroki (mało liczne, fundamentalne
dla widoku „Nadzór prawny").

Wyjścia (pod --out):
  units/acts_catalog/{KOD}_{rok}.json   katalog roczny (surowe metadane + match)
  city/{slug}/acts/{rok}/{eli_safe}.json  akt z tekstem (per miasto)
  units/acts_index.json                 licznik per miasto + licznik RN/Wyrok

Grzeczność: 1 host naraz, żądania z odstępem --sleep (domyślnie 1.1 s),
wznawianie: istniejące pliki wyjściowe są pomijane.
"""
from __future__ import annotations

import argparse
import json
import re
import ssl
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# Hosty e-dzienników wg oficjalnego katalogu dziennikiurzedowe.gov.pl
# (dzienniki-wojewodztw.html); wszystkie odpowiedziały /api/eli/acts z ATX 2026-09-06.
# Kod ELI pobierany z API, nie z listy. Uwaga: Podlaskie i Kujawsko-Pomorskie
# zwracaja klucze PascalCase (obsłużone w year_acts/detail).
HOSTS = [
    "edziennik.gdansk.uw.gov.pl",        # Pomorskie
    "edziennik.poznan.uw.gov.pl",        # Wielkopolskie
    "edziennik.lublin.uw.gov.pl",        # Lubelskie
    "edziennik.bialystok.uw.gov.pl",     # Podlaskie (PascalCase)
    "edziennik.rzeszow.uw.gov.pl",       # Podkarpackie
    "duwo.opole.uw.gov.pl",              # Opolskie
    "edziennik.kielce.uw.gov.pl",        # Świętokrzyskie
    "edzienniki.duw.pl",                 # Dolnośląskie
    "dzienniki.slask.eu",                # Śląskie
    "e-dziennik.szczecin.uw.gov.pl",     # Zachodniopomorskie
    "edzienniki.bydgoszcz.uw.gov.pl",    # Kujawsko-Pomorskie (PascalCase)
    "edzienniki.olsztyn.uw.gov.pl",      # Warmińsko-Mazurskie
    "edziennik.malopolska.uw.gov.pl",    # Małopolskie (powolny; http->https 301)
    "dziennik.lodzkie.eu",               # Łódzkie (powolny; http->https 301)
    "dzienniki.luw.pl",                  # Lubuskie (powolny; http->https 301)
    # Mazowieckie: edziennik.mazowieckie.pl nie odpowiada (2026-09-06, obie strony)
]
USER_AGENT = "RadoskopBot/1.0 (kolektor aktow publicznych; kontakt: kontakt@radoskop.eu)"

NUM_RE = re.compile(r"\b([IVXLC]{2,}[./]?[0-9]{1,4}[./][0-9]{2,4})\b")  # XII/214/25


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^a-z0-9]+", "", s.lower())
    return s


def _grab(req: urllib.request.Request, timeout: int, ctx=None) -> bytes:
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        return r.read()


_INSECURE_CTX = ssl._create_unverified_context()


def _retry_sleep(attempt: int) -> None:
    time.sleep(1.5 * (attempt + 1))


def _get(url: str, total_timeout: int) -> bytes:
    """Jedno zadanie z twardym terminem; retry + fallback na uszkodzony cats
    (lubuskie/lodzkie ida na wlasnym CA — weryfikacja zawodzi, -k dziala)."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    last: BaseException = RuntimeError("no attempts")
    for attempt in range(3):
        for ctx in (None, _INSECURE_CTX):
            box: dict = {}

            def worker():
                try:
                    box["data"] = _grab(req, min(40, total_timeout), ctx)
                except BaseException as e:  # noqa: BLE001 — przenosimy do watku glownego
                    box["err"] = e

            t = threading.Thread(target=worker, daemon=True)
            t.start()
            t.join(total_timeout)
            if t.is_alive():
                last = TimeoutError(f"HTTP twardy limit {total_timeout}s: {url}")
                continue
            if "err" not in box:
                return box["data"]
            last = box["err"]
        _retry_sleep(attempt)
    raise last


def http_json(url: str, total_timeout: int = 90):
    """GET+JSON z TWARDOYM terminem calym zazadaniem. socket-timeout nie chroni
    przed serwerem kapanym bajtami (dolnoslaski wisial 35 min, Recv-Q nie rasta).
    Watek-daemon + join z terminem: po przekroczeniu rzucamy TimeoutError i
    zostawiamy wiszace polaczenie za sobiem (daemon umrze razem z procesem)."""
    return json.loads(_get(url, total_timeout).decode("utf-8"))


def city_forms_map(cities_dir: Path) -> tuple[dict[str, str], dict[str, str]]:
    """(form_rady -> slug, form_miasta -> slug); kuratorowane z config.json.
    Formy rady (rada_name/rada_name_genitive) atrybuują akt radzie;
    formy miasta tylko oznaczaja wzmianke."""
    rada: dict[str, str] = {}
    mention: dict[str, str] = {}
    for cfg in sorted(cities_dir.glob("*/config.json")):
        try:
            c = json.loads(cfg.read_text(encoding="utf-8"))
        except Exception:
            continue
        if (c.get("country") or c.get("locale") or "pl").lower() != "pl" or c.get("disabled"):
            continue
        slug = cfg.parent.name
        for k in ("rada_name", "rada_name_genitive"):
            v = norm(c.get(k) or "")
            if len(v) >= 8:
                rada.setdefault(v, slug)
        for k in ("city_name", "city_genitive"):
            v = norm(c.get(k) or "")
            if len(v) >= 5:
                mention.setdefault(v, slug)
    return rada, mention


def best_match(t: str, forms: dict[str, str]) -> str | None:
    best = None
    for form, slug in forms.items():
        if form in t and (best is None or len(form) > len(best[0])):
            best = (form, slug)
    return best[1] if best else None


def journal_meta(host: str) -> dict | None:
    try:
        d = http_json(f"https://{host}/api/eli/acts")
    except Exception as e:
        print(f"  {host}: BRAK dostepu ({type(e).__name__})", file=sys.stderr)
        return None
    j = (d or [{}])[0]
    code = j.get("code") or j.get("Code")
    if not code:
        return None
    years = j.get("years") or j.get("Years") or []
    return {"host": host, "code": code, "years": sorted(years),
            "name": (j.get("shortName") or j.get("ShortName") or host),
            "deeds": j.get("deedsCount")}


def act_field(it: dict, *keys, default=None):
    for k in keys:
        if k in it and it[k] not in (None, ""):
            return it[k]
    return default


def year_acts(host: str, code: str, year: int, sleep: float) -> list[dict]:
    items, seen = [], set()
    d = http_json(f"https://{host}/api/eli/acts/{code}/{year}")  # API zwraca caly rocznik bez paginacji
    chunk = d.get("items") or d.get("Items") or []
    for it in chunk:
        it = {(k[0].lower() + k[1:]): v for k, v in it.items()}  # PascalCase (Podlaskie) -> camel
        eli = act_field(it, "eli", default="")
        if eli and eli not in seen:
            seen.add(eli)
            items.append(it)
    return items


def act_detail(host: str, item: dict, sleep: float) -> dict | None:
    year = act_field(item, "year", "Year")
    vol = act_field(item, "volume", "Volume", default=0)
    pos = act_field(item, "pos", "Pos", "position", "Position")
    dup = act_field(item, "duplicateChar", "DuplicateChar")
    q = f"year={year}&journal={vol}&position={pos}"
    if dup:
        q += f"&duplicateChar={dup}"
    det = http_json(f"https://{host}/api/legalact?{q}")
    time.sleep(sleep)
    a = det.get("LegalAct") or det
    oid = a.get("Oid")
    html = ""
    if oid:
        try:
            h = http_json(f"https://{host}/api/LegalActHtml/{oid}")
            html = h.get("Content") or ""
        except Exception:
            pass
    atts = [{"file": x.get("FileName"), "oid": x.get("Oid"),
             "kb": (x.get("FileSizeBytes") or 0) // 1024}
            for x in (a.get("Attachments") or []) if x.get("Extension") == "PDF" and x.get("Oid")]
    return {
        "oid": oid,
        "act_date": a.get("ActDate"), "status": a.get("ActStatus"),
        "publishers": [p.get("Name") for p in (a.get("Publishers") or a.get("PublishersList") or []) if p],
        "attachments": atts,
        "pdf_oid": atts[0]["oid"] if atts else None,
        "html_chars": len(html),
        "html": html[:400_000],
    }


def http_bytes(url: str, total_timeout: int = 120) -> bytes:
    """GET z twardym terminem i retry (jak http_json) — do PDF-ów."""
    return _get(url, total_timeout)


def pdf_to_text(raw: bytes) -> str:
    """pdftotext (poppler) przez stdin/stdout. Pusty wynik = skan bez warstwy tekstu."""
    try:
        p = subprocess.run(
            ["pdftotext", "-enc", "UTF-8", "-", "-"],
            input=raw, capture_output=True, timeout=45)
        return p.stdout.decode("utf-8", "replace")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=".", help="repo radoskop (cities/)")
    ap.add_argument("--out", required=True, help="katalog wyjsciowy")
    ap.add_argument("--years", default=str(datetime.now().year - 2),
                    help="od ktorego roku (lata piecia lata wstecz nie robimy)")
    ap.add_argument("--host", action="append", help="przeslon list HOSTS")
    ap.add_argument("--texts-for", default="matched,rn,wyrok",
                    help="ktore akty dostaja pelny tekst: matched=portfel,"
                         " rn=rozstrzygniecia nadzorcze, wyrok=wyroki WSA")
    ap.add_argument("--sleep", type=float, default=1.1)
    ap.add_argument("--max-texts", type=int, default=0, help="limit tekstow (0=bez)")
    args = ap.parse_args()

    out = Path(args.out)
    rada_forms, mention_forms = city_forms_map(Path(args.repo) / "cities")
    print(f"form rady: {len(rada_forms)} | form miasta: {len(mention_forms)}")
    want = set(args.texts_for.split(","))
    hosts = args.host or HOSTS
    n_text = 0

    for host in hosts:
        meta = journal_meta(host)
        if not meta:
            continue
        print(f"{meta['name']}: {meta['deeds']} aktow, lata {meta['years'][0]}-{meta['years'][-1]}")
        cat_dir = out / "units" / "acts_catalog"
        cat_dir.mkdir(parents=True, exist_ok=True)
        for year in [y for y in meta["years"] if y >= int(args.years)]:
            cat_p = cat_dir / f"{meta['code']}_{year}.json"
            if cat_p.exists():
                # wznawianie: katalog jest, ale teksty mogly przerwac sie w polowie
                items = json.loads(cat_p.read_text(encoding="utf-8")).get("items") or []
            else:
                try:
                    items = year_acts(host, meta["code"], year, args.sleep)
                except Exception as e:
                    print(f"  {year}: blad {type(e).__name__} {e}", file=sys.stderr)
                    continue
                for it in items:
                    t = norm(it.get("title") or "")
                    it["journal"] = meta["code"]
                    it["rada_match"] = best_match(t, rada_forms)        # atrybucja aktu radzie
                    it["city_mention"] = best_match(t, mention_forms)   # wzmianka miasta (routing RN/Wyrok)
                cat_p.write_text(json.dumps(
                    {"generated": datetime.now(timezone.utc).isoformat(),
                     "host": host, "journal": meta["code"], "year": year,
                     "count": len(items), "items": items}, ensure_ascii=False))
            matched = sum(1 for i in items if i.get("rada_match"))
            print(f"  {year}: {len(items)} aktow (akty rad portfela: {matched})", flush=True)

            # teksty: akty rad portfela + RN + Wyrok
            for it in items:
                typ = (it.get("type") or "").lower()
                why = None
                if "matched" in want and it["rada_match"]:
                    why = "portfel"
                elif "rn" in want and "nadzorc" in typ:
                    why = "nadzor"
                elif "wyrok" in want and typ in ("wyrok", "postanowienie"):
                    why = "sad"
                if not why:
                    continue
                slug = it["rada_match"] or it["city_mention"]
                if not slug:
                    # akt bez miasta z portfela: odkladamy do katalogu wojewodzinskiego
                    slug = "_kraj"
                eli_safe = re.sub(r"[^A-Za-z0-9._-]+", "_", it.get("eli") or f"{meta['code']}-{year}-{it['pos']}")
                dst = out / "city" / slug / "acts" / str(year) / f"{eli_safe}.json"
                if dst.exists() or (args.max_texts and n_text >= args.max_texts):
                    continue
                try:
                    d = act_detail(host, it, args.sleep)
                except Exception as e:
                    print(f"  tekst {it.get('eli')}: blad {type(e).__name__}", file=sys.stderr)
                    continue
                if not d:
                    continue
                text_html = d.get("html") or ""
                text_pdf = ""
                if not text_html and d.get("pdf_oid"):
                    # niektore dzienniki (np. dolnoslaski) nie maja endpointu HTML
                    # (500 Object reference) — tekst wycagamy z ogloszonego akt.pdf
                    try:
                        raw = http_bytes(f"https://{host}/GetFileXml.ashx?signature=true&id={d['pdf_oid']}")
                        text_pdf = pdf_to_text(raw)
                        time.sleep(args.sleep)
                    except Exception as e:
                        print(f"  pdf {it.get('eli')}: blad {type(e).__name__}", file=sys.stderr)
                        continue
                if not text_html and not text_pdf.strip():
                    continue  # skan bez warstwy tekstu — do OCR pozniej
                m = NUM_RE.search(it.get("title") or "")
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_text(json.dumps({
                    "eli": it.get("eli"), "journal": meta["code"], "journal_name": meta["name"],
                    "year": year, "pos": it.get("pos"), "type": it.get("type"),
                    "title": it.get("title"), "status": it.get("status"),
                    "announcement_date": it.get("announcementDate"),
                    "act_date": d.get("act_date"), "act_status": d.get("status"),
                    "publishers": d.get("publishers"), "attachments_pdf": d.get("attachments"),
                    "resolution_no": m.group(1) if m else None,
                    "city": slug, "why": why,
                    "source": f"https://{host}/",
                    "text_html": text_html,
                    "text_pdf": text_pdf if not text_html else "",
                    "generated": datetime.now(timezone.utc).isoformat(),
                }, ensure_ascii=False))
                n_text += 1
                if n_text % 50 == 0:
                    print(f"  teksty: {n_text}", flush=True)
            n_done = sum(1 for _ in (out / "city").glob(f"*/acts/{year}/*.json"))
            print(f"  {year}: teksty zapisane (narastajaco): {n_done}", flush=True)

    # indeks
    idx = {"generated": datetime.now(timezone.utc).isoformat(), "cities": {}, "kraj_rn": 0}
    for p in (out / "city").glob("*/acts/*/*.json"):
        slug = p.parts[-4] if len(p.parts) >= 4 else "_kraj"
        try:
            typ = (json.loads(p.read_text())["type"] or "").lower()
        except Exception:
            continue
        if slug == "_kraj":
            idx["kraj_rn"] += 1
        else:
            c = idx["cities"].setdefault(slug, {"uchwaly": 0, "nadzor": 0, "sad": 0})
            if "nadzorc" in typ:
                c["nadzor"] += 1
            elif typ in ("wyrok", "postanowienie"):
                c["sad"] += 1
            else:
                c["uchwaly"] += 1
    (out / "units").mkdir(parents=True, exist_ok=True)
    (out / "units" / "acts_index.json").write_text(json.dumps(idx, ensure_ascii=False))
    print(f"GOTOWE: teksty={n_text}, miasta z aktami={len(idx['cities'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
