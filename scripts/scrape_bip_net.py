#!/usr/bin/env python3
"""
scrape_bip_net.py — generyczny scraper głosowań imiennych z BIP na platformie
Nefeni (bip.net.pl) dla DOWOLNEGO miasta.

Cel: miasta które NIE mają imiennych głosowań na publicznym lisingu eSesja
(nawet jeśli działają na oprogramowaniu app.esesja.pl w tle), lecz publikują
wyniki jako pliki "Raport z przeprowadzonych głosowań" (PDF albo DOCX) na swoim
BIPie na platformie Nefeni (bip.net.pl / Next.js). To obejście blokera
"eSesja Portal Mieszkańca", który odcina ~40 miast (patrz
strategia/EXPANSION_100_200_CITIES.md).

MIASTO JEST CONFIG-DRIVEN — zero bespoke kodu per miasto. Wystarczy,
żeby cities/{slug}/config.json miał (oprócz standardowych club_assignments
i kadencje):

    "scrape": {
      "script": "scrape_bip_net.py",
      "deps": "requests beautifulsoup4 lxml pdfplumber python-docx",
      "bip_net": {
        "base_url": "https://piechowice.bip.net.pl",
        "votes_category": "/kategorie/197-wyniki-glosowan-z-sesji-rady-miasta-piechowice",
        "kadencja_hint": "IX kadencja",
        "kadencja_subcat_fallback": "/kategorie/295-ix-kadencja-...-20242029"
      }
    }

Struktura BIP Nefeni (sprawdzona na Piechowicach):
  BIP ({base_url})
    └─ kategoria "Rada ..."
         └─ kategoria "Wyniki głosowań z sesji"  (votes_category)
              └─ podkategoria aktywnej kadencji   (szukana po kadencja_hint)
                   └─ artykuły: "Raport z przeprowadzonych głosowań podczas
                                 XX.. sesji ..."  (każdy ma załącznik PDF/DOCX)

Ten scraper:
  1. Wczytuje config.json (club_assignments jako jedno źródło radnych).
  2. Znajduje podkategorię aktywnej kadencji pod votes_category.
  3. Listuje artykuły raportów (paginacja ?page=N) + metadane sesji (numer, data).
  4. Pobiera załącznik (PDF lub DOCX, cache sha256), parsiuje przez
     lib_voting_pdf_table (format eSesja standard, "Wyniki imienne").
  5. Mapuje nazwiska na kanoniczne (config club_assignments), naprawia
     sklejone nazwiska ("ArturLipiński") i format bez dwukropka.
  6. Składa data.json + kadencja-{id}.json + profiles.json (standard Radoskop).

Skład rady + kluby: config.json club_assignments (NIGDY hardcoded).

Użycie:
  python3 scrape_bip_net.py --config cities/{slug}/config.json \
        --output cities/{slug}/docs/data.json --profiles .../profiles.json \
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
_HERE = Path(__file__).resolve()
SCRIPTS_DIR = _HERE.parent
sys.path.insert(0, str(SCRIPTS_DIR))

from lib_bip_net import NefeniRaport, download_pdf  # noqa: E402
from lib_voting_pdf_table import extract_pdf_text, extract_docx_text, parse_voting_text  # noqa: E402
from lib_slug import make_slug  # noqa: E402


def _default_repo_root() -> Path:
    # radoskop/scripts/scrape_bip_net.py → radoskop/
    return SCRIPTS_DIR.parent


# ---------------------------------------------------------------------------
# Konfiguracja miasta (z config.json)
# ---------------------------------------------------------------------------

class BipNetConfig:
    def __init__(self, cfg: dict, slug: str):
        scrape = cfg.get("scrape", {}) or {}
        bn = scrape.get("bip_net", {}) or {}
        if not bn.get("base_url") or not bn.get("votes_category"):
            raise ValueError(
                f"[{slug}] config.json wymaga scrape.bip_net.base_url i "
                f"scrape.bip_net.votes_category (Nefeni raport adapter)."
            )
        self.base_url = bn["base_url"].rstrip("/")
        self.votes_category = bn["votes_category"]
        self.kadencja_hint = bn.get("kadencja_hint", "")
        self.kadencja_subcat_fallback = bn.get("kadencja_subcat_fallback", "")

        self.club_assignments: dict[str, str] = cfg.get("club_assignments", {}) or {}
        kad = cfg.get("kadencje", {}) or {}
        self.current_kadencja = cfg.get(
            "kadencja_active") or cfg.get("default_kadencja") or next(iter(kad), "")
        self.KADENCJE = kad or {self.current_kadencja: {"label": self.current_kadencja}}


# ---------------------------------------------------------------------------
# Kanonizacja nazwisk + naprawa formatu raportów (jak scrape_piechowice.py)
# ---------------------------------------------------------------------------

def _norm_key(name: str) -> str:
    return " ".join((name or "").strip().lower().split())


def _surname_key(name: str) -> str:
    parts = (name or "").strip().lower().split()
    return parts[-1] if parts else ""


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


def _fix_concatenated(name: str, councilors: dict[str, str]) -> str | None:
    """Odwraca "ImięNazwisko" (bez spacji) do kanonicznej "Imię Nazwisko"."""
    tok = _plain("".join(ch for ch in name if ch.isalpha())).lower()
    if not tok:
        return None
    for canonical in councilors:
        words = canonical.split()
        if len(words) < 2:
            continue
        if tok == _plain(words[0] + words[-1]):
            return canonical
    return None


def resolve_canonical(name: str, full_lookup: dict[str, str],
                      surname_lookup: dict[str, str], councilors: dict[str, str]) -> str | None:
    n = _norm_key(name)
    if n in full_lookup:
        return full_lookup[n]
    hit = surname_lookup.get(_plain(_surname_key(name)))
    if hit:
        return hit
    return _fix_concatenated(name, councilors)


# Format raportów: starsze bez dwukropka ("Głosowano w sprawie " vs ":")
_GLOSOWANO_NOCOLON = re.compile(
    r"(G[łl]osowano\s+wniosek\s+w sprawie|G[łl]osowano\s+w sprawie)(?!\s*:)", re.IGNORECASE)
_TOPIC_TS = re.compile(
    r"[\s:,;–-]*\d{1,2}\s+(stycznia|lutego|marca|kwietnia|maja|czerwca|lipca|"
    r"sierpnia|września|października|listopada|grudnia)\s+\d{4},?\s+"
    r"godz\.?\s*\d{1,2}:\d{2}\s*$", re.IGNORECASE)


def _fix_concatenated_names_in_text(text: str, councilors: dict[str, str]) -> str:
    """Wstawia spację do sklejonych nazwisk przed parsowaniem (lib filtruje
    nazwy <2 słów, więc tok "ArturLipiński" ginął)."""
    out = text
    for canonical in councilors:
        words = canonical.split()
        if len(words) < 2:
            continue
        pattern = re.compile(r"(?<!\w)" + re.escape(words[0]) + re.escape(words[-1]) + r"(?!\w)")
        out = pattern.sub(f"{words[0]} {words[-1]}", out)
    return out


def _parse_raport_file(path: Path, councilors: dict[str, str]) -> dict:
    """Parsuje raport (PDF/DOCX), format eSesja, tolerując brak dwukropka
    i sklejone nazwiska. Zwraca dict jak parse_voting_pdf."""
    raw = path.read_bytes()[:4]
    if raw.startswith(b"PK"):
        full_text = extract_docx_text(path)
        first_page = full_text[:2000]
        source = path.name
    else:
        full_text, first_page = extract_pdf_text(path)
        source = path.name
    norm = _GLOSOWANO_NOCOLON.sub(lambda m: m.group(1) + ":", full_text)
    norm = _fix_concatenated_names_in_text(norm, councilors)
    return parse_voting_text(norm, first_page, source_name=source)


def _clean_topic(topic: str) -> str:
    topic = _TOPIC_TS.sub("", topic or "").strip().rstrip(";:,- ")
    return topic[:300] if topic else topic


# ---------------------------------------------------------------------------
# Odkrywanie podkategorii aktywnej kadencji
# ---------------------------------------------------------------------------

def find_kadencja_category(cfg: BipNetConfig, nb: NefeniRaport, debug: bool = False) -> str:
    if cfg.kadencja_subcat_fallback:
        return cfg.kadencja_subcat_fallback.lstrip("/")
    if not cfg.kadencja_hint:
        # Bez hinta i fallbacka: nie możemy wybrać podkategorii.
        raise ValueError(
            f"[{cfg.current_kadencja}] wymagany scrape.bip_net.kadencja_hint "
            f"albo kadencja_subcat_fallback."
        )
    try:
        html = nb.fetch(nb.base_url + cfg.votes_category, use_cache=True)
    except Exception as exc:
        if debug:
            print(f"  [warn] nie pobrano kategorii głosowań: {exc}")
        return cfg.kadencja_subcat_fallback.lstrip("/") if cfg.kadencja_subcat_fallback else ""
    hint = cfg.kadencja_hint.lower()
    for m in re.finditer(r'href="(/kategorie/\d+-[^"]*)"[^>]*>([^<]*)', html):
        slug, title = m.group(1), m.group(2).strip()
        if hint in title.lower() or hint in slug.lower():
            if debug:
                print(f"  Podkategoria kadencji: {slug.split('?')[0]} — {title}")
            return slug.split("?")[0].lstrip("/")
    return cfg.kadencja_subcat_fallback.lstrip("/") if cfg.kadencja_subcat_fallback else ""


# ---------------------------------------------------------------------------
# Składanie outputów (standard Radoskop)
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


def build_profiles(sessions: list[dict], all_votes: list[dict],
                   cfg: BipNetConfig) -> list[dict]:
    profiles = {}
    kid = cfg.current_kadencja
    for name, club in cfg.club_assignments.items():
        profiles[name] = {
            "name": name,
            "slug": make_slug(name),
            "kadencje": {kid: {
                "club": club, "club_full": club,
                "frekwencja": 0, "aktywnosc": 0, "zgodnosc_z_klubem": 0,
                "votes_za": 0, "votes_przeciw": 0, "votes_wstrzymal": 0,
                "votes_total": 0, "rebellion_count": 0, "rebellions": [],
            }},
        }
    total_votes = len(all_votes)
    for v in all_votes:
        nv = v.get("named_votes", {})
        for key, names in nv.items():
            k = key if key != "wstrzymal_sie" else "wstrzymal"
            for n in names:
                if n not in profiles:
                    continue
                kd = profiles[n]["kadencje"][kid]
                if k == "za":
                    kd["votes_za"] += 1
                elif k == "przeciw":
                    kd["votes_przeciw"] += 1
                elif k == "wstrzymal":
                    kd["votes_wstrzymal"] += 1
    for p in profiles.values():
        kd = p["kadencje"][kid]
        active = kd["votes_za"] + kd["votes_przeciw"] + kd["votes_wstrzymal"]
        kd["votes_total"] = active
        if total_votes > 0:
            kd["frekwencja"] = round(100 * active / total_votes, 1)
            kd["aktywnosc"] = round(100 * active / total_votes, 1)
    return list(profiles.values())


def build_outputs(sessions: list[dict], all_votes: list[dict],
                  cfg: BipNetConfig, output_path: Path, profiles_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    profiles_path.parent.mkdir(parents=True, exist_ok=True)
    kid = cfg.current_kadencja
    label = CfgLabel(cfg)

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
            "number": s["number"], "date": s["date"], "url": s["article_url"],
            "vote_count": len(s_votes), "attendee_count": len(attendees),
            "extraordinary": s["extraordinary"],
        })

    profiles = build_profiles(sessions_out, all_votes, cfg)
    councilors = [
        {"name": p["name"], "slug": p["slug"], **p["kadencje"][kid]}
        for p in profiles
    ]
    kad = {
        "id": kid, "label": label,
        "scraped_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "sessions": sessions_out, "votes": all_votes, "councilors": councilors,
        "total_sessions": len(sessions_out), "total_votes": len(all_votes),
        "total_councilors": len(councilors), "clubs": _build_clubs_summary(councilors),
    }
    index = {"default_kadencja": kid, "kadencje": [{"id": kid, "label": label}]}
    kad_path = output_path.parent / f"kadencja-{kid}.json"
    output_path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    kad_path.write_text(json.dumps(kad, ensure_ascii=False, indent=2), encoding="utf-8")
    profiles_path.write_text(json.dumps({"profiles": profiles}, ensure_ascii=False, indent=2),
                             encoding="utf-8")

    print("\n=== Wyniki ===")
    print(f"Sesji:      {len(sessions_out)}")
    print(f"Głosowań:   {len(all_votes)}")
    print(f"Radnych:    {len(councilors)}")
    print(f"Zapisano:   {output_path}")
    print(f"            {kad_path}")
    print(f"            {profiles_path}")


def CfgLabel(cfg: BipNetConfig) -> str:
    kad = cfg.KADENCJE.get(cfg.current_kadencja, {})
    return kad.get("label", cfg.current_kadencja) if isinstance(kad, dict) else str(kad)


# ---------------------------------------------------------------------------
# Główna logika scrape
# ---------------------------------------------------------------------------

def scrape(cfg: BipNetConfig, output_path: Path, profiles_path: Path, pdf_dir: Path,
           cache_dir: str | None = None, max_sessions: int = 0,
           debug: bool = False, dry_run: bool = False) -> int:
    print("\n=== Radoskop BIP Nefeni (bip.net.pl) scraper ===")
    print(f"Base:        {cfg.base_url}")
    print(f"Kategoria:   {cfg.votes_category}")
    print(f"Output:      {output_path}")
    print(f"PDF cache:   {pdf_dir}")
    if cache_dir:
        print(f"HTML cache:  {cache_dir}")

    full_lookup, surname_lookup = _build_lookups(cfg.club_assignments)
    print(f"Radnych w club_assignments: {len(cfg.club_assignments)}")

    nb = NefeniRaport(cfg.base_url, debug=debug, cache_dir=cache_dir)

    print("\n[1/3] Odkrywanie kategorii aktywnej kadencji")
    kad_cat = find_kadencja_category(cfg, nb, debug=debug)
    print(f"  Kategoria kadencji: /{kad_cat}")
    if not kad_cat:
        print("  UWAGA: nie znaleziono podkategorii kadencji.")
        build_outputs([], [], cfg, output_path, profiles_path)
        return 0

    print(f"\n[2/3] Listowanie artykułów raportów (/{kad_cat})")
    articles = nb.articles_in_category(kad_cat, require="raport")
    print(f"  Znaleziono: {len(articles)} raportów sesji")
    if max_sessions:
        articles = articles[:max_sessions]
    if not articles:
        print("  UWAGA: brak artykułów raportów — sprawdź votes_category/kadencja.")
        build_outputs([], [], cfg, output_path, profiles_path)
        return 0

    if dry_run:
        print("\nDry-run, sesje:")
        for a in articles:
            print(f"  {a.number:>5} | {a.date} | {a.title[:70]}")
        return 0

    pdf_dir.mkdir(parents=True, exist_ok=True)
    sessions = []
    all_votes = []
    print("\n[3/3] Pobieranie i parsowanie raportów PDF/DOCX")
    for i, art in enumerate(articles, 1):
        print(f"  [{i}/{len(articles)}] Sesja {art.number or '?'} ({art.date or '?'}) — {art.title[:60]}")
        try:
            pdf_url, pdf_name = nb.attachment_for_article(art.article_url)
        except Exception as exc:
            print(f"      BŁĄD strony artykułu: {exc}")
            continue
        if not pdf_url:
            print("      brak załącznika PDF/DOCX głosowań")
            continue

        path = download_pdf(nb._session, pdf_url, pdf_dir, timeout=60)
        if not path:
            continue
        magic = path.read_bytes()[:4]
        if not (magic.startswith(b"%PDF") or magic.startswith(b"PK")):
            print(f"      załącznik nie jest PDF/DOCX, pomijam ({path.stat().st_size}B)")
            path.unlink(missing_ok=True)
            continue
        try:
            parsed = _parse_raport_file(path, cfg.club_assignments)
        except Exception as exc:
            print(f"      BŁĄD parsowania raportu: {exc}")
            continue

        numeral, date_iso = art.number, art.date
        if not date_iso and parsed.get("date"):
            date_iso = parsed["date"]
        extraordinary = "nadzwycz" in art.title.lower()

        session_votes = []
        for vi, pv in enumerate(parsed.get("votes", [])):
            named = {}
            for cat_key, names in pv.get("named_votes", {}).items():
                canon = []
                for nm in names:
                    c = resolve_canonical(nm, full_lookup, surname_lookup, cfg.club_assignments)
                    if c and c not in canon:
                        canon.append(c)
                    elif c is None and debug:
                        print(f"        [warn] nieznany radny: {nm!r}")
                named[cat_key] = canon
            if not any(named.values()):
                if debug:
                    print(f"        [warn] głosowanie bez listy imiennej, pomijam: {pv.get('topic','')[:45]!r}")
                continue
            session_votes.append({
                "id": f"{date_iso}_{numeral}_{vi:03d}",
                "session_number": numeral or "",
                "session_date": date_iso or "",
                "topic": _clean_topic(pv.get("topic") or f"Głosowanie nr {vi + 1}"),
                "counts": pv.get("counts", {}),
                "named_votes": named,
            })
        if not session_votes:
            print("      brak głosowań do zapisania, pomijam sesję")
            continue
        print(f"      {len(session_votes)} głosowań imiennych")
        sessions.append({
            "number": numeral, "date": date_iso, "title": art.title,
            "article_url": art.article_url, "extraordinary": extraordinary,
        })
        all_votes.extend(session_votes)

    build_outputs(sessions, all_votes, cfg, output_path, profiles_path)
    return 0 if sessions else 1


def load_cfg(config_path: Path, slug: str) -> BipNetConfig:
    cfg = json.loads(config_path.read_text(encoding="utf-8"))
    return BipNetConfig(cfg, slug)


def main() -> int:
    p = argparse.ArgumentParser(description="Radoskop BIP Nefeni (bip.net.pl) scraper")
    p.add_argument("--config", default=None, help="Ścieżka do config.json miasta.")
    p.add_argument("--city", default=None, help="Slug miasta (config z cities/{slug}/config.json).")
    p.add_argument("--output", default="docs/data.json")
    p.add_argument("--profiles", default="docs/profiles.json")
    p.add_argument("--pdf-dir", default=None)
    p.add_argument("--cache-dir", default=None)
    p.add_argument("--max-sessions", type=int, default=0)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--debug", action="store_true")
    args = p.parse_args()

    if args.config:
        cfg_path = Path(args.config)
        slug = cfg_path.parent.parent.name if cfg_path.parent.parent.name != "cities" else "?"
    elif args.city:
        cfg_path = _default_repo_root() / "cities" / args.city / "config.json"
        slug = args.city
    else:
        cfg_path = Path("config.json")
        slug = cfg_path.parent.name
    if not cfg_path.is_file():
        print(f"BŁĄD: brak config.json: {cfg_path}")
        return 2

    try:
        cfg = load_cfg(cfg_path, slug)
    except Exception as exc:
        print(f"BŁĄD konfiguracji: {exc}")
        return 2

    output = Path(args.output)
    profiles = Path(args.profiles)
    pdf_dir = Path(args.pdf_dir) if args.pdf_dir else Path("pdfs")
    try:
        rc = scrape(cfg, output, profiles, pdf_dir, cache_dir=args.cache_dir,
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
