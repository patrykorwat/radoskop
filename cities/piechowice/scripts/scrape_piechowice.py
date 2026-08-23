#!/usr/bin/env python3
"""
Radoskop Piechowice — scraper głosowań z BIP na platformie Nefeni (bip.net.pl).

Piechowice NIE publikuje aktualnych głosowań imiennych na publicznej liście
eSesja (piechowice.esesja.pl/glosowania ma tylko 1 martwą sesję — XXII,
26.02.2026). Prawdziwe wyniki głosowań rada publikuje jako PDF-y
"Raport z przeprowadzonych głosowań" (generowane przez app.esesja.pl) na
swoim BIPie: https://piechowice.bip.net.pl

Struktura BIP:
  Rada Miasta (/kategorie/5)
    └─ Wyniki głosowań z sesji (/kategorie/197)
         └─ IX kadencja (2024-2029) (/kategorie/295)  ← tu są raporty sesji

Każdy artykuł raportu ma załącznik PDF (https://piechowice-api.bip.net.pl/
api/attachments/{id}) w formacie eSesja "standard" (Głosowano w sprawie /
Wyniki imienne). Parsuje go istniejący lib_voting_pdf_table.parse_voting_pdf.

Ten scraper:
  1. Listuje kategorie → znajduje podkategorię aktywnej kadencji.
  2. Listuje artykuły raportów (paginacja) z metadanymi sesji (numer, data).
  3. Dla każdego pobiera PDF (cache sha256), parsuje głosowania imienne.
  4. Mapuje nazwiska na kanoniczne (config.json club_assignments) i składa
     data.json + kadencja-{id}.json + profiles.json (standard Radoskop).

Skład rady + przypisania klubowe: config.json (sekcja club_assignments) —
jedno źródło prawdy, NIE hardcoded (patrz memory Poznań BIP).

Użycie:
  python3 scrape_piechowice.py --output docs/data.json --profiles docs/profiles.json
                                [--pdf-dir <scratch>/pdfs] [--cache-dir <scratch>/.cache/html]
                                [--max-sessions N] [--dry-run] [--debug]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

# Biblioteki projektu
HERE = Path(__file__).resolve()
_SCRIPTS = HERE.parents[3] / "scripts"
sys.path.insert(0, str(_SCRIPTS))

from lib_bip_net import NefeniRaport, download_pdf  # noqa: E402
from lib_voting_pdf_table import extract_pdf_text, extract_docx_text, parse_voting_text  # noqa: E402
from lib_slug import make_slug  # noqa: E402


BIP_BASE = "https://piechowice.bip.net.pl"
VOTES_CATEGORY = "/kategorie/197-wyniki-glosowan-z-sesji-rady-miasta-piechowice"
# Aktywna kadencja (podkategoria "Wyniki głosowań z sesji"). Odkrywana
# dynamicznie po frazie z config, z fallbackiem na twardy slug.
KADENCJE = {
    "2024-2029": {"label": "IX kadencja (2024–2029)", "start": "2024-05-07"},
}
DEFAULT_KADENCJA = "2024-2029"
KADENCJA_HINT = "IX kadencja"          # fraza w tytule podkategorii
KADENCJA_SUBCAT_FALLBACK = "/kategorie/295-ix-kadencja-rady-miasta-piechowice-20242029"

DELAY = 0.3
TIMEOUT = 40


def _load_councilors() -> dict[str, str]:
    """Wczytuje przypisania klubowe z config.json (single source of truth)."""
    config_path = HERE.parent.parent / "config.json"
    if not config_path.is_file():
        return {}
    try:
        cfg = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return cfg.get("club_assignments", {}) or {}


COUNCILORS: dict[str, str] = _load_councilors()


# ---------------------------------------------------------------------------
# Kanonizacja nazwisk (PDF eSesja zwykle podaje "Imię Nazwisko"; obronnie
# obsługujemy też "Nazwisko Imię" i nazwiska z drugimi imionami)
# ---------------------------------------------------------------------------

def _norm_key(name: str) -> str:
    return " ".join((name or "").strip().lower().split())


def _surname_key(name: str) -> str:
    parts = (name or "").strip().lower().split()
    if not parts:
        return ""
    # ostatni token = nazwisko (nazwiska złożone są łączone dywizem)
    last = parts[-1]
    return last


_DIAC = str.maketrans("ąćęłńóśźżĄĆĘŁŃÓŚŹŻ", "acelnoszzacelnoszz")


def _plain(s: str) -> str:
    return (s or "").translate(_DIAC).lower()


def _build_lookups(councilors: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    full: dict[str, str] = {}
    by_surname: dict[str, str] = {}
    for canonical in councilors:
        full[_norm_key(canonical)] = canonical
        by_surname[_plain(_surname_key(canonical))] = canonical
    return full, by_surname


def resolve_canonical(name: str, full_lookup: dict[str, str],
                      surname_lookup: dict[str, str]) -> str | None:
    n = _norm_key(name)
    if n in full_lookup:
        return full_lookup[n]
    hit = surname_lookup.get(_plain(_surname_key(name)))
    if hit:
        return hit
    # eSesja bywa publikuje imiona bez spacji ("ArturLipiński") — rozbijamy
    # wg znanej rady (pierwsze+ostatnie słowo kanonicznej nazwy bez spacji).
    c = _fix_concatenated(name)
    if c:
        return c
    return None


def _fix_concatenated(name: str, councilors: dict[str, str] | None = None) -> str | None:
    """Odwraca "ImięNazwisko" (bez spacji) do kanonicznej "Imię Nazwisko".

    Niektóre raporty eSesja mają nazwiska sklejone z imieniem ("ArturLipiński").
    Dopasowujemy token z samych liter (bez spacji/diakrytyków) do
    firstword+lastword każdej kanonicznej nazwy z club_assignments.
    Tylko pełne dopasowanie — zero ryzyka pomyłki.
    """
    tok = _plain("".join(ch for ch in name if ch.isalpha())).lower()
    if not tok:
        return None
    councilors = councilors if councilors is not None else COUNCILORS
    for canonical in councilors:
        words = canonical.split()
        if len(words) < 2:
            continue
        key = _plain(words[0] + words[-1])
        if tok == key:
            return canonical
    return None


# ---------------------------------------------------------------------------
# Odkrywanie podkategorii aktywnej kadencji
# ---------------------------------------------------------------------------

def find_kadencja_category(nb: NefeniRaport, debug: bool = False) -> str:
    """Znajduje podkategorię aktywnej kadencji pod kategorią wyniki głosowań.

    Jedno źródło prawdy tytułu = KADENCJA_HINT. Fallback na stały slug gdy
    BIP przestawi kategorię (nie wywalamy runu).
    """
    try:
        html = nb.fetch(BIP_BASE + VOTES_CATEGORY, use_cache=True)
    except Exception as exc:
        if debug:
            print(f"  [warn] nie pobrano kategorii głosowań: {exc}")
        return KADENCJA_SUBCAT_FALLBACK
    for m in re.finditer(r'href="(/kategorie/\d+-[^"]*)"[^>]*>([^<]*)', html):
        slug, title = m.group(1), m.group(2).strip()
        if KADENCJA_HINT.lower() in title.lower() or KADENCJA_HINT.lower() in slug.lower():
            if debug:
                print(f"  Podkategoria kadencji: {slug.split('?')[0]} — {title}")
            return slug.split("?")[0]
    return KADENCJA_SUBCAT_FALLBACK


# ---------------------------------------------------------------------------
# Składanie outputów (standard Radoskop, jak scrape_czestochowa.py)
# ---------------------------------------------------------------------------

def _build_clubs_summary(councilors: list[dict]) -> dict:
    out = {}
    for c in councilors:
        club = c.get("club", "")
        if not club:
            continue
        if club not in out:
            out[club] = {"members": 0, "members_list": []}
        out[club]["members"] += 1
        out[club]["members_list"].append(c["name"])
    return out


def build_profiles(sessions: list[dict], all_votes: list[dict]) -> list[dict]:
    """Profile per radny na podstawie imiennych głosowań (jak czestochowa)."""
    profiles = {}
    for name, club in COUNCILORS.items():
        profiles[name] = {
            "name": name,
            "slug": make_slug(name),
            "kadencje": {
                DEFAULT_KADENCJA: {
                    "club": club,
                    "club_full": club,
                    "frekwencja": 0,
                    "aktywnosc": 0,
                    "zgodnosc_z_klubem": 0,
                    "votes_za": 0,
                    "votes_przeciw": 0,
                    "votes_wstrzymal": 0,
                    "votes_total": 0,
                    "rebellion_count": 0,
                    "rebellions": [],
                }
            },
        }
    total_votes = len(all_votes)
    for v in all_votes:
        nv = v.get("named_votes", {})
        for key, names in nv.items():
            k = key if key != "wstrzymal_sie" else "wstrzymal"
            for n in names:
                if n not in profiles:
                    continue
                kd = profiles[n]["kadencje"][DEFAULT_KADENCJA]
                if k == "za":
                    kd["votes_za"] += 1
                elif k == "przeciw":
                    kd["votes_przeciw"] += 1
                elif k == "wstrzymal":
                    kd["votes_wstrzymal"] += 1
    for p in profiles.values():
        kd = p["kadencje"][DEFAULT_KADENCJA]
        active = kd["votes_za"] + kd["votes_przeciw"] + kd["votes_wstrzymal"]
        kd["votes_total"] = active
        if total_votes > 0:
            kd["frekwencja"] = round(100 * active / total_votes, 1)
            kd["aktywnosc"] = round(100 * active / total_votes, 1)
    return list(profiles.values())


def build_outputs(sessions: list[dict], all_votes: list[dict],
                  output_path: Path, profiles_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    profiles_path.parent.mkdir(parents=True, exist_ok=True)

    sessions_out = []
    for s in sessions:
        s_votes = [v for v in all_votes
                   if v["session_number"] == s["number"] and v["session_date"] == s["date"]]
        attendees = set()
        for v in s_votes:
            for key, names in v.get("named_votes", {}).items():
                if key in ("za", "przeciw", "wstrzymal_sie", "brak_glosu"):
                    attendees.update(names)
        sessions_out.append({
            "number": s["number"],
            "date": s["date"],
            "url": s["article_url"],
            "vote_count": len(s_votes),
            "attendee_count": len(attendees),
            "extraordinary": s["extraordinary"],
        })

    profiles = build_profiles(sessions_out, all_votes)
    councilors = [
        {"name": p["name"], "slug": p["slug"], **p["kadencje"][DEFAULT_KADENCJA]}
        for p in profiles
    ]

    kad = {
        "id": DEFAULT_KADENCJA,
        "label": KADENCJE[DEFAULT_KADENCJA]["label"],
        "scraped_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "sessions": sessions_out,
        "votes": all_votes,
        "councilors": councilors,
        "total_sessions": len(sessions_out),
        "total_votes": len(all_votes),
        "total_councilors": len(councilors),
        "clubs": _build_clubs_summary(councilors),
    }
    index = {
        "default_kadencja": DEFAULT_KADENCJA,
        "kadencje": [{"id": DEFAULT_KADENCJA, "label": KADENCJE[DEFAULT_KADENCJA]["label"]}],
    }
    kad_path = output_path.parent / f"kadencja-{DEFAULT_KADENCJA}.json"

    output_path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    kad_path.write_text(json.dumps(kad, ensure_ascii=False, indent=2), encoding="utf-8")
    profiles_path.write_text(
        json.dumps({"profiles": profiles}, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n=== Wyniki ===")
    print(f"Sesji:      {len(sessions_out)}")
    print(f"Głosowań:   {len(all_votes)}")
    print(f"Radnych:    {len(councilors)}")
    print(f"Zapisano:   {output_path}")
    print(f"            {kad_path}")
    print(f"            {profiles_path}")


# ---------------------------------------------------------------------------
# Główna logika scrape
# ---------------------------------------------------------------------------

# Starsze raporty (2024) piszą "Głosowano w sprawie " bez dwukropka, nowsze
# z dwukropkiem. lib_voting_pdf_table dzieli bloki po "Głosowano w sprawie:",
# więc normalizujemy dodając dwukropka przy braku.
_GLOSOWANO_NOCOLON = re.compile(r"(G[łl]osowano\s+wniosek\s+w sprawie|G[łl]osowano\s+w sprawie)(?!\s*:)", re.IGNORECASE)
# Końcówka "15 lipca 2024, godz. 11:08" wklejona w topic starszych formatów.
_TOPIC_TS = re.compile(
    r"[\s:,;–-]*\d{1,2}\s+(stycznia|lutego|marca|kwietnia|maja|czerwca|lipca|"
    r"sierpnia|września|października|listopada|grudnia)\s+\d{4},?\s+"
    r"godz\.?\s*\d{1,2}:\d{2}\s*$",
    re.IGNORECASE,
)


def _fix_concatenated_names_in_text(text: str, councilors: dict[str, str] | None = None) -> str:
    """Wstawia spację do sklejonych nazwisk ("ArturLipiński" -> "Artur Lipiński").

    eSesja w niektórych raportach publikuje "ImięNazwisko" bez spacji, przez co
    lib_voting_pdf_table filtruje je (wymaga >=2 słów) i giną z listy imiennej.
    Robimy to NA TEKŚCIE przed parsowaniem, żeby parser widział pełne nazwy.
    Tylko dokładne dopasowanie first+last kanonicznej nazwy — bez ryzyka pomyłki.
    """
    councilors = councilors if councilors is not None else COUNCILORS
    out = text
    for canonical in councilors:
        words = canonical.split()
        if len(words) < 2:
            continue
        joined = re.escape(words[0]) + re.escape(words[-1])
        spaced = f"{words[0]} {words[-1]}"
        pattern = re.compile(r"(?<!\w)" + joined + r"(?!\w)")
        out = pattern.sub(spaced, out)
    return out


def _parse_raport_pdf(path: Path) -> dict:
    """Parsuje raport głosowań (eSesja standard), tolerując brak dwukropka.

    Akceptuje PDF i DOCX (raporty bywają publikowane w obu — np. sesja III/2024
    to DOCX). Zwraca dict jak parse_voting_pdf (klucze date, votes, ...).
    """
    raw = path.read_bytes()[:4]
    if raw.startswith(b"PK"):
        full_text = extract_docx_text(path)
        first_page = full_text[:2000]
        source = path.name
    else:
        full_text, first_page = extract_pdf_text(path)
        source = Path(path).name
    norm = _GLOSOWANO_NOCOLON.sub(lambda m: m.group(1) + ":", full_text)
    norm = _fix_concatenated_names_in_text(norm)
    return parse_voting_text(norm, first_page, source_name=source)


def _clean_topic(topic: str) -> str:
    topic = _TOPIC_TS.sub("", topic or "").strip().rstrip(";:,- ")
    return topic[:300] if topic else topic


def scrape(output_path: Path, profiles_path: Path, pdf_dir: Path,
           cache_dir: str | None = None,
           max_sessions: int = 0, debug: bool = False, dry_run: bool = False) -> int:
    print("\n=== Radoskop Piechowice — BIP Nefeni (bip.net.pl) scraper ===")
    print(f"Output:      {output_path}")
    print(f"Profiles:    {profiles_path}")
    print(f"PDF cache:   {pdf_dir}")
    if cache_dir:
        print(f"HTML cache:  {cache_dir}")

    full_lookup, surname_lookup = _build_lookups(COUNCILORS)
    print(f"Radnych w club_assignments: {len(COUNCILORS)}")

    nb = NefeniRaport(BIP_BASE, delay=DELAY, timeout=TIMEOUT,
                      debug=debug, cache_dir=cache_dir)

    print(f"\n[1/3] Odkrywanie kategorii aktywnej kadencji")
    kad_cat = find_kadencja_category(nb, debug=debug)
    print(f"  Kategoria kadencji: {kad_cat}")

    print(f"\n[2/3] Listowanie artykułów raportów ({kad_cat})")
    articles = nb.articles_in_category(kad_cat, require="raport")
    print(f"  Znaleziono: {len(articles)} raportów sesji")
    if max_sessions:
        articles = articles[:max_sessions]
    if not articles:
        print("  UWAGA: brak artykułów raportów — BIP zmienił strukturę?")
        build_outputs([], [], output_path, profiles_path)
        return 0

    if dry_run:
        print("\nDry-run, sesje:")
        for a in articles:
            print(f"  {a.number:>5} | {a.date} | {a.title[:70]}")
        return 0

    pdf_dir.mkdir(parents=True, exist_ok=True)
    sessions = []
    all_votes = []
    print(f"\n[3/3] Pobieranie i parsowanie raportów PDF")
    for i, art in enumerate(articles, 1):
        print(f"  [{i}/{len(articles)}] Sesja {art.number or '?'} ({art.date or '?'}) — {art.title[:60]}")
        try:
            pdf_url, pdf_name = nb.attachment_for_article(art.article_url)
        except Exception as exc:
            print(f"      BŁĄD strony artykułu: {exc}")
            continue
        if not pdf_url:
            print(f"      brak załącznika PDF głosowań")
            continue
        art.pdf_url = pdf_url
        art.pdf_filename = pdf_name

        pdf_path = download_pdf(nb._session, pdf_url, pdf_dir, timeout=60)
        if not pdf_path:
            continue
        # Zabezpieczenie przed HTML-em błędu zamiast dokumentu (stare załączniki).
        magic = pdf_path.read_bytes()[:4]
        if not (magic.startswith(b"%PDF") or magic.startswith(b"PK")):
            print(f"      załącznik nie jest PDF/DOCX, pomijam ({pdf_path.stat().st_size}B)")
            pdf_path.unlink(missing_ok=True)
            continue
        try:
            parsed = _parse_raport_pdf(pdf_path)
        except Exception as exc:
            print(f"      BŁĄD parsowania PDF: {exc}")
            continue

        numeral, date_iso = art.number, art.date
        if not date_iso and parsed.get("date"):
            date_iso = parsed["date"]
        if not numeral:
            numeral = ""

        extraordinary = "nadzwycz" in art.title.lower()
        session_votes = []
        for vi, pv in enumerate(parsed.get("votes", [])):
            named = {}
            for cat_key, names in pv.get("named_votes", {}).items():
                canon = []
                for nm in names:
                    c = resolve_canonical(nm, full_lookup, surname_lookup)
                    if c and c not in canon:
                        canon.append(c)
                    elif c is None and debug:
                        print(f"        [warn] nieznany radny w głosowaniu: {nm!r}")
                named[cat_key] = canon
            # Głosowanie bez imiennych (rzadki format "Nazwisko Imię (ZA)" — np.
            # sesja VII/2024) jest bezużyteczne dla Radoskopu (brak listy imiennej),
            # więc go pomijamy zamiast pisać mylące głosowanie z 0 radnymi.
            if not any(named.values()):
                if debug:
                    print(f"        [warn] głosowanie bez listy imiennej, pomijam: {pv.get('topic','')[:50]!r}")
                continue
            session_votes.append({
                "id": f"{date_iso}_{numeral}_{vi:03d}",
                "session_number": numeral or "",
                "session_date": date_iso or "",
                "topic": _clean_topic(pv.get("topic") or f"Głosowanie nr {vi + 1}"),
                "counts": pv.get("counts", {}),
                "named_votes": named,
            })
        # Sesja bez żadnego sparsowanego głosowania (stary/nietypowy format PDF)
        # nie trafia do outputu — unikamy pustych sesji na stronie.
        if not session_votes:
            print(f"      brak głosowań do zapisania, pomijam sesję")
            continue
        print(f"      {len(session_votes)} głosowań imiennych")
        sessions.append({
            "number": numeral,
            "date": date_iso,
            "title": art.title,
            "article_url": art.article_url,
            "extraordinary": extraordinary,
        })
        all_votes.extend(session_votes)

    build_outputs(sessions, all_votes, output_path, profiles_path)
    return 0 if sessions else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Radoskop Piechowice, BIP Nefeni scraper")
    parser.add_argument("--output", default="docs/data.json")
    parser.add_argument("--profiles", default="docs/profiles.json")
    parser.add_argument("--pdf-dir", default=None, help="Katalog cache PDF (scratch).")
    parser.add_argument("--cache-dir", default=None, help="Katalog cache HTML (opcjonalny).")
    parser.add_argument("--max-sessions", type=int, default=0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    output_path = Path(args.output)
    profiles_path = Path(args.profiles)
    pdf_dir = Path(args.pdf_dir) if args.pdf_dir else Path("pdfs")

    try:
        rc = scrape(output_path, profiles_path, pdf_dir, cache_dir=args.cache_dir,
                    max_sessions=args.max_sessions, debug=args.debug, dry_run=args.dry_run)
    except KeyboardInterrupt:
        print("\nPrzerwano.")
        return 130
    except Exception as exc:
        print(f"\nBŁĄD: {exc}")
        import traceback
        traceback.print_exc()
        return 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
