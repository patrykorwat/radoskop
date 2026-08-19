#!/usr/bin/env python3
"""Scraper interpelacji radnych Rady Miejskiej w Nieszawie.

Źródło: BIP Nieszawy na platformie mojregion.info — kategoria
"Petycje i interpelacje":

    https://nieszawa.rbip.mojregion.info/687/petycje-i-interpelacje.html

eSesja Nieszawy (nieszawa.esesja.pl) publikuje tylko archiwalne sesje kadencji
2018–2024 (miasto `disabled: old_only`), więc jedynym realnym źródłem
interpelacji IX kadencji (2024–2029) jest ten statyczny rejestr BIP.

Struktura (pojedyncza strona, ZBIÓR ZAŁĄCZNIKÓW PDF — bez stron szczegółów):
  * Każda interpelacja to załącznik PDF, którego tytuł linku koduje radnego
    (w dopełniaczu, skrót imienia) i datę:
        "Interpelacja radnej J.Lipigórskiej 27.06.2024"
        "Interpelacja radnego K.Siecińskiego 03.07.2024 nr 2"
    ("nr 2"/"nr 3" = osobne, ponumerowane interpelacje; traktujemy jako
    osobne rekordy.)
  * Odpowiedź to osobny załącznik z tytułem zawierającym
    "Odpowiedź na interpelacje ... radn... z dnia DD.MM.YYYY" — dopasowujemy
    do interpelacji po (nazwisko, data).
  * Petycje (oddzielne załączniki "Petycja ...") i odpowiedzi na petycje —
    POMIJAMY (nie są interpelacjami radnych).
  * Przedmiot interpelacji jest tylko w zeskanowanym PDF (bez warstwy
    tekstowej) — zostawiamy `przedmiot=""` i NIE fabrykujemy (zgodnie z
    pułapką nr 5 w skill). Źródło klasyfikowane jako partial / subject
    'none-reliable'.

Radny jest w tytule w dopełniaczu i skrócie imienia ("J.Lipigórskiej"):
dopasowujemy fuzzy do pełnych nazwisk z config.json (club_assignments) i bierzemy
stamtąd klub.

Output: format Radoskop:
    {cri, typ, rok, kadencja, radny, przedmiot, data_wplywu, klub,
     odpowiedz_status, tresc_url, odpowiedz_url, data_odpowiedzi, bip_url}

Użycie:
    python3 scrape_interpelacje.py --output docs/interpelacje.json
    python3 scrape_interpelacje.py --output docs/interpelacje.json --cache-dir /tmp/c
"""

import argparse
import json
import re
import sys
import time
from pathlib import Path

import requests

HERE = Path(__file__).resolve()
try:
    sys.path.insert(0, str(HERE.parents[3] / "scripts"))
    from http_cache import init_cache  # noqa: F401
except Exception:
    def init_cache(*a, **k):
        return None

REJESTR_URL = "https://nieszawa.rbip.mojregion.info/687/petycje-i-interpelacje.html"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "pl-PL,pl;q=0.9",
}

DELAY = 0.8
MIN_ROK_DEFAULT = 2024
KADENCJA = "2024-2029"

_DEBUG = False


def _log(*a):
    if _DEBUG:
        print(*a)


def _load_clubs() -> tuple[dict, dict]:
    cfg_path = HERE.parent.parent / "config.json"
    if not cfg_path.is_file():
        return {}, {}
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception:
        return {}, {}
    return cfg.get("club_assignments", {}) or {}, cfg.get("clubs", {}) or {}


_CLUB_ASSIGN, _CLUBS = _load_clubs()


def _club_for_radny(radny: str) -> str:
    code = _CLUB_ASSIGN.get(radny, "")
    if not code:
        return ""
    club = _CLUBS.get(code)
    return club.get("name", "") if isinstance(club, dict) else ""


def _match_radny(surname: str, gender: str = "") -> str:
    """Map z dopelniaczowego nazwiska/skrotu do pelnego imienia i nazwiska z config."""
    s = surname.strip().lower()
    if not s:
        return ""
    import difflib
    if "ego" in gender.lower():
        is_fem = False
    elif "ej" in gender.lower():
        is_fem = True
    else:
        is_fem = None
    best, bestr = "", 0.0
    for full in _CLUB_ASSIGN:
        parts = full.split()
        if not parts:
            continue
        if is_fem is not None:
            fem_cand = parts[0].rstrip(".").endswith("a")
            if is_fem != fem_cand:
                continue  # dopasuj tylko pod wzgledem rodzaju (radnej/radnego)
        cand_surname = parts[-1].lower()
        score = difflib.SequenceMatcher(None, s, cand_surname).ratio()
        # nazwiska zlozone — dopasuj pierwszy czlon
        if "-" in cand_surname and score < 0.85:
            head = cand_surname.split("-")[0]
            score = max(score, difflib.SequenceMatcher(None, s, head).ratio())
        if score > bestr:
            bestr, best = score, full
    return best if bestr >= 0.60 else ""


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def fetch_text(session, url):
    for attempt in range(3):
        try:
            resp = session.get(url, timeout=30)
            if resp.status_code == 200:
                return resp.text
        except Exception as e:
            _log("fetch err", e)
        time.sleep(DELAY * (attempt + 1))
    return ""


