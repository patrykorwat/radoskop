#!/usr/bin/env python3
"""
Scraper interpelacji i zapytań Sejmiku Województwa Mazowieckiego.

BIP Mazovii pod https://mazovia.pl/pl/bip/sejmik/interpelacje-i-zapytania-radnych/vii-kadencja/
trzyma listę interpelacji z filtrem `?p=YYYY^typ`, gdzie typ to
`interpelacja` albo `zapytanie`. Każda pozycja to osobny artykuł BIP-a
z metadanymi w `?format=json` i załącznikami PDF (treść interpelacji,
treść odpowiedzi).

Skrypt:
1. Iteruje po (rok, typ) ∈ {2024, 2025, 2026, ...} × {interpelacja, zapytanie}
2. Stronicuje przez `&page=N` aż lista pustnieje
3. Dla każdego URLu interpelacji pobiera ?format=json, wyciąga:
   - tytuł (numer + autor)
   - dateFirstPublicate (data publikacji)
   - PDFy: pierwszy z "interpelacja"/"zapytanie" w nazwie to treść,
     pierwszy z "odpowiedz" w nazwie to odpowiedź
4. Buduje listę zgodną ze schematem miast (cri, radny, przedmiot,
   tresc_url, odpowiedz_url, ...).

Schemat top-level: dict z `scraped_at` i `items[]`. Konsumenci miast
akceptują obie formy (lista lub dict z items), więc to działa.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError


BASE = "https://mazovia.pl"
LIST_PATH = "/pl/bip/sejmik/interpelacje-i-zapytania-radnych/vii-kadencja/"
KADENCJA = "VII"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "Radoskop/1.0 (+https://radoskop.pl)"
)
DEFAULT_TIMEOUT = 30
SLEEP_BETWEEN = 0.05

YEARS = ("2024", "2025", "2026")
TYPES = ("interpelacja", "zapytanie")
PAGE_SIZE = 20


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def fetch(url: str, *, cache_dir: Path | None = None, binary: bool = False,
          referer: str | None = None) -> bytes:
    cache_path = None
    if cache_dir:
        from hashlib import md5
        suffix = ".bin"
        cache_path = cache_dir / (md5(url.encode()).hexdigest() + suffix)
        if cache_path.is_file():
            return cache_path.read_bytes()
    req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    if referer:
        req.add_header("Referer", referer)
    try:
        with urlopen(req, timeout=DEFAULT_TIMEOUT) as resp:
            data = resp.read()
    except (HTTPError, URLError) as e:
        raise RuntimeError(f"GET {url} failed: {e}") from e
    if cache_path:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(data)
    time.sleep(SLEEP_BETWEEN)
    return data


def fetch_text(url: str, **kw) -> str:
    return fetch(url, **kw).decode("utf-8", errors="replace")


def fetch_json(url: str, **kw) -> Any:
    return json.loads(fetch_text(url, **kw))


# ---------------------------------------------------------------------------
# Discovery: listy interpelacji per (rok, typ) z paginacją
# ---------------------------------------------------------------------------

INTERP_HREF = re.compile(
    r'href="(' + re.escape(LIST_PATH) + r'\d+\d{2}-(?:interpelacja|zapytanie)-[^"]+\.html)"'
)


def discover_items_for(year: str, typ: str, *, cache_dir: Path | None) -> list[str]:
    """Stronicuj `?p=YYYY^typ&page=N` aż przestaną się pojawiać nowe linki."""
    seen: set[str] = set()
    page = 1
    while True:
        url = f"{BASE}{LIST_PATH}?p={year}%5E{typ}&page={page}"
        try:
            html = fetch_text(url, cache_dir=cache_dir)
        except RuntimeError:
            break
        new_links = set(INTERP_HREF.findall(html)) - seen
        if not new_links:
            break
        seen.update(new_links)
        if len(new_links) < PAGE_SIZE:
            # ostatnia strona albo niepełna
            break
        page += 1
        if page > 50:  # paranoja
            break
    return sorted(seen)


# ---------------------------------------------------------------------------
# Parser pojedynczej interpelacji
# ---------------------------------------------------------------------------

# Slug typu "2126-interpelacja-radnego-zdzislawa-sipiery" -> 21/26
SLUG_NUMBER = re.compile(r"^(\d+)(\d{2})-")
SLUG_TYPE = re.compile(r"-(interpelacja|zapytanie)-rad(?:nego|nej|nych)-(.+?)(?:-\d+)?$")
TITLE_NUMBER = re.compile(r"^(\d+)/(\d{2})\s+(interpelacja|zapytanie)\s", re.IGNORECASE)


def slug_from_url(url: str) -> str:
    return url.rstrip("/").split("/")[-1].removesuffix(".html")


def parse_cri_from_slug(slug: str) -> tuple[str | None, str | None, str | None]:
    """Zwróć (numer, rok2, typ). Slug typu '2126-interpelacja-radnego-jana-kowalskiego'
    rozumiemy jako: 21 = numer porządkowy, 26 = ostatnie 2 cyfry roku.
    """
    m_num = SLUG_NUMBER.match(slug)
    m_typ = SLUG_TYPE.search(slug)
    number = rok2 = None
    if m_num:
        number = m_num.group(1)
        rok2 = m_num.group(2)
    typ = m_typ.group(1) if m_typ else None
    return number, rok2, typ


def parse_author_from_title(title: str) -> str:
    """Z 'NN/RR interpelacja radnego Jana Kowalskiego' wytnij 'Jana Kowalskiego'
    (forma genitivus). Konwersja na mianownik dzieje się później przez
    fuzzy match z councilor_index z kadencja-2024-2029.json.
    """
    if not title:
        return ""
    # zdejmij prefix "NN/RR interpelacja|zapytanie "
    body = re.sub(
        r"^\d+/\d{2}\s+(?:interpelacja|zapytanie)\s+",
        "",
        title.strip(),
        flags=re.IGNORECASE,
    )
    # "radnego Jana Kowalskiego" -> "Jana Kowalskiego". Zostaje genitivus,
    # bo skupianie na zamianie genitivus -> mianownik bez słownika
    # nie wystarcza dla polskich nazwisk. Robimy to osobnym krokiem.
    # Powtarzane radn- (np. 'radnego radnych:') zdejmujemy w pętli.
    body = re.sub(r"^(?:rad(?:nego|nej|nych)\s*:?\s*)+", "", body, flags=re.IGNORECASE)
    return body.strip().rstrip(".;:,")


# ---------------------------------------------------------------------------
# Mapowanie genitivus -> mianownik
# ---------------------------------------------------------------------------

def load_councilor_index(kadencja_path: Path) -> list[str]:
    """Wczytaj listę mianowników radnych z kadencja-{id}.json."""
    if not kadencja_path.is_file():
        return []
    try:
        d = json.loads(kadencja_path.read_text(encoding="utf-8"))
        idx = d.get("councilor_index") or []
        return [n for n in idx if isinstance(n, str)]
    except (OSError, json.JSONDecodeError):
        return []


def _ratio(a: str, b: str) -> float:
    """SequenceMatcher ratio na lowercase, traktując myślnik i spacje równo."""
    from difflib import SequenceMatcher
    norm = lambda s: re.sub(r"[\s\-]+", " ", s.lower())
    return SequenceMatcher(None, norm(a), norm(b)).ratio()


def _match_single(name_g: str, candidates: list[str], threshold: float = 0.72) -> str | None:
    """Najlepszy mianownik z `candidates` dla genitivus `name_g`. None gdy
    żaden nie przekracza progu."""
    best: tuple[float, str] | None = None
    for nom in candidates:
        r = _ratio(name_g, nom)
        if best is None or r > best[0]:
            best = (r, nom)
    if best and best[0] >= threshold:
        return best[1]
    return None


def map_to_nominative(name_genitivus: str, candidates: list[str]) -> str:
    """Zwróć najsensowniejszą formę mianownika dla pola `radny`.

    Obsługuje:
      - pojedyncze nazwisko (Pawła Lisieckiego -> Paweł Lisiecki)
      - klub (Klubu Koalicji Obywatelskiej -> Klub Koalicji Obywatelskiej)
      - wielu radnych po przecinkach (mapuje każdego osobno, łączy '; ')
      - prefix 'radnych:' usunięty wcześniej już w parse_author_from_title

    Gdy fuzzy match poniżej progu, zwraca oryginalny string.
    """
    if not name_genitivus or not candidates:
        return name_genitivus

    s = name_genitivus.strip()

    # Klub: 'Klubu X Y' -> 'Klub X Y'. Genitivus form klubu jest zwykle
    # "Koalicji Obywatelskiej" itd. Bez słownika klubów nie zamieniamy
    # przymiotników; klucz 'Klubu' -> 'Klub' i rest zostaje.
    if s.lower().startswith("klubu "):
        return "Klub" + s[5:]

    # Wielu radnych: heurystyka - co najmniej 2 przecinki i każdy fragment
    # ma 2-3 słowa.
    if s.count(",") >= 1:
        parts = [p.strip() for p in s.split(",") if p.strip()]
        # Sprawdź czy każdy fragment to prawdopodobnie nazwisko (2 lub 3 słowa)
        if all(2 <= len(p.split()) <= 3 for p in parts):
            mapped = [_match_single(p, candidates) or p for p in parts]
            return "; ".join(mapped)

    # Pojedyncze nazwisko genitivus
    return _match_single(s, candidates) or s


def pick_attachment(files: list[dict[str, Any]], keywords: tuple[str, ...]) -> str | None:
    """Pierwszy plik którego src zawiera któryś z keywordów (po decode)."""
    from urllib.parse import unquote
    for f in files:
        src = f.get("src") or ""
        decoded = unquote(unquote(src)).lower()
        if any(k in decoded for k in keywords):
            return urljoin(BASE, src)
    return None


def fetch_interpelacja_meta(url: str, *, cache_dir: Path | None) -> dict[str, Any] | None:
    """Pobierz ?format=json i wyciągnij meta + linki do PDFów."""
    json_url = url + "?format=json"
    try:
        d = fetch_json(json_url, cache_dir=cache_dir)
    except RuntimeError:
        return None

    title = d.get("title") or d.get("header") or ""
    date_pub = (d.get("dateFirstPublicate") or "").split(" ")[0] or None
    date_mod = (d.get("dateLastVersionPublicate") or "").split(" ")[0] or None

    files: list[dict[str, Any]] = []
    for c in d.get("components", []) or []:
        if c.get("type") == "Attachment":
            files = c.get("content") or []
            break

    tresc_url = pick_attachment(
        files, ("interpelacj", "zapytani", "tres", "wniosek", "pismo")
    )
    odpowiedz_url = pick_attachment(files, ("odpowiedz", "odpowiedź"))

    return {
        "url": url,
        "title": title,
        "date_pub": date_pub,
        "date_mod": date_mod,
        "tresc_url": tresc_url,
        "odpowiedz_url": odpowiedz_url,
    }


# ---------------------------------------------------------------------------
# Główna pętla
# ---------------------------------------------------------------------------

def build_interpelacje(
    *, cache_dir: Path | None = None,
    councilor_candidates: list[str] | None = None,
) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    candidates = councilor_candidates or []

    for year in YEARS:
        for typ in TYPES:
            print(f"==> {year} {typ}", file=sys.stderr)
            urls = discover_items_for(year, typ, cache_dir=cache_dir)
            print(f"    {len(urls)} pozycji", file=sys.stderr)

            for url in urls:
                full_url = urljoin(BASE, url)
                meta = fetch_interpelacja_meta(full_url, cache_dir=cache_dir)
                if not meta:
                    continue
                slug = slug_from_url(url)
                number, rok2, slug_typ = parse_cri_from_slug(slug)

                # Wybierz typ z URL filtru, ale weryfikuj slug.
                final_typ = slug_typ or typ
                rok_int = int(year)
                cri = f"{number}/{rok2}" if number and rok2 else slug
                radny_genitivus = parse_author_from_title(meta["title"])
                radny = map_to_nominative(radny_genitivus, candidates)

                items.append({
                    "cri": cri,
                    "ezd": "",
                    "data_wplywu": meta["date_pub"] or "",
                    "radny": radny,
                    "radny_oryginalny": radny_genitivus,
                    "przedmiot": meta["title"],
                    "tresc_url": meta["tresc_url"] or "",
                    "data_odpowiedzi": meta["date_mod"] if meta["odpowiedz_url"] else "",
                    "odpowiedz_url": meta["odpowiedz_url"] or "",
                    "data_publikacji": meta["date_pub"] or "",
                    "rok": rok_int,
                    "typ": final_typ,
                    "kadencja": KADENCJA,
                    "url": full_url,
                })

    return {
        "scraped_at": datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "kadencja": KADENCJA,
        "items": items,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        default=str(Path(__file__).resolve().parent.parent / "docs" / "interpelacje.json"),
    )
    parser.add_argument(
        "--cache-dir",
        default=str(Path(__file__).resolve().parent.parent / "data" / "cache_interp"),
    )
    parser.add_argument(
        "--kadencja",
        default=str(Path(__file__).resolve().parent.parent / "docs" / "kadencja-2024-2029.json"),
        help="Ścieżka do kadencja-{id}.json (źródło mianowników radnych do mapowania).",
    )
    args = parser.parse_args()

    cache_dir = Path(args.cache_dir).resolve()
    cache_dir.mkdir(parents=True, exist_ok=True)

    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)

    candidates = load_councilor_index(Path(args.kadencja).resolve())
    if candidates:
        print(
            f"==> Wczytano {len(candidates)} mianowników z {args.kadencja}",
            file=sys.stderr,
        )

    payload = build_interpelacje(
        cache_dir=cache_dir,
        councilor_candidates=candidates,
    )
    with output.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    items = payload["items"]
    by_typ: dict[str, int] = {}
    by_year: dict[int, int] = {}
    for it in items:
        by_typ[it["typ"]] = by_typ.get(it["typ"], 0) + 1
        by_year[it["rok"]] = by_year.get(it["rok"], 0) + 1

    print(
        f"\nZapisano {output}: {len(items)} pozycji",
        file=sys.stderr,
    )
    for t, n in sorted(by_typ.items()):
        print(f"  {t}: {n}", file=sys.stderr)
    for y, n in sorted(by_year.items()):
        print(f"  {y}: {n}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