def _clean_title(t):
    t = re.sub(r"<[^>]+>", "", t)
    t = re.sub(r"\(PDF[^)]*\)", "", t)
    return re.sub(r"\s+", " ", t).strip()


def _extract_surname_abbrev(title):
    """"Interpelacja radnej J.Lipigórskiej 27.06.2024" -> (surname_full, date_str)."""
    m = re.search(
        r"radn[eo]jg?o?|radne[jg]?|radn[eo]go"
        r"\s+([A-Z]\.\s*)?([\wŁ-]+)",
        title,
        re.I,
    )
    return m

def _parse_part(line):
    """From title -> dict(radny_abbrev, zloz_nazw, data, is_interp, is_odp)."""
    title = _clean_title(line)
    out = {"title": title, "is_interp": bool(re.match(r"[Ii]nterpelacja", title)),
           "is_odp": bool(re.search(r"[Oo]dpowied[źz]", title)),
           "radny": "", "radny_abbrev": "", "data": "", "gender": ""}
    dm_all = re.findall(r"(\d{2})\.(\d{2})\.(\d{4})", title)
    if dm_all:
        dm = dm_all[-1] if ("z dnia" in title and len(dm_all) > 1) else dm_all[0]
        out["data"] = f"{dm[2]}-{dm[1]}-{dm[0]}"
    # nazwisko: token po 'radn..' w formie dopelniaczowej (np. Lipigórskiej)
    nm = re.search(
        r"(radn[eo]j?g?o?|radn[eo]go)\s+([A-Z]\.\s*)?([A-ZŁŚŻŹĆĘÓĄ][\wŁ-]+)",
        title,
        re.I,
    )
    if nm:
        out["gender"] = nm.group(1)
        out["radny_abbrev"] = (nm.group(2) or "").strip()
        out["radny"] = nm.group(3)
    return out


def scrape(session):
    html = fetch_text(session, REJESTR_URL)
    if not html:
        raise RuntimeError("BIP Nieszawy nieosiagalny")
    links = re.findall(
        r'<a[^>]*href="([^"]*?/download/attachment/\d+/[^"]+)"[^>]*>(.*?)</a>',
        html, re.S,
    )
    records = []
    odp_by_key = {}
    # pierwszy przebieg: zebrac odpowiedzi, key=(nazwisko_lower, data_YYYY-MM-DD)
    odp_raw = []
    for href, txt in links:
        p = _parse_part(txt)
        if not p["title"] or "/download/" not in href:
            continue
        # absolutny URL
        url = href if href.startswith("http") else "https://nieszawa.rbip.mojregion.info" + href
        if p["is_odp"]:
            odp_raw.append((p, url))
        elif p["is_interp"] and p["radny"] and p["data"]:
            records.append((p, url))
    # klucz odpowiedzi = znormalizowane nazwisko + data
    def key(p):
        return (p["radny"].lower(), p["data"])
    for p, url in odp_raw:
        odp_by_key.setdefault(key(p), []).append(url)
    # buduj rekordy
    result = []
    seen = set()
    n = 0
    for p, url in records:
        rok = int(p["data"][:4]) if p["data"] else 0
        if not rok or rok < MIN_ROK_DEFAULT:
            continue
        k = key(p)
        o_urls = odp_by_key.get(k, [])
        # cri = czesc numeryczna attachment id
        m = re.search(r"/attachment/(\d+)/", url)
        cri = m.group(1) if m else f"nieszawa-{n}"
        dedup = (p["radny"].lower(), p["data"], url)
        if dedup in seen:
            continue
        seen.add(dedup)
        surname = p["radny"]
        radny = _match_radny(surname, p["gender"])
        rec = {
            "cri": cri,
            "typ": "interpelacja",
            "rok": rok,
            "kadencja": KADENCJA,
            "radny": radny if radny else surname,
            "przedmiot": "",  # skany, bez warstwy tekstowej — nie fabrykujemy
            "data_wplywu": p["data"],
            "klub": _club_for_radny(radny) if radny else "",
            "odpowiedz_status": "Udzielono" if o_urls else "Nie udzielono",
            "tresc_url": url,
            "odpowiedz_url": o_urls[0] if o_urls else "",
            "data_odpowiedzi": "",
            "bip_url": REJESTR_URL,
        }
        result.append(rec)
        n += 1
    # posortuj po dacie
    result.sort(key=lambda r: r["data_wplywu"])
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", default="interpelacje.json")
    ap.add_argument("--cache-dir", default=None)
    ap.add_argument("--max-pages", type=int, default=0)
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()

    init_cache(args.cache_dir)
    session = _session()
    data = scrape(session)
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[nieszawa] zapisano {len(data)} interpelacji -> {args.output}")
    for i, r in enumerate(data, 1):
        print(f"  {i}. {r['data_wplywu']} | {r['radny']} | {r['odpowiedz_status']} | {r.get('tresc_url','')[-40:]}")


if __name__ == "__main__":
    main()
