#!/usr/bin/env python3
"""
Generate SEO-optimized static pages for Radoskop city instances.

Creates content-rich HTML pages for search engines to index. Po migracji
2026-05 wszystkie miasta (PL, CZ, DE) używają angielskich URL slugów.
Cloudflare Worker robi 301 ze starych polskich URL-i (PATH_REDIRECTS
w radoskop-premium/cloudflare/worker.js), żeby zachować link equity z
historycznych Google index entries.

Output paths (per city slug w S3):
  - /profile/{slug}/index.html  (councillor profiles)
  - /vote/{id}/index.html       (individual votes)
  - /session/{number}/index.html (sessions)
  - /term/{slug}/index.html     (term tabs)
  - /budget/index.html          (budget page)
  - /reports/index.html         (reports landing)
  - /privacy/index.html         (privacy policy)
  - /terms/index.html           (terms of service)
  - sitemap.xml                 (full sitemap)

Each page:
  1. Has unique <title>, <meta description>, <link canonical>, OG tags
  2. Has og:image pointing to generated OG image (if available)
  3. Contains visible text content for Google to index
  4. Loads the full SPA JS so the page becomes interactive after hydration

Usage:
    python generate_seo_pages.py --base /path/to/gdansk-network
    python generate_seo_pages.py --base /path/to/gdansk-network --city radoskop-gdansk
"""

import argparse
import html
import json
import re
from pathlib import Path

# Repliki historycznych slugify do mapy redirectów stary→kanoniczny
# (_redirects/profiles.json, czyta ją worker) — patrz lib_slug.py.
from lib_slug import legacy_nfkd_slug, legacy_table_slug, legacy_surname_first_slug
from lib_session_summary import (
    session_votes, summarize_session, valid_session_number,
)
from datetime import date as _date
from i18n import apply_locale


# ── Lokalizacja prerendera dla miast nie-PL (2026-06-11) ─────────────
# Strony SEO (profile/vote/session/term/budget) wstrzykują polskie frazy
# do już zlokalizowanego main_html, więc np. vilnius.radoskop.eu/vote/...
# pokazywał "Sesja", "Wynik: przyjete", "Jak glosowali radni" po polsku.
# Dla locale != "pl" title/description/extra_body przechodzą przez
# _localize_seo: najpierw normalizacja ASCII→PL (frazy prerendera są
# celowo pisane bez diakrytyków i nie matchowałyby kluczy i18n), potem
# apply_locale na katalogu danego locale. Dla "pl" zero zmian — output
# bajt w bajt jak dotychczas.
_SEO_ASCII_TO_PL = [
    ("Jak glosowali radni", "Jak głosowali radni"),
    ("Imienne glosy radnych", "Imienne głosy radnych"),
    ("Glosowania na tej sesji", "Głosowania na tej sesji"),
    ("Wstrzymali sie", "Wstrzymali się"),
    ("wstrzymalo sie", "wstrzymało się"),
    ("Wstrzymal sie", "Wstrzymał się"),
    ("wstrzymal sie", "wstrzymał się"),
    ("wstrzymane", "wstrzymało się"),
    ("Brak glosu", "Brak głosu"),
    ("Glosowanie", "Głosowanie"),
    ("glosowanie", "głosowanie"),
    ("Glosowania", "Głosowania"),
    ("glosowania", "głosowania"),
    ("Glosowan", "Głosowań"),
    ("glosowan", "głosowań"),
    ("przyjete", "przyjęte"),
    ("Uchwala", "Uchwała"),
    ("Budzet", "Budżet"),
    ("budzetu", "budżetu"),
    ("aktywnosc", "aktywność"),
]
# Ustawiane per-miasto w process_city. Dynamiczne pary obsługują frazy
# z odmienioną nazwą miasta ("Rady Miasta Vilniaus"), których nie da się
# trzymać w statycznym katalogu i18n.
_SEO_PRERENDER_LOCALE = "pl"
_SEO_DYNAMIC_PAIRS: list = []


def _localize_seo(text):
    """Tłumaczy frazę prerendera na locale bieżącego miasta.

    No-op dla locale "pl" (i pustych wartości) — polskie miasta zachowują
    dotychczasowy output co do bajta.
    """
    if not text or _SEO_PRERENDER_LOCALE == "pl":
        return text
    out = text
    for a, b in _SEO_DYNAMIC_PAIRS:
        out = out.replace(a, b)
    for a, b in _SEO_ASCII_TO_PL:
        out = re.sub(rf"(?<!\w){re.escape(a)}(?!\w)", b, out)
    return apply_locale(out, _SEO_PRERENDER_LOCALE)


def esc(text):
    """HTML-escape text for safe embedding."""
    return html.escape(str(text), quote=True)


def _jsonld_script(objs):
    """Render one or more schema.org objects as a <script type=ld+json>.

    Przyjmuje dict albo listę dictów. Lista >1 elementu trafia jako top-level
    JSON array (dozwolone w JSON-LD). '<' jest escapowane na \\u003c, żeby
    nazwa zawierająca '</script>' nie wybiła nas z bloku skryptu.
    """
    if not objs:
        return ""
    if isinstance(objs, dict):
        objs = [objs]
    payload = json.dumps(
        objs if len(objs) > 1 else objs[0],
        ensure_ascii=False, separators=(",", ":"),
    ).replace("<", "\\u003c")
    return f'<script type="application/ld+json">{payload}</script>\n'


def _breadcrumb(items):
    """Build a BreadcrumbList from [(name, url_or_None), ...].

    Ostatni element (bieżąca strona) może mieć url=None — Google tego nie
    wymaga. Elementy pośrednie powinny mieć url, inaczej crumb jest bezużyteczny.
    """
    elements = []
    for i, (name, url) in enumerate(items, 1):
        el = {"@type": "ListItem", "position": i, "name": name}
        if url:
            el["item"] = url
        elements.append(el)
    return {
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": elements,
    }


def _faqpage(qa):
    """Build FAQPage JSON-LD from [(question, answer_text), ...].

    UWAGA: Google wymaga, żeby ta sama treść Q&A była też widoczna w body
    strony (inaczej structured-data-only FAQ łamie wytyczne). Wołać razem z
    _faq_html na tym samym zestawie. FAQ rich result został wycofany dla
    komercji, ale utrzymany dla domen rządowych/obywatelskich, więc tu jest
    bezpieczny."""
    return {
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "mainEntity": [
            {
                "@type": "Question",
                "name": q,
                "acceptedAnswer": {"@type": "Answer", "text": a},
            }
            for q, a in qa
        ],
    }


def _faq_html(qa, heading="Najczęstsze pytania"):
    """Widoczny odpowiednik _faqpage — ten sam zestaw Q&A w HTML."""
    parts = [f"<h2>{esc(heading)}</h2>"]
    for q, a in qa:
        parts.append(f"<h3>{esc(q)}</h3>\n<p>{esc(a)}</p>")
    return "\n".join(parts) + "\n"


def _strlist(v):
    """Coerce roles/komisje (lista str lub dict) na listę napisów. Odporne na
    None, mieszane typy i dicty bez nazwy."""
    out = []
    if isinstance(v, list):
        for it in v:
            if isinstance(it, str):
                s = it.strip()
            elif isinstance(it, dict):
                s = (it.get("name") or it.get("label") or it.get("title") or "").strip()
            else:
                s = str(it).strip()
            if s:
                out.append(s)
    return out


def make_page(main_html, canonical_url, title, description, og_image=None, extra_body="", jsonld=None):
    """Create a page variant with unique SEO tags and optional body content."""
    # Lokalizacja fraz prerendera dla miast nie-PL (no-op dla "pl").
    title = _localize_seo(title)
    description = _localize_seo(description)
    extra_body = _localize_seo(extra_body)
    h = main_html

    # Usuń homepage'owy blok SEO ({{SEO_CONTENT}} = <section id="seo-content">…)
    # wstrzyknięty do index.html dla strony głównej. Na prerenderowanych
    # podstronach (profil/sesja/głosowanie) jego treść (landing miasta) nie ma
    # sensu i kolidowała id="seo-content" z blokiem per-strona niżej — przez co
    # zostawała widoczna po hydratacji. Strona główna (/) zachowuje swój blok,
    # bo nie przechodzi przez make_page.
    h = re.sub(
        r'<section id="seo-content">.*?</section>\s*'
        r'<script>var _sc=document\.getElementById\("seo-content"\);'
        r'if\(_sc\)_sc\.style\.display="none";</script>\s*',
        '',
        h,
        flags=re.S,
    )

    # Replace canonical
    h = re.sub(
        r'<link rel="canonical" href="[^"]*">',
        f'<link rel="canonical" href="{canonical_url}">',
        h
    )

    # Replace <title>
    h = re.sub(r'<title>[^<]*</title>', f'<title>{esc(title)}</title>', h)

    # Replace meta description
    h = re.sub(
        r'<meta name="description" content="[^"]*">',
        f'<meta name="description" content="{esc(description)}">',
        h
    )

    # Replace og:title
    h = re.sub(
        r'<meta property="og:title" content="[^"]*">',
        f'<meta property="og:title" content="{esc(title)}">',
        h
    )

    # Replace og:description
    h = re.sub(
        r'<meta property="og:description" content="[^"]*">',
        f'<meta property="og:description" content="{esc(description)}">',
        h
    )

    # Replace og:url
    h = re.sub(
        r'<meta property="og:url" content="[^"]*">',
        f'<meta property="og:url" content="{canonical_url}">',
        h
    )

    # Replace twitter:title
    h = re.sub(
        r'<meta name="twitter:title" content="[^"]*">',
        f'<meta name="twitter:title" content="{esc(title)}">',
        h
    )

    # Replace twitter:description
    h = re.sub(
        r'<meta name="twitter:description" content="[^"]*">',
        f'<meta name="twitter:description" content="{esc(description)}">',
        h
    )

    # Add og:image if provided (insert after og:url)
    if og_image:
        og_image_tag = f'<meta property="og:image" content="{og_image}">'
        og_image_tw = '<meta name="twitter:card" content="summary_large_image">'
        # Remove existing og:image if any
        h = re.sub(r'<meta property="og:image" content="[^"]*">\n?', '', h)
        # Change twitter card to summary_large_image
        h = re.sub(r'<meta name="twitter:card" content="[^"]*">', og_image_tw, h)
        # Insert og:image after og:url
        h = h.replace(
            f'<meta property="og:url" content="{canonical_url}">',
            f'<meta property="og:url" content="{canonical_url}">\n{og_image_tag}'
        )

    # Inject SEO body content (visible text for crawlers) before the loading div
    if extra_body:
        # Insert as a noscript-visible section right after <div id="loading">
        seo_block = f'\n<div id="seo-content" style="padding:20px;max-width:800px;margin:0 auto">\n{extra_body}\n</div>\n'
        # Hide seo-content once JS loads (the SPA will take over)
        hide_script = '<script>var sc=document.getElementById("seo-content");if(sc)sc.style.display="none";</script>\n'
        h = h.replace(
            '<div id="loading">',
            seo_block + hide_script + '<div id="loading">'
        )

    # Per-page structured data (Person / BreadcrumbList). Wstrzykiwane przed
    # </head>, niezależnie od sitewide WebApplication JSON-LD z head.html.
    if jsonld:
        h = h.replace('</head>', _jsonld_script(jsonld) + '</head>', 1)

    return h


def write_page(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


# Globalny przełącznik: gdy True, pomija pisanie tysięcy HTML SEO pages
# i generuje WYŁĄCZNIE sitemap.xml. Cloudflare Worker (radoskop-premium/
# cloudflare/worker.js) renderuje per-route meta tagi dynamicznie z S3
# template + JSON, więc statyczny pre-render nie jest potrzebny. Sitemap
# musi być statyczny żeby Google mógł go pobierać bez kosztu Workera.
SITEMAP_ONLY = False


def _maybe_write_page(path: Path, content: str):
    """Pisze tylko gdy SITEMAP_ONLY=False. Sitemap entries i tak są
    aktualizowane (sitemap_entries.append(...) leci niezależnie)."""
    if SITEMAP_ONLY:
        return
    write_page(path, content)


def _percentile_rank(value: float, sorted_values: list) -> int:
    """Return what % of values this value is higher than (0–100)."""
    if not sorted_values:
        return 0
    below = sum(1 for v in sorted_values if v < value)
    return round(below / len(sorted_values) * 100)


def _enrich_profiles_with_percentiles(profiles: list, city_slug: str, city_dir: Path) -> None:
    """Inject percentile_* fields into each profile's most recent kadencja.

    Reads councilor-percentiles.json from radoskop/docs/ (sibling of city_dir).
    Silently no-ops if the file is missing or the city has no tier mapping.
    Fields added to each kadencja entry:
      percentile_tier          str   e.g. 'medium'
      percentile_tier_label    str   e.g. '50–200 tys. mieszkańców'
      percentile_tier_n_cities int
      percentile_tier_n_councilors int
      percentile_aktywnosc     int   0–100 (higher = better)
      percentile_frekwencja    int   0–100
      percentile_zgodnosc      int   0–100
    """
    # city_dir = radoskop/cities/{slug}, so parent.parent = radoskop/
    percentiles_path = city_dir.parent.parent / 'docs' / 'councilor-percentiles.json'
    if not percentiles_path.exists():
        return

    try:
        with open(percentiles_path, 'r', encoding='utf-8') as f:
            pdata = json.load(f)
    except Exception:
        return

    tier_slug = pdata.get('city_tiers', {}).get(city_slug)
    if not tier_slug:
        return

    tier = pdata.get('tiers', {}).get(tier_slug)
    if not tier:
        return

    sorted_fr = tier.get('sorted_frekwencja', [])
    sorted_ak = tier.get('sorted_aktywnosc', [])
    sorted_zg = tier.get('sorted_zgodnosc_z_klubem', [])

    for p in profiles:
        kad_keys = sorted(p.get('kadencje', {}).keys(), reverse=True)
        if not kad_keys:
            continue
        kad = p['kadencje'][kad_keys[0]]
        if not kad.get('has_voting_data'):
            continue
        kad['percentile_tier'] = tier_slug
        kad['percentile_tier_label'] = tier.get('label', '')
        kad['percentile_tier_n_cities'] = tier.get('n_cities', 0)
        kad['percentile_tier_n_councilors'] = tier.get('n_councilors', 0)
        kad['percentile_frekwencja'] = _percentile_rank(kad.get('frekwencja', 0), sorted_fr)
        kad['percentile_aktywnosc'] = _percentile_rank(kad.get('aktywnosc', 0), sorted_ak)
        kad['percentile_zgodnosc'] = _percentile_rank(kad.get('zgodnosc_z_klubem', 0), sorted_zg)


def process_city(city_dir: Path, output_dir: Path | None = None, force: bool = False):
    """Generate all SEO pages for one city.

    Reads source data (config.json, index.html, profiles.json, data.json,
    kadencja-*.json) z `city_dir/docs/`. Pisze prerendered SEO HTML do
    `output_dir` (jeśli podany) albo do `city_dir/docs/` (default).

    Pipeline NAS używa output_dir do scratch dir żeby nie zaśmiecać
    monorepo radoskop tysiącami HTML-i — kanonicznie idą one do S3
    przez deploy_main_s3.py.
    """
    docs = city_dir / "docs"
    config_path = city_dir / "config.json"
    out = output_dir if output_dir is not None else docs

    if not docs.exists() or not config_path.exists():
        print(f"  Skipping {city_dir.name}: missing docs/ or config.json")
        return

    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)

    # Miasta z "disabled": true (np. paris w trybie frakcyjnym z danymi
    # przykładowymi, rostock zablokowany) nie generują stron SEO — żeby dane
    # niegotowe / przykładowe nie trafiły do sitemapy i Google.
    # --force (przekazywane przez run_pipeline gdy --city) omija ten check.
    if config.get("disabled") and not force:
        print(f"  Skipping {city_dir.name}: disabled w config.json (użyj --force żeby nadpisać)")
        return

    site_url = config["site_url"].rstrip("/")
    city_name = config["city_name"]
    city_gen = config["city_genitive"]
    locale = (config.get("locale") or "pl").lower()

    # Konfiguracja _localize_seo dla tego miasta. Frazy z odmienioną nazwą
    # ("Rady Miasta Vilniaus") tłumaczymy dynamicznie przez klucz katalogowy
    # "Rada Miasta {{CITY_GENITIVE}}", w którym locale może przestawić szyk
    # (np. lt: "{{CITY_GENITIVE}} miesto savivaldybės taryba").
    global _SEO_PRERENDER_LOCALE, _SEO_DYNAMIC_PAIRS
    _SEO_PRERENDER_LOCALE = locale
    if locale != "pl":
        council = (
            apply_locale("Rada Miasta {{CITY_GENITIVE}}", locale)
            .replace("{{CITY_GENITIVE}}", city_gen)
            .replace("{{CITY_NAME}}", city_name)
        )
        _SEO_DYNAMIC_PAIRS = [
            (f"Radzie Miasta {city_gen}", council),
            (f"Rady Miasta {city_gen}", council),
            (f"Rada Miasta {city_gen}", council),
        ]
    else:
        _SEO_DYNAMIC_PAIRS = []

    # Path slug map. Po migracji 2026-05 wszystkie miasta używają
    # angielskich slugów dla URL paths. Mapping musi zgadzać się z
    # apply_english_paths() w generate_site.py i PATH_REDIRECTS w
    # radoskop-premium/cloudflare/worker.js. Worker robi 301 ze starych
    # polskich URL-i, więc Google index zachowuje link equity.
    SLUG = {
        "profile": "profile",
        "vote": "vote",
        "session": "session",
        "term": "term",
        "budget": "budget",
        "reports": "reports",
        "tab_profiles": "councillors",
        "tab_sessions": "sessions",
        "tab_votes": "votes",
        "tab_similarity": "similarity",
        "tab_interpelacje": "interpellations",
    }

    # Read main index.html
    main_html_path = docs / "index.html"
    with open(main_html_path, "r", encoding="utf-8") as f:
        main_html = f.read()

    # Load profiles
    profiles = []
    profiles_path = docs / "profiles.json"
    if profiles_path.exists():
        with open(profiles_path, "r", encoding="utf-8") as f:
            profiles = json.load(f).get("profiles", [])

    # Enrich profiles with cross-city percentile data (if available).
    city_slug = city_dir.name
    _enrich_profiles_with_percentiles(profiles, city_slug, city_dir)

    # Interpelacje (zapytania radnych) — zasila zakładkę interpelacji realną,
    # unikalną treścią. Zapotrzebowanie w Search Console jest realne
    # ("interpelacje {miasto}", "interpelacje radnych {miasto}"), a strona
    # rankowała ~7–9 z pustym body (sam nagłówek) i zerowym CTR. Plik
    # docs/interpelacje.json jest gitignored, ale istnieje lokalnie w czasie
    # generacji. Dwa kształty (lista albo {"items": [...]}), oba obsłużone.
    interp_items = []
    interp_path = docs / "interpelacje.json"
    if interp_path.exists():
        try:
            with open(interp_path, "r", encoding="utf-8") as f:
                _iraw = json.load(f)
            if isinstance(_iraw, list):
                interp_items = _iraw
            elif isinstance(_iraw, dict):
                interp_items = _iraw.get("interpelacje") or _iraw.get("items") or []
        except Exception:
            interp_items = []
    # Mapa nazwisko → slug profilu dla linków wewnętrznych z listy interpelacji.
    _profile_slug_by_name = {
        (p.get("name") or "").strip(): p.get("slug", "")
        for p in profiles if p.get("name") and p.get("slug")
    }

    # Load data.json for kadencje index
    kadencje = []
    data_path = docs / "data.json"
    if data_path.exists():
        with open(data_path, "r", encoding="utf-8") as f:
            kadencje = json.load(f).get("kadencje", [])

    # Extract KAD_SLUGS from JS
    KAD_SLUGS = {}
    kad_match = re.search(r"const\s+KAD_SLUGS\s*=\s*\{([^}]+)\}", main_html)
    if kad_match:
        for m in re.finditer(r"'([^']+)'\s*:\s*'([^']+)'", kad_match.group(1)):
            KAD_SLUGS[m.group(1)] = m.group(2)
    if not KAD_SLUGS:
        for k in kadencje:
            KAD_SLUGS[k["id"]] = k["id"]

    sitemap_entries = []

    # Lista radnych (/councillors/) gdy miasto ma landing. Od 2026-06-06 to
    # NIE jest statyczna strona (build_landing.py wycofany, orphan w S3
    # kasowany przez deploy --delete) — URL obsługuje SPA fallback workera,
    # router SPA mapuje /councillors/ na tab ranking. Wpis w sitemap zostaje,
    # bo Googlebot wykonuje JS.
    if config.get("landing_enabled"):
        sitemap_entries.append({
            "loc": f"{site_url}/councillors/",
            "changefreq": "weekly", "priority": "0.9",
        })

    # ════════════════════════════════════════════
    # 1. Profile pages
    # ════════════════════════════════════════════
    profile_count = 0
    for p in profiles:
        slug = p["slug"]
        name = p["name"]

        # Get stats from most recent kadencja
        kad_keys = sorted(p.get("kadencje", {}).keys(), reverse=True)
        kad = p["kadencje"][kad_keys[0]] if kad_keys else {}

        club = kad.get("club", "")
        club_full = kad.get("club_full", club)
        frekwencja = kad.get("frekwencja", 0)
        aktywnosc = kad.get("aktywnosc", 0)
        zgodnosc = kad.get("zgodnosc_z_klubem", 0)
        votes_za = kad.get("votes_za", 0)
        votes_przeciw = kad.get("votes_przeciw", 0)
        votes_wstrzymal = kad.get("votes_wstrzymal", 0)

        canonical = f"{site_url}/{SLUG['profile']}/{slug}/"

        # Dodatkowe pola pod SEO/CTR (mog\u0105 nie istnie\u0107 w starszych profiles.json).
        has_vd = bool(kad.get("has_voting_data"))
        votes_total = kad.get("votes_total") or (votes_za + votes_przeciw + votes_wstrzymal)
        rebellion = kad.get("rebellion_count") or 0
        perc_fr = kad.get("percentile_frekwencja")
        tier_label = kad.get("percentile_tier_label") or ""
        roles = _strlist(kad.get("roles"))
        komisje = _strlist(kad.get("komisje"))
        okreg = kad.get("okr\u0119g")

        club_short = (club or "").strip()
        _club_low = club_short.lower()
        club_is_none = _club_low in ("", "niezrzeszony", "niezrzeszona",
                                     "niezrzeszeni", "?", "brak", "-")
        club_paren = f" ({club_short})" if not club_is_none else ""

        # is_pl: polskie miasta (i sejmiki) dostaj\u0105 pe\u0142n\u0105 sprzeda\u017cow\u0105 kopi\u0119.
        # Obce lokalizacje (nl/cs/de/...) NIE przechodz\u0105 przez apply_locale na
        # tym etapie (wstrzykujemy gotowe stringi do ju\u017c-zlokalizowanego
        # main_html), wi\u0119c nowe polskie frazy marketingowe by tam zosta\u0142y po
        # polsku. Dla non-pl trzymamy si\u0119 rzeczowych fraz z katalogu i2n
        # ("Rada Miasta {gen}" t\u0142umaczy si\u0119 per-locale w main_html; tu zostaj\u0105
        # rzeczownik + liczby), bez "jak g\u0142osuje"/"Zapis g\u0142osowa\u0144".
        is_pl = locale == "pl"

        # Title: nazwisko z przodu (prze\u017cyje uci\u0119cie w SERP), obietnica tre\u015bci
        # kt\u00f3rej szuka pytaj\u0105cy o polityka ("jak g\u0142osuje" \u2014 bezp\u0142ciowo, brak pola
        # p\u0142ci), rada w mianowniku "Rada Miasta {gen}" (na sejmikach
        # _assembly_transform zamienia to na "Sejmik Wojew\u00f3dztwa {gen}").
        if has_vd and is_pl:
            title = f"{name}{club_paren} \u2013 jak g\u0142osuje, Rada Miasta {city_gen}"
        else:
            title = f"{name}{club_paren} \u2013 Rada Miasta {city_gen}"

        # Description: konkretne liczby (renderuj\u0105 si\u0119 w SERP i nap\u0119dzaj\u0105 klik),
        # bezp\u0142ciowo, nazwisko w mianowniku (bez wymuszania odmiany imienia).
        if has_vd and is_pl:
            bits = [
                f"{votes_total} g\u0142osowa\u0144",
                f"frekwencja {frekwencja:.0f}%",
                f"zgodno\u015b\u0107 z klubem {zgodnosc:.0f}%",
            ]
            if rebellion:
                bits.append(f"{rebellion} razy wbrew klubowi")
            desc = (
                f"Zapis g\u0142osowa\u0144: {name}{club_paren}, Rada Miasta {city_gen}. "
                + ", ".join(bits)
                + ". Sprawd\u017a pe\u0142n\u0105 aktywno\u015b\u0107 i przynale\u017cno\u015b\u0107 klubow\u0105."
            )
        elif has_vd:
            desc = (
                f"{name}{club_paren}, Rada Miasta {city_gen}. "
                f"Frekwencja {frekwencja:.0f}%, zgodno\u015b\u0107 z klubem {zgodnosc:.0f}%."
            )
        else:
            desc = (
                f"{name}{club_paren} \u2013 Rada Miasta {city_gen}. "
                f"Sk\u0142ad rady, kluby i aktywno\u015b\u0107 radnych w serwisie Radoskop."
            )

        og_img = f"{site_url}/{SLUG['profile']}/{slug}/og.png"
        # OG image cache lookup: pierwsze sprawdzamy now\u0105 \u015bcie\u017ck\u0119 (locale-aware),
        # potem legacy /profil/ \u2014 bo generate_og_images.py m\u00f3g\u0142 jeszcze nie
        # przej\u015b\u0107 na nowy slug, a chcemy zachowa\u0107 obrazek dop\u00f3ki tam jest.
        og_img_path = docs / SLUG["profile"] / slug / "og.png"
        if not og_img_path.exists():
            og_img_path_legacy = docs / "profil" / slug / "og.png"
            if og_img_path_legacy.exists():
                og_img_path = og_img_path_legacy
            else:
                og_img = None

        # Body widoczne dla crawlera (chowane po hydratacji). Unikalny, faktyczny
        # tekst per radny + opisowe linki wewnętrzne — podbija trafność encji dla
        # zapytań nazwiskiem i rozprowadza link equity na zakładkę aktywności.
        body_parts = [f"<h1>{esc(name)}{esc(club_paren)}</h1>"]
        if is_pl:
            intro = f"{esc(name)} zasiada w Radzie Miasta {esc(city_gen)}"
            if club_full and not club_is_none:
                intro += f", klub {esc(club_full)}"
            if okreg:
                intro += f", okręg {esc(str(okreg))}"
            intro += "."
            body_parts.append(f"<p>{intro}</p>")
            if roles:
                body_parts.append(f"<p>Funkcje: {esc(', '.join(roles))}.</p>")
            if has_vd:
                body_parts.append(
                    f"<p>Frekwencja na sesjach: {frekwencja:.0f}%. "
                    f"Zgodność głosowań z klubem: {zgodnosc:.0f}%. "
                    f"Aktywność: {aktywnosc:.0f}%.</p>"
                )
                if perc_fr is not None and tier_label:
                    body_parts.append(
                        f"<p>Frekwencja wyższa niż u {perc_fr}% radnych "
                        f"w miastach kategorii {esc(tier_label)}.</p>"
                    )
                vline = (
                    f"Zarejestrowane głosowania: {votes_total}, w tym "
                    f"za {votes_za}, przeciw {votes_przeciw}, wstrzymane {votes_wstrzymal}."
                )
                if rebellion:
                    vline += f" Liczba głosów niezgodnych z klubem: {rebellion}."
                body_parts.append(f"<p>{vline}</p>")
            if komisje:
                body_parts.append(f"<p>Komisje: {esc(', '.join(komisje))}.</p>")
            body_parts.append(
                f"<p>Zobacz <a href=\"{canonical}?tab=activity\">głosowania i interpelacje: {esc(name)}</a>, "
                f"<a href=\"{site_url}/{SLUG['profile']}/\">wszystkich radnych {esc(city_gen)}</a> oraz "
                f"<a href=\"{site_url}/\">aktualne dane Rady Miasta {esc(city_gen)}</a>.</p>"
            )
        else:
            # Non-PL: rzeczowe frazy etykietowe zamiast polskiej prozy —
            # wszystkie etykiety mają klucze w katalogach i18n, a _localize_seo
            # tłumaczy je w make_page. Pomijamy linijkę percentyla, bo
            # tier_label przyjeżdża z danych po polsku.
            intro = f"{esc(name)} – Rada Miasta {esc(city_gen)}"
            if club_full and not club_is_none:
                intro += f". Klub: {esc(club_full)}"
            if okreg:
                intro += f". Okręg wyborczy: {esc(str(okreg))}"
            intro += "."
            body_parts.append(f"<p>{intro}</p>")
            if roles:
                body_parts.append(f"<p>Funkcje: {esc(', '.join(roles))}.</p>")
            if has_vd:
                body_parts.append(
                    f"<p>Frekwencja: {frekwencja:.0f}%. "
                    f"Zgodność z klubem: {zgodnosc:.0f}%. "
                    f"Aktywność: {aktywnosc:.0f}%.</p>"
                )
                vline = (
                    f"Głosowania: {votes_total}. Za: {votes_za} · "
                    f"Przeciw: {votes_przeciw} · Wstrzymał się: {votes_wstrzymal}."
                )
                if rebellion:
                    vline += f" Głosy wbrew klubowi: {rebellion}."
                body_parts.append(f"<p>{vline}</p>")
            if komisje:
                body_parts.append(f"<p>Komisje: {esc(', '.join(komisje))}.</p>")
            body_parts.append(
                f"<p><a href=\"{canonical}?tab=activity\">{esc(name)}: Głosowania · Interpelacje</a> · "
                f"<a href=\"{site_url}/{SLUG['profile']}/\">Profile radnych</a> · "
                f"<a href=\"{site_url}/\">Radoskop {esc(city_name)}</a></p>"
            )
        body = "\n".join(body_parts) + "\n"

        # Structured data: Person (radny) + breadcrumb. To jest główny zysk
        # SEO dla wyszukiwań nazwiskiem radnego — pomaga Google rozpoznać encję
        # i pokazać breadcrumb w wynikach.
        person = {
            "@context": "https://schema.org",
            "@type": "Person",
            "name": name,
            "url": canonical,
            "jobTitle": "radny miejski",
            "memberOf": {
                "@type": "GovernmentOrganization",
                "name": f"Rada Miasta {city_gen}",
            },
        }
        if og_img:
            person["image"] = og_img
        # hasOccupation + knowsAbout wzmacniają encytyzację (Google rozpoznaje
        # osobę-byt) bez nowych danych: occupation z roli radnego, knowsAbout z
        # komisji w których zasiada.
        person["hasOccupation"] = {
            "@type": "Occupation",
            "name": f"Radny Rady Miasta {city_gen}",
        }
        if komisje:
            person["knowsAbout"] = komisje
        _club_norm = (club_full or "").strip().lower()
        if club_full and _club_norm not in ("niezrzeszeni", "niezrzeszony", "niezrzeszona", "?", ""):
            person["affiliation"] = {"@type": "Organization", "name": club_full}
        crumbs = _breadcrumb([
            (f"Radoskop {city_name}", f"{site_url}/"),
            (f"Radni {city_gen}", f"{site_url}/{SLUG['profile']}/"),
            (name, canonical),
        ])

        # ProfilePage jako typ nadrzędny — Google preferuje go dla stron-wizytówek
        # osób publicznych (mainEntity → Person), lepsza encytyzacja niż samo
        # Person. dateModified sygnalizuje świeżość (przeciwko nieświeżym
        # prerenderom). Person traci własny @context, bo jest teraz zagnieżdżony.
        person.pop("@context", None)
        profile_page = {
            "@context": "https://schema.org",
            "@type": "ProfilePage",
            "dateModified": _date.today().isoformat(),
            "mainEntity": person,
        }

        page = make_page(main_html, canonical, title, desc, og_image=og_img, extra_body=body, jsonld=[profile_page, crumbs])
        _maybe_write_page(out / SLUG["profile"] / slug / "index.html", page)
        profile_count += 1

        sitemap_entries.append({"loc": canonical, "changefreq": "weekly", "priority": "0.7"})

    print(f"  {profile_count} profile pages")

    # ════════════════════════════════════════════
    # 2. Vote pages (per kadencja)
    # ════════════════════════════════════════════
    vote_count = 0
    # Mapa data → rzymski numer sesji. Po migracji ID głosowań z formatu
    # DATA_NNN na DATA_RZYMSKI_NNN (np. 2024-12-18_011 → 2024-12-18_VII_011)
    # stare permalinki z indeksu Google nie pasują do nowych ID i dają
    # soft-404. Worker (radoskop-premium/cloudflare/worker.js) czyta tę mapę
    # z /_redirects/votes.json i robi 301 ze starego DATA_NNN na nowy
    # DATA_RZYMSKI_NNN. NNN jest zachowane między scrape'ami (zweryfikowane).
    # Daty z więcej niż jednym rzymskim (kolizja dwóch sesji w jednym dniu)
    # są pomijane — nie da się jednoznacznie zmapować DATA_NNN.
    vote_id_romans: dict[str, str] = {}
    vote_id_roman_conflict: set[str] = set()
    # Liczność głosowań per data, per kadencja, dla ID w formacie
    # DATA_NNN_MMM (eSesja/bip_static). Z tego liczymy mapę offsetów dla
    # workera (/_redirects/vote_offsets.json): miasta które przeszły z
    # globalnej numeracji głosowań (np. Łódź: 2025-10-22_1205_000, licznik
    # ciągły przez całą kadencję) na per-sesyjną (2025-10-22_036_000)
    # zostawiły w indeksie Google stare URL-e, które lądują w SPA fallback
    # jako soft-404 / "Alternate page with proper canonical tag" w GSC.
    # global_start(data) = 1 + suma liczności wcześniejszych dat w kadencji.
    vote_seq_counts: dict[str, dict[str, int]] = {}
    for k in kadencje:
        kid = k.get("id", "")
        kad_file = docs / f"kadencja-{kid}.json"
        if not kad_file.exists():
            continue

        with open(kad_file, "r", encoding="utf-8") as f:
            kad_data = json.load(f)

        # Roster do rozwiązywania named_votes (indeksy → "Nazwisko (Klub)").
        # Bez rozwinięcia imiennych głosów strony głosowań różniły się tylko
        # 4 linijkami na ~15KB wspólnego szkieletu i Google klastrował je
        # jako duplikaty ("Duplicate, Google chose different canonical than
        # user" w GSC, narastająco od 2026-05). Imienna rozpiska jest
        # unikalna per głosowanie i to jest właściwa treść strony.
        # Roster jako pary (nazwisko, klub) — pozwala rozbić głosy per klub
        # i linkować nazwiska do profili. _cnames zostaje dla zgodności.
        _council_entries: list[tuple[str, str]] = []
        for _c in kad_data.get("councilors") or []:
            if isinstance(_c, dict):
                _nm = (_c.get("name", "") or "").strip()
                _cl = (_c.get("club") or "").strip()
                _council_entries.append((_nm, _cl if _cl and _cl != "?" else ""))
            else:
                _council_entries.append((str(_c), ""))
        _cnames = [f"{n} ({c})" if c else n for (n, c) in _council_entries]

        def _nv_names(lst):
            # Miasta faction-mode trzymają w named_votes inne struktury niż
            # listy — wtedy nic nie rozwijamy (strona zostaje bez rozpiski).
            if not isinstance(lst, list):
                return []
            resolved = []
            for _x in lst:
                if isinstance(_x, int):
                    if 0 <= _x < len(_cnames):
                        resolved.append(_cnames[_x])
                elif isinstance(_x, str):
                    resolved.append(_x)
            return resolved

        def _nv_entries(lst):
            # Jak _nv_names, ale zwraca pary (nazwisko, klub) — do rozbicia
            # klubowego i linków do profili.
            if not isinstance(lst, list):
                return []
            out: list[tuple[str, str]] = []
            for _x in lst:
                if isinstance(_x, int):
                    if 0 <= _x < len(_council_entries):
                        out.append(_council_entries[_x])
                elif isinstance(_x, str):
                    out.append((_x, ""))
            return out

        # Slug kadencji + zbiór realnych numerów sesji (dla bezpiecznych
        # linków wewnętrznych — linkujemy do strony sesji tylko gdy istnieje).
        _kslug = KAD_SLUGS.get(kid, kid)
        _session_numbers = {
            str(s.get("number")) for s in kad_data.get("sessions", [])
            if s.get("number")
        }

        # Indeks numer sesji → [(vid, topic), ...] do linkowania sąsiednich
        # głosowań (variation-to-variation). Każda strona głosowania linkuje do
        # pozostałych z tej samej sesji, co rozprowadza link equity i leczy
        # "Discovered/Crawled – currently not indexed" w GSC.
        _session_vote_index: dict[str, list[tuple[str, str]]] = {}
        for _v in kad_data.get("votes", []):
            _vsn = str(_v.get("session_number") or "")
            _vvid = _v.get("id", "")
            if _vsn and _vvid:
                _session_vote_index.setdefault(_vsn, []).append(
                    (_vvid, (_v.get("topic", "") or "").strip())
                )

        for vote in kad_data.get("votes", []):
            vid = vote.get("id", "")
            if not vid:
                continue

            # Zbierz data → rzymski dla mapy redirectów (tylko ID w formacie
            # DATA_RZYMSKI_NNN). Inne formaty (np. DATA_NNN bez rzymskiego)
            # nie wymagają remapowania, więc je pomijamy.
            _vm = re.match(r"^(\d{4}-\d{2}-\d{2})_([IVXLC]+)_\d+$", vid)
            if _vm:
                _vdate, _vroman = _vm.group(1), _vm.group(2)
                _prev = vote_id_romans.get(_vdate)
                if _prev is not None and _prev != _vroman:
                    vote_id_roman_conflict.add(_vdate)
                else:
                    vote_id_romans[_vdate] = _vroman

            # Liczność per data dla mapy offsetów (tylko format DATA_NNN_MMM).
            _gm = re.match(r"^(\d{4}-\d{2}-\d{2})_\d{3}_\d{3}$", vid)
            if _gm:
                _kc = vote_seq_counts.setdefault(kid, {})
                _kc[_gm.group(1)] = _kc.get(_gm.group(1), 0) + 1

            topic = vote.get("topic", "").replace(";", "").strip()
            counts = vote.get("counts", {})
            za = counts.get("za", 0)
            przeciw = counts.get("przeciw", 0)
            wstrzymal = counts.get("wstrzymal_sie", 0)
            session_date = vote.get("session_date", "")
            session_number = vote.get("session_number", "")

            if za > przeciw:
                result = "przyjete"
            elif przeciw > za:
                result = "odrzucone"
            else:
                result = "remis"

            canonical = f"{site_url}/{SLUG['vote']}/{vid}/"
            # Etykieta sesji bez pustego numeru ("Sesja , 2024-12-12" gdy
            # scrape nie daje session_number, np. Warszawa).
            sess_label = (
                f"Sesja {session_number}, {session_date}" if session_number
                else f"Sesja z {session_date}"
            )
            # Numer g\u0142osowania z ID do disambiguacji title \u2014 sesje bud\u017cetowe
            # maj\u0105 dziesi\u0105tki g\u0142osowa\u0144 o identycznych tematach ("g\u0142osowanie
            # poprawki nr 3..."), a topic[:80] dodatkowo skleja\u0142 r\u00f3\u017cne tematy
            # w ten sam title.
            # Numer bierzemy z części ID PO dacie, żeby nie złapać roku
            # z daty wewnątrz ID (np. copenhagen_2026-01-22_punkt-10).
            _vid_tail = (
                vid.split(session_date, 1)[1]
                if session_date and session_date in vid else vid
            )
            _vseq_m = re.findall(r"_(\d+)", _vid_tail)
            vote_no = str(int(_vseq_m[0])) if _vseq_m else ""
            title_suffix = (
                f" ({session_date}, glosowanie {vote_no})" if vote_no
                else f" ({session_date})"
            ) if session_date else ""
            title_text = topic[:70] if topic else f"Glosowanie {vid}"
            title = f"{title_text}{title_suffix} \u2013 Radoskop {city_name}"
            desc = (
                f"Glosowanie: {topic[:120]}. "
                f"Wynik: {result} (za {za}, przeciw {przeciw}, "
                f"wstrzymal sie {wstrzymal}). "
                f"{sess_label}, Radoskop {city_name}. "
                f"Imienne glosy radnych i rozbicie klubowe."
            )

            og_img = f"{site_url}/{SLUG['vote']}/{vid}/og.png"
            og_img_path = docs / SLUG["vote"] / vid / "og.png"
            if not og_img_path.exists():
                og_img_legacy = docs / "glosowanie" / vid / "og.png"
                if og_img_legacy.exists():
                    og_img_path = og_img_legacy
                else:
                    og_img = None

            # Imienna rozpiska głosów + rozbicie klubowe + linki wewnętrzne —
            # unikalny, merytoryczny content per strona. Każda strona różni się
            # teraz pełną rozpiską imienną (z linkami do profili), tabelą głosów
            # per klub i linkami do sesji/listy głosowań, co eliminuje
            # klastrowanie "Duplicate canonical" i wzmacnia graf linków pod
            # "Crawled/Discovered – currently not indexed" w GSC.
            nv = vote.get("named_votes") or {}
            _choices = (
                ("za", "Za"),
                ("przeciw", "Przeciw"),
                ("wstrzymal_sie", "Wstrzymali sie"),
                ("brak_glosu", "Brak glosu"),
                ("nieobecni", "Nieobecni"),
            )

            # Rozbicie głosów per klub (unikalne per głosowanie).
            club_tally: dict[str, dict[str, int]] = {}
            if isinstance(nv, dict):
                for _ckey, _ in _choices:
                    for _nm, _cl in _nv_entries(nv.get(_ckey)):
                        _club = _cl or "Niezrzeszeni"
                        _row = club_tally.setdefault(_club, {})
                        _row[_ckey] = _row.get(_ckey, 0) + 1
            club_html = ""
            if len(club_tally) > 1:
                _rows = ""
                for _club in sorted(club_tally):
                    _t = club_tally[_club]
                    _rows += (
                        f"<tr><td>{esc(_club)}</td>"
                        f"<td>{_t.get('za', 0)}</td>"
                        f"<td>{_t.get('przeciw', 0)}</td>"
                        f"<td>{_t.get('wstrzymal_sie', 0)}</td></tr>\n"
                    )
                club_html = (
                    "<h2>Jak glosowaly kluby</h2>\n"
                    "<table><thead><tr><th>Klub</th><th>Za</th>"
                    "<th>Przeciw</th><th>Wstrzymali sie</th></tr></thead>\n"
                    f"<tbody>\n{_rows}</tbody></table>\n"
                )

            # Imienna rozpiska z linkami do profili radnych.
            nv_html = ""
            if isinstance(nv, dict):
                for _nkey, _nlabel in _choices:
                    _entries = _nv_entries(nv.get(_nkey))
                    if not _entries:
                        continue
                    _parts = []
                    for _nm, _cl in _entries:
                        _disp = esc(_nm + (f" ({_cl})" if _cl else ""))
                        _ps = _profile_slug_by_name.get(_nm.strip())
                        if _ps:
                            _parts.append(
                                f'<a href="{site_url}/{SLUG["profile"]}/{_ps}/">'
                                f"{_disp}</a>"
                            )
                        else:
                            _parts.append(_disp)
                    nv_html += (
                        f"<h3>{_nlabel} ({len(_entries)})</h3>\n"
                        "<p>" + ", ".join(_parts) + "</p>\n"
                    )
            if nv_html:
                nv_html = "<h2>Jak glosowali radni</h2>\n" + nv_html

            ref_html = ""
            if vote.get("resolution"):
                ref_html += f"<p>Uchwala: {esc(str(vote['resolution']))}</p>\n"
            if vote.get("druk"):
                ref_html += f"<p>Druk: {esc(str(vote['druk']))}</p>\n"

            # Linki wewnętrzne: sesja (tylko gdy strona sesji istnieje), pełna
            # lista głosowań kadencji, profile radnych.
            nav_links = []
            if session_number and str(session_number) in _session_numbers:
                nav_links.append(
                    f'<a href="{site_url}/{SLUG["session"]}/'
                    f'{esc(str(session_number))}/">'
                    f"Cala sesja {esc(str(session_number))}</a>"
                )
            nav_links.append(
                f'<a href="{site_url}/{SLUG["term"]}/{_kslug}/'
                f'{SLUG["tab_votes"]}/">Wszystkie glosowania</a>'
            )
            nav_links.append(
                f'<a href="{site_url}/{SLUG["profile"]}/">Profile radnych</a>'
            )
            nav_html = "<p>" + " · ".join(nav_links) + "</p>\n"

            # Sąsiednie głosowania z tej samej sesji (variation-to-variation).
            siblings_html = ""
            _sibs = [
                (_svid, _stopic) for (_svid, _stopic)
                in _session_vote_index.get(str(session_number), [])
                if _svid != vid
            ][:8]
            if _sibs:
                _sib_items = "".join(
                    f'<li><a href="{site_url}/{SLUG["vote"]}/{_svid}/">'
                    f"{esc(_stopic[:80] or _svid)}</a></li>\n"
                    for _svid, _stopic in _sibs
                )
                siblings_html = (
                    "<h2>Inne glosowania z tej sesji</h2>\n<ul>\n"
                    + _sib_items
                    + "</ul>\n"
                )

            body = (
                f"<h1>{esc(topic or f'Glosowanie {vid}')}</h1>\n"
                f"<p>{esc(sess_label)}"
                + (f" · Glosowanie nr {esc(vote_no)}" if vote_no else "")
                + "</p>\n"
                f"<p>Wynik: <strong>{result}</strong> — za {za}, "
                f"przeciw {przeciw}, wstrzymalo sie {wstrzymal}.</p>\n"
                + ref_html
                + club_html
                + nv_html
                + siblings_html
                + nav_html
                + f"<p><a href=\"{site_url}/\">Radoskop {esc(city_name)}</a></p>\n"
            )

            crumbs = _breadcrumb([
                (f"Radoskop {city_name}", f"{site_url}/"),
                ((topic[:90] or f"Glosowanie {vid}"), canonical),
            ])

            # Dataset: każde głosowanie to maszynowo czytelny zbiór danych
            # (wyniki + imienne głosy). Daje odkrywalność w Google Dataset
            # Search — kanał ruchu od dziennikarzy/analityków. distribution
            # wskazuje na publiczny plik kadencji (JSON), w którym ten głos
            # realnie siedzi. JSON-LD nie przechodzi przez _localize_seo, więc
            # ramka jest po polsku dla wszystkich miast — spójnie z blokiem
            # Person.
            dataset = {
                "@context": "https://schema.org",
                "@type": "Dataset",
                "name": f"Wyniki głosowania: {(topic[:90] or vid)}"
                        + (f" ({session_date})" if session_date else ""),
                "description": (
                    f"Imienne wyniki głosowania Rady Miasta {city_gen}. "
                    f"Za: {za}, przeciw: {przeciw}, wstrzymało się: {wstrzymal}. "
                    f"Wynik: {result}. {sess_label}."
                ),
                "url": canonical,
                "isAccessibleForFree": True,
                "creator": {
                    "@type": "GovernmentOrganization",
                    "name": f"Rada Miasta {city_gen}",
                },
                "variableMeasured": [
                    "Liczba głosów za",
                    "Liczba głosów przeciw",
                    "Liczba głosów wstrzymujących się",
                    "Imienne głosy radnych",
                ],
                "distribution": [{
                    "@type": "DataDownload",
                    "encodingFormat": "application/json",
                    "contentUrl": f"{site_url}/kadencja-{kid}.json",
                }],
            }
            if session_date:
                dataset["temporalCoverage"] = session_date
            if og_img:
                dataset["image"] = og_img

            page = make_page(main_html, canonical, title, desc, og_image=og_img, extra_body=body, jsonld=[dataset, crumbs])
            _maybe_write_page(out / SLUG["vote"] / vid / "index.html", page)
            vote_count += 1

            sitemap_entries.append({"loc": canonical, "changefreq": "monthly", "priority": "0.5"})

    print(f"  {vote_count} vote pages")

    # ════════════════════════════════════════════
    # 3. Session pages (podsumowania posesyjne)
    # ════════════════════════════════════════════
    # Pełne podsumowanie sesji (2026-06-10): statystyki, najbardziej sporne
    # głosowania, nieobecni radni, nawigacja poprzednia/następna sesja,
    # JSON-LD Article. Heurystyka spornych współdzielona z frontendem przez
    # lib_session_summary.py. Strona ma działać jako samodzielny, cytowalny
    # news ("co uchwaliła rada") dla mediów i Google Discover.
    profile_slug_by_name = {
        p.get("name", ""): p.get("slug", "") for p in profiles
    }
    session_count = 0
    today = _date.today()
    for k in kadencje:
        kid = k.get("id", "")
        kad_file = docs / f"kadencja-{kid}.json"
        if not kad_file.exists():
            continue

        with open(kad_file, "r", encoding="utf-8") as f:
            kad_data = json.load(f)

        all_votes = kad_data.get("votes", []) or []
        councilors = kad_data.get("councilors", []) or []

        # Sesje z poprawnym numerem, chronologicznie — do linków
        # poprzednia/następna. Guard numeru jak dotychczas (przeniesiony do
        # lib_session_summary.valid_session_number): zepsuta ekstrakcja ze
        # scrape'a nie może wyprodukować brzydkiego URL-a, który utknie w
        # Google index.
        ordered_sessions = []
        for s in kad_data.get("sessions", []):
            snum = s.get("number", "")
            if not snum:
                continue
            if not valid_session_number(snum):
                print(f"  skipping invalid session number: {str(snum).strip()!r}")
                continue
            ordered_sessions.append(s)
        ordered_sessions.sort(key=lambda s: (s.get("date") or "", str(s.get("number") or "")))

        for si, s in enumerate(ordered_sessions):
            snum = str(s.get("number")).strip()
            sdate = s.get("date", "")
            sess_votes = session_votes(s, all_votes)
            summary = summarize_session(s, sess_votes, councilors)
            vote_cnt = summary["vote_count"] or s.get("vote_count", 0)
            attendee_cnt = summary["attendee_count"] or s.get("attendee_count", 0)

            canonical = f"{site_url}/{SLUG['session']}/{snum}/"
            title = f"Sesja {snum} ({sdate}) – Radoskop {city_name}"
            if sess_votes:
                desc = (
                    f"Sesja {snum} Rady Miasta {city_gen}, {sdate}: "
                    f"{vote_cnt} głosowań, {summary['passed']} przyjętych, "
                    f"{summary['contested_count']} spornych, "
                    f"{attendee_cnt} obecnych radnych. Imienne wyniki głosowań."
                )
            else:
                desc = (
                    f"Sesja {snum} Rady Miasta {city_gen}, {sdate}. "
                    f"{vote_cnt} glosowan, {attendee_cnt} obecnych radnych."
                )

            body_parts = [f"<h1>Sesja {esc(snum)} Rady Miasta {esc(city_gen)}</h1>"]

            if sess_votes:
                # Notacja dwukropkowa zamiast pełnych zdań — liczby polskie
                # wymagałyby odmiany liczebnikowej (42 rozstrzygnięcia vs
                # 45 rozstrzygnięć), której nie chcemy liczyć per wartość.
                intro = (
                    f"Sesja {esc(snum)} Rady Miasta {esc(city_gen)} odbyła się "
                    f"{esc(sdate)}. Radni głosowali {vote_cnt} razy. "
                    f"Wyniki przyjęte: {summary['passed']}, "
                    f"odrzucone: {summary['rejected']}, "
                    f"jednogłośne: {summary['unanimous']}"
                )
                if summary["contested_count"]:
                    intro += (
                        f", sporne (wyraźnie podzielona rada): "
                        f"{summary['contested_count']}"
                    )
                intro += "."
                body_parts.append(f"<p>{intro}</p>")
                obecni_line = f"Obecnych radnych: {attendee_cnt}."
                if summary["absent"]:
                    obecni_line += f" Nieobecnych: {len(summary['absent'])}."
                body_parts.append(f"<p>{obecni_line}</p>")
            else:
                body_parts.append(f"<p>Data: {esc(sdate)}</p>")
                body_parts.append(
                    f"<p>Glosowan: {vote_cnt} · Obecnych: {attendee_cnt}</p>"
                )

            # Najbardziej sporne głosowania (top 5 wg udziału mniejszości).
            if summary["contested"]:
                _items = []
                for _v in summary["contested"][:5]:
                    _vid = _v.get("id", "")
                    _vt = (_v.get("topic") or "").strip() or f"Glosowanie {_vid}"
                    _c = _v.get("counts", {}) or {}
                    _items.append(
                        f"<li><a href=\"{site_url}/{SLUG['vote']}/{_vid}/\">{esc(_vt[:140])}</a>"
                        f" (za {_c.get('za', 0)}, przeciw {_c.get('przeciw', 0)},"
                        f" wstrzymało się {_c.get('wstrzymal_sie', 0)})</li>"
                    )
                body_parts.append(
                    "<h2>Najbardziej sporne głosowania</h2>\n<ol>\n"
                    + "\n".join(_items) + "\n</ol>"
                )

            # Nieobecni z linkami do profili (tylko rozpoznane nazwiska).
            if summary["absent"]:
                _abs_parts = []
                for _n in summary["absent"]:
                    _ps = profile_slug_by_name.get(_n)
                    if _ps:
                        _abs_parts.append(
                            f"<a href=\"{site_url}/{SLUG['profile']}/{_ps}/\">{esc(_n)}</a>"
                        )
                    else:
                        _abs_parts.append(esc(_n))
                body_parts.append(
                    f"<h2>Nieobecni radni ({len(summary['absent'])})</h2>\n"
                    "<p>" + ", ".join(_abs_parts) + "</p>"
                )

            # Pełna lista głosowań — unikalna treść strony sesji (wcześniej
            # body to były 3 linijki i Google klastrował strony sesji jako
            # duplikaty wybierając własny canonical).
            if sess_votes:
                _items = []
                for _v in sess_votes:
                    _vid = _v.get("id", "")
                    _vt = (_v.get("topic") or "").strip() or f"Glosowanie {_vid}"
                    _c = _v.get("counts", {}) or {}
                    _items.append(
                        f"<li><a href=\"{site_url}/{SLUG['vote']}/{_vid}/\">{esc(_vt[:140])}</a>"
                        f" (za {_c.get('za', 0)}, przeciw {_c.get('przeciw', 0)},"
                        f" wstrzymalo sie {_c.get('wstrzymal_sie', 0)})</li>"
                    )
                body_parts.append(
                    "<h2>Glosowania na tej sesji</h2>\n<ol>\n"
                    + "\n".join(_items) + "\n</ol>"
                )

            # Nawigacja poprzednia/następna sesja + powrót.
            nav_parts = []
            if si > 0:
                _p = ordered_sessions[si - 1]
                nav_parts.append(
                    f"<a href=\"{site_url}/{SLUG['session']}/{_p['number']}/\">"
                    f"Poprzednia sesja ({esc(str(_p['number']))}, {esc(_p.get('date', ''))})</a>"
                )
            if si + 1 < len(ordered_sessions):
                _n = ordered_sessions[si + 1]
                nav_parts.append(
                    f"<a href=\"{site_url}/{SLUG['session']}/{_n['number']}/\">"
                    f"Następna sesja ({esc(str(_n['number']))}, {esc(_n.get('date', ''))})</a>"
                )
            nav_parts.append(f"<a href=\"{site_url}/\">Radoskop {esc(city_name)}</a>")
            body_parts.append("<p>" + " · ".join(nav_parts) + "</p>")

            body = "\n".join(body_parts) + "\n"

            # Karta OG sesji (generate_og_images.py, render_session_card).
            og_img = f"{site_url}/{SLUG['session']}/{snum}/og.png"
            og_img_path = docs / SLUG["session"] / snum / "og.png"
            if not og_img_path.exists():
                og_img = None

            jsonld = []
            # Article tylko dla sesji z głosowaniami — podsumowanie jest
            # wtedy realnym newsem z datą publikacji.
            if sess_votes and sdate:
                article = {
                    "@context": "https://schema.org",
                    "@type": "Article",
                    "headline": (
                        f"Sesja {snum} Rady Miasta {city_gen}: "
                        f"{vote_cnt} głosowań, {summary['contested_count']} spornych"
                    ),
                    "datePublished": sdate,
                    "dateModified": sdate,
                    "mainEntityOfPage": canonical,
                    "author": {"@type": "Organization", "name": "Radoskop",
                               "url": "https://radoskop.pl"},
                    "publisher": {"@type": "Organization", "name": "Radoskop",
                                  "url": "https://radoskop.pl"},
                }
                if og_img:
                    article["image"] = og_img
                jsonld.append(article)
            jsonld.append(_breadcrumb([
                (f"Radoskop {city_name}", f"{site_url}/"),
                (f"Sesja {snum}", canonical),
            ]))

            page = make_page(main_html, canonical, title, desc, og_image=og_img,
                             extra_body=body, jsonld=jsonld)
            _maybe_write_page(out / SLUG["session"] / snum / "index.html", page)
            session_count += 1

            # Świeże sesje (90 dni) dostają wyższy priorytet i changefreq —
            # to one są newsem; archiwalne zostają na 0.5/monthly. lastmod
            # tylko dla poprawnych dat ISO.
            recent = False
            try:
                recent = (today - _date.fromisoformat(sdate)).days <= 90
            except (ValueError, TypeError):
                pass
            entry = {
                "loc": canonical,
                "changefreq": "weekly" if recent else "monthly",
                "priority": "0.8" if recent else "0.5",
            }
            if re.match(r"^\d{4}-\d{2}-\d{2}$", sdate or ""):
                entry["lastmod"] = sdate
            sitemap_entries.append(entry)

    print(f"  {session_count} session pages")

    # ════════════════════════════════════════════
    # 4. Kadencja tab pages
    # ════════════════════════════════════════════
    # Tab name labels (display) per locale, slugs (URL) per locale.
    # Display names zostaj\u0105 po polsku w body content, bo PRIVACY/TERMS
    # ju\u017c s\u0105 po angielsku dla non-PL i Google indeksuje po tre\u015bci, nie
    # po nazwach tab\u00f3w.
    TAB_NAMES = {
        SLUG["tab_profiles"]: "Profile radnych",
        SLUG["tab_sessions"]: "Sesje",
        SLUG["tab_votes"]: "Glosowania",
        SLUG["tab_similarity"]: "Podobienstwo glosowan",
        SLUG["tab_interpelacje"]: "Interpelacje",
    }
    # ranking ma zawsze ten sam slug we wszystkich locale
    TAB_NAMES["ranking"] = "Ranking radnych"

    # Unikalne body per tab. Wcze\u015bniej wszystkie taby kadencji dzieli\u0142y
    # identyczny main_html r\u00f3\u017cni\u0105c si\u0119 tylko title/desc/canonical \u2014 Google
    # klastrowa\u0142 je jako duplikaty i wybiera\u0142 w\u0142asny canonical (GSC
    # "Duplicate, Google chose different canonical than user").
    def _tab_body(tab_slug, kid, kad_data):
        heading = TAB_NAMES.get(tab_slug, tab_slug)
        head_html = f"<h1>{esc(heading)}, kadencja {esc(kid)}</h1>\n"
        # Interpelacje korzystają z danych miejskich (interp_items), nie z
        # kadencja-*.json — obsługujemy je PRZED strażnikiem `if not kad_data`,
        # żeby zakładka miała treść nawet gdy plik kadencji jest niedostępny.
        if tab_slug == SLUG["tab_interpelacje"]:
            if not interp_items:
                return head_html
            def _idate(it):
                return it.get("data_wplywu") or it.get("data") or ""
            recent = sorted(interp_items, key=_idate, reverse=True)[:50]
            lead = (
                f"<p>Interpelacje i zapytania radnych {esc(city_gen)}: "
                f"{len(interp_items)} pism, najnowsze poniżej. "
                f"Każde prowadzi do profilu autora z pełnym zapisem aktywności.</p>\n"
            )
            items = []
            for it in recent:
                topic = (it.get("przedmiot") or it.get("temat") or it.get("topic") or "").strip()
                topic = " ".join(topic.split())[:140] or "Interpelacja"
                author = (it.get("radny") or it.get("autor") or "").strip()
                date = _idate(it)
                typ = (it.get("typ") or "").lower()
                kind = "Zapytanie" if typ.startswith("z") else "Interpelacja"
                aslug = _profile_slug_by_name.get(author, "")
                if author and aslug:
                    author_html = (
                        f"<a href=\"{site_url}/{SLUG['profile']}/{aslug}/\">{esc(author)}</a>"
                    )
                else:
                    author_html = esc(author) if author else ""
                meta = " · ".join(x for x in (author_html, esc(date)) if x)
                items.append(
                    f"<li>{kind}: {esc(topic)}" + (f" — {meta}" if meta else "") + "</li>"
                )
            return head_html + lead + "<ul>\n" + "\n".join(items) + "\n</ul>\n"
        if not kad_data:
            return head_html
        councilors = kad_data.get("councilors") or []
        sessions = kad_data.get("sessions") or []
        votes = kad_data.get("votes") or []

        def _cl(c):
            club = (c.get("club") or "").strip()
            return f" ({esc(club)})" if club and club != "?" else ""

        def _cname(c):
            # Nazwisko jako link do profilu (gdy znamy slug) — rozprowadza
            # link equity z list kadencji na strony profili i wiąże URL profilu
            # z frazą-nazwiskiem (główny typ zapytań w Search Console). Slug z
            # danych councilora albo z mapy nazwisko→slug (profiles.json).
            name = c.get("name", "")
            cslug = c.get("slug") or _profile_slug_by_name.get((name or "").strip(), "")
            if name and cslug:
                return f"<a href=\"{site_url}/{SLUG['profile']}/{cslug}/\">{esc(name)}</a>"
            return esc(name)

        if tab_slug == "ranking":
            ranked = sorted(
                (c for c in councilors if isinstance(c, dict)),
                key=lambda c: (c.get("aktywnosc") or 0), reverse=True,
            )
            items = [
                f"<li>{_cname(c)}{_cl(c)}: aktywnosc "
                f"{(c.get('aktywnosc') or 0):.0f}%, frekwencja "
                f"{(c.get('frekwencja') or 0):.0f}%</li>"
                for c in ranked
            ]
            return head_html + "<ol>\n" + "\n".join(items) + "\n</ol>\n"
        if tab_slug == SLUG["tab_profiles"]:
            items = [
                f"<li>{_cname(c)}{_cl(c)}</li>"
                for c in sorted(
                    (c for c in councilors if isinstance(c, dict)),
                    key=lambda c: c.get("name", ""),
                )
            ]
            return head_html + "<ul>\n" + "\n".join(items) + "\n</ul>\n"
        if tab_slug == SLUG["tab_sessions"]:
            items = [
                f"<li>Sesja {esc(str(s.get('number', '')))}, {esc(s.get('date', ''))}"
                f" ({s.get('vote_count', 0)} glosowan)</li>"
                for s in sessions
            ]
            return head_html + "<ul>\n" + "\n".join(items) + "\n</ul>\n"
        if tab_slug == SLUG["tab_votes"]:
            recent = sorted(
                votes, key=lambda v: v.get("session_date", ""), reverse=True,
            )[:40]
            items = []
            for v in recent:
                c = v.get("counts", {}) or {}
                topic = (v.get("topic") or "").strip() or f"Glosowanie {v.get('id', '')}"
                items.append(
                    f"<li><a href=\"{site_url}/{SLUG['vote']}/{v.get('id', '')}/\">"
                    f"{esc(topic[:120])}</a> ({esc(v.get('session_date', ''))}:"
                    f" za {c.get('za', 0)}, przeciw {c.get('przeciw', 0)})</li>"
                )
            return head_html + "<ul>\n" + "\n".join(items) + "\n</ul>\n"
        if tab_slug == SLUG["tab_similarity"]:
            parts = []
            for key, label in (
                ("similarity_top", "Najbardziej zgodne pary radnych"),
                ("similarity_bottom", "Najmniej zgodne pary radnych"),
            ):
                pairs = kad_data.get(key) or []
                if pairs:
                    items = [
                        f"<li>{esc(p.get('a', ''))} i {esc(p.get('b', ''))}:"
                        f" {(p.get('score') or 0):.0f}% zgodnosci"
                        f" ({p.get('common_votes', 0)} wspolnych glosowan)</li>"
                        for p in pairs[:15] if isinstance(p, dict)
                    ]
                    parts.append(f"<h2>{label}</h2>\n<ul>\n" + "\n".join(items) + "\n</ul>\n")
            return head_html + "".join(parts)
        return head_html

    kad_count = 0
    for kid, kslug in KAD_SLUGS.items():
        kad_file = docs / f"kadencja-{kid}.json"
        kad_data = None
        if kad_file.exists():
            with open(kad_file, "r", encoding="utf-8") as f:
                kad_data = json.load(f)

        canonical = f"{site_url}/{SLUG['term']}/{kslug}/"
        title = f"Kadencja {kid} \u2013 Radoskop {city_name}"
        desc = f"Monitoring Rady Miasta {city_gen}, kadencja {kid}. Ranking, sesje, glosowania i aktywnosc radnych."

        term_body = f"<h1>Kadencja {esc(kid)}</h1>\n"
        if kad_data:
            term_body += (
                f"<p>Radnych: {kad_data.get('total_councilors', 0)} \u00b7"
                f" Sesji: {kad_data.get('total_sessions', 0)} \u00b7"
                f" Glosowan: {kad_data.get('total_votes', 0)}</p>\n"
            )
            clubs = kad_data.get("clubs") or {}
            # clubs to dict {nazwa: liczba_radnych}; lista jako fallback.
            if isinstance(clubs, dict):
                club_parts = [
                    f"{esc(n)} ({cnt})" for n, cnt in sorted(
                        clubs.items(), key=lambda kv: -kv[1]
                    ) if n and n != "?"
                ]
            else:
                club_parts = [
                    esc(c.get("name", "") if isinstance(c, dict) else str(c))
                    for c in clubs
                ]
            club_parts = [p for p in club_parts if p]
            if club_parts:
                term_body += "<p>Kluby: " + ", ".join(club_parts) + "</p>\n"

        term_crumbs = _breadcrumb([
            (f"Radoskop {city_name}", f"{site_url}/"),
            (f"Kadencje Rady Miasta {city_gen}", f"{site_url}/{SLUG['term']}/"),
            (f"Kadencja {kid}", canonical),
        ])

        # FAQ tylko dla polskich lokalizacji — odpowiedzi to pełna polska proza
        # bez kluczy i18n, więc na miastach non-PL zostałyby po polsku. Strona
        # bazowa kadencji to hub o niskiej kardynalności (jedna per miasto per
        # kadencja), więc stały explainer Q&A jest tu na temat i nie dubluje
        # się masowo. Treść jest też widoczna w body (wymóg Google).
        term_jsonld = [term_crumbs]
        if locale == "pl":
            term_faq = [
                (
                    f"Czym jest kadencja {kid} Rady Miasta {city_gen}?",
                    f"To okres pracy radnych wybranych w wyborach samorządowych. "
                    f"Radoskop zbiera w jednym miejscu sesje, głosowania i aktywność "
                    f"radnych {city_gen} w tej kadencji.",
                ),
                (
                    "Co oznacza frekwencja radnego?",
                    "Frekwencja to udział radnego w głosowaniach imiennych w stosunku "
                    "do wszystkich zarejestrowanych głosowań w kadencji.",
                ),
                (
                    "Co to jest zgodność głosowań z klubem?",
                    "To odsetek głosowań, w których radny głosował tak samo jak "
                    "większość jego klubu. Niska wartość oznacza częste głosowanie "
                    "wbrew klubowi.",
                ),
                (
                    "Skąd pochodzą dane na Radoskopie?",
                    "Dane pochodzą z oficjalnych źródeł publicznych: Biuletynów "
                    "Informacji Publicznej, protokołów sesji i imiennych wyników "
                    "głosowań udostępnianych przez urzędy.",
                ),
            ]
            term_body += _faq_html(term_faq)
            term_jsonld.append(_faqpage(term_faq))

        page = make_page(main_html, canonical, title, desc, extra_body=term_body, jsonld=term_jsonld)
        _maybe_write_page(out / SLUG["term"] / kslug / "index.html", page)

        sitemap_entries.append({"loc": canonical, "changefreq": "weekly", "priority": "0.8"})

        for tab_slug, tab_name in TAB_NAMES.items():
            tab_canonical = f"{site_url}/{SLUG['term']}/{kslug}/{tab_slug}/"
            tab_title = f"{tab_name}, kadencja {kid} \u2013 Radoskop {city_name}"
            tab_desc = f"{tab_name} Rady Miasta {city_gen}, kadencja {kid}."
            # Interpelacje: tytu\u0142 prowadzony s\u0142owem kluczowym pod realne
            # zapytania ("interpelacje radnych {miasto}"), opis z liczb\u0105 pism.
            if tab_slug == SLUG["tab_interpelacje"]:
                tab_title = f"Interpelacje radnych {city_gen} \u2013 Radoskop {city_name}"
                if interp_items:
                    tab_desc = (
                        f"Interpelacje i zapytania radnych {city_gen}: "
                        f"{len(interp_items)} pism z wyszukiwark\u0105 po autorze i temacie. "
                        f"Pe\u0142ny zapis aktywno\u015bci Rady Miasta {city_gen}."
                    )
                else:
                    tab_desc = (
                        f"Interpelacje i zapytania radnych {city_gen}. "
                        f"Pe\u0142ny zapis aktywno\u015bci Rady Miasta {city_gen} w serwisie Radoskop."
                    )

            tab_crumbs = _breadcrumb([
                (f"Radoskop {city_name}", f"{site_url}/"),
                (f"Kadencje Rady Miasta {city_gen}", f"{site_url}/{SLUG['term']}/"),
                (f"Kadencja {kid}", canonical),
                (tab_name, tab_canonical),
            ])
            tab_page = make_page(
                main_html, tab_canonical, tab_title, tab_desc,
                extra_body=_tab_body(tab_slug, kid, kad_data),
                jsonld=tab_crumbs,
            )
            _maybe_write_page(out / SLUG["term"] / kslug / tab_slug / "index.html", tab_page)
            kad_count += 1

            sitemap_entries.append({"loc": tab_canonical, "changefreq": "weekly", "priority": "0.6"})

    print(f"  {kad_count} kadencja tab pages")

    # ════════════════════════════════════════════
    # 5. Budget page
    # ════════════════════════════════════════════
    if config.get("has_budget"):
        canonical = f"{site_url}/{SLUG['budget']}/"
        title = f"Budzet {city_gen} \u2013 Radoskop {city_name}"
        desc = f"Analiza budzetu miasta {city_gen}. Wydatki, dochody i inwestycje miejskie."

        budget_crumbs = _breadcrumb([
            (f"Radoskop {city_name}", f"{site_url}/"),
            (f"Budzet {city_gen}", canonical),
        ])
        page = make_page(main_html, canonical, title, desc, jsonld=budget_crumbs)
        _maybe_write_page(out / SLUG["budget"] / "index.html", page)
        sitemap_entries.append({"loc": canonical, "changefreq": "monthly", "priority": "0.8"})
        print(f"  1 budget page")

    # ════════════════════════════════════════════
    # 6. Catch-all directory pages
    # ════════════════════════════════════════════
    for dirname, title_part, desc_part, prio in [
        (SLUG["profile"], f"Radni {city_gen}", f"Profile radnych {city_gen}. Frekwencja, glosowania i aktywnosc.", "0.9"),
        (SLUG["term"], f"Kadencje Rady Miasta {city_gen}", f"Kadencje Rady Miasta {city_gen}. Ranking, sesje i glosowania.", "0.9"),
    ]:
        d = out / dirname
        if (out / dirname).is_dir() or (docs / dirname).is_dir() or profiles:
            canonical = f"{site_url}/{dirname}/"
            title = f"{title_part} \u2013 Radoskop {city_name}"
            dir_crumbs = _breadcrumb([
                (f"Radoskop {city_name}", f"{site_url}/"),
                (title_part, canonical),
            ])
            page = make_page(main_html, canonical, title, desc_part, jsonld=dir_crumbs)
            _maybe_write_page(d / "index.html", page)
            sitemap_entries.append({"loc": canonical, "changefreq": "monthly", "priority": prio})

    # ════════════════════════════════════════════
    # 6b. Privacy policy page
    # ════════════════════════════════════════════
    # Privacy/terms slugi: po migracji 2026-05 zawsze angielskie. Treść
    # strony zostaje zlokalizowana (Polish vs English legal text), ale
    # URL slug jest zawsze /privacy/ i /terms/.
    privacy_slug = "privacy"
    terms_slug = "terms"

    privacy_canonical = f"{site_url}/{privacy_slug}/"
    privacy_title = f"Polityka prywatności \u2013 Radoskop {city_name}"
    privacy_desc = f"Polityka prywatności i informacje o plikach cookies serwisu Radoskop {city_name}."
    privacy_page = make_page(main_html, privacy_canonical, privacy_title, privacy_desc)
    _maybe_write_page(out / privacy_slug / "index.html", privacy_page)
    sitemap_entries.append({"loc": privacy_canonical, "changefreq": "yearly", "priority": "0.3"})

    terms_canonical = f"{site_url}/{terms_slug}/"
    terms_title = f"Regulamin \u2013 Radoskop {city_name}"
    terms_desc = f"Regulamin serwisu Radoskop {city_name}. Źródła danych, metodologia i zasady korzystania."
    terms_page = make_page(main_html, terms_canonical, terms_title, terms_desc)
    _maybe_write_page(out / terms_slug / "index.html", terms_page)
    sitemap_entries.append({"loc": terms_canonical, "changefreq": "yearly", "priority": "0.3"})

    # ════════════════════════════════════════════
    # 6c. Reports page
    # ════════════════════════════════════════════
    reports_canonical = f"{site_url}/{SLUG['reports']}/"
    reports_title = f"Raporty PDF \u2013 Radoskop {city_name}"
    reports_desc = f"Szczeg\u00f3\u0142owe raporty PDF z analiz\u0105 pracy radnych, klub\u00f3w i rady miasta {city_gen}. Frekwencja, g\u0142osowania, rebelie."
    reports_crumbs = _breadcrumb([
        (f"Radoskop {city_name}", f"{site_url}/"),
        ("Raporty PDF", reports_canonical),
    ])
    reports_page = make_page(main_html, reports_canonical, reports_title, reports_desc, jsonld=reports_crumbs)
    _maybe_write_page(out / SLUG["reports"] / "index.html", reports_page)
    sitemap_entries.append({"loc": reports_canonical, "changefreq": "weekly", "priority": "0.6"})

    # NB: /premium/ NIE wchodzi do per-city sitemap - oferta Premium jest
    # jedna per kraj (B2B), kanonicznie na https://radoskop.pl/premium/.
    # Per-city /premium/ robi 301 redirect na apex (router w SPA).
    # Sitemap apex (build_main_sitemap.py) ma /premium/.

    # ════════════════════════════════════════════
    # 7. Fix main index.html canonical
    # ════════════════════════════════════════════
    main_canonical = f"{site_url}/"
    main_check = re.search(r'<link rel="canonical" href="([^"]*)">', main_html)
    if main_check and main_check.group(1) != main_canonical:
        main_html = re.sub(
            r'<link rel="canonical" href="[^"]*">',
            f'<link rel="canonical" href="{main_canonical}">',
            main_html
        )
        with open(main_html_path, "w", encoding="utf-8") as f:
            f.write(main_html)
        print(f"  Fixed main canonical")

    # ════════════════════════════════════════════
    # 8. Generate sitemap.xml
    # ════════════════════════════════════════════
    sitemap_lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
        f'  <url>\n    <loc>{site_url}/</loc>\n    <changefreq>weekly</changefreq>\n    <priority>1.0</priority>\n  </url>',
    ]
    for entry in sitemap_entries:
        # lastmod opcjonalny (na razie tylko strony sesji — data sesji).
        lastmod = (
            f'    <lastmod>{entry["lastmod"]}</lastmod>\n'
            if entry.get("lastmod") else ""
        )
        sitemap_lines.append(
            f'  <url>\n    <loc>{entry["loc"]}</loc>\n'
            + lastmod
            + f'    <changefreq>{entry["changefreq"]}</changefreq>\n'
            f'    <priority>{entry["priority"]}</priority>\n  </url>'
        )
    sitemap_lines.append('</urlset>')

    out.mkdir(parents=True, exist_ok=True)
    with open(out / "sitemap.xml", "w", encoding="utf-8") as f:
        f.write("\n".join(sitemap_lines) + "\n")

    total_urls = len(sitemap_entries) + 1
    print(f"  sitemap.xml: {total_urls} URLs")

    # ════════════════════════════════════════════
    # 9. Mapa redirectów ID głosowań (_redirects/votes.json)
    # ════════════════════════════════════════════
    # Daty z kolizją rzymskich usuwamy z mapy (niejednoznaczne).
    vote_redirect_map = {
        d: r for d, r in sorted(vote_id_romans.items())
        if d not in vote_id_roman_conflict
    }
    redirects_dir = out / "_redirects"
    redirects_dir.mkdir(parents=True, exist_ok=True)
    with open(redirects_dir / "votes.json", "w", encoding="utf-8") as f:
        json.dump(vote_redirect_map, f, ensure_ascii=False, separators=(",", ":"))
    print(
        f"  _redirects/votes.json: {len(vote_redirect_map)} dat"
        + (f" ({len(vote_id_roman_conflict)} pominiętych przez kolizję)"
           if vote_id_roman_conflict else "")
    )

    # ════════════════════════════════════════════
    # 10. Mapa offsetów globalnej numeracji (_redirects/vote_offsets.json)
    # ════════════════════════════════════════════
    # {data: [global_start, count]}. Worker przekierowuje stare URL-e
    # DATA_GLOBAL_MMM (global w zakresie [start, start+count-1]) na
    # DATA_{global-start+1:03d}_MMM. Emitujemy tylko daty jednoznaczne
    # (start > count), gdzie zakres globalny nie nachodzi na per-sesyjny —
    # inaczej worker mógłby przekierować prawidłowy bieżący URL. Pierwsza
    # sesja kadencji (start == 1) to mapowanie tożsamościowe, też odpada.
    vote_offsets: dict[str, list[int]] = {}
    for _kid_counts in vote_seq_counts.values():
        _cum = 0
        for _d in sorted(_kid_counts):
            _n = _kid_counts[_d]
            _start = _cum + 1
            _cum += _n
            if _start > _n:
                vote_offsets[_d] = [_start, _n]
    with open(redirects_dir / "vote_offsets.json", "w", encoding="utf-8") as f:
        json.dump(vote_offsets, f, ensure_ascii=False, separators=(",", ":"))
    print(f"  _redirects/vote_offsets.json: {len(vote_offsets)} dat")

    # ════════════════════════════════════════════
    # 11. Mapa starych slugów profili (_redirects/profiles.json)
    # ════════════════════════════════════════════
    # {stary_slug: kanoniczny_slug} dla nazwisk, gdzie którykolwiek z
    # historycznych slugify dawał inny wynik niż obecny lib_slug.make_slug:
    # wariant NFKD gubił ł (Warszawa, sejmiki, berlińskie ß), wariant
    # tabelowy nie kolabował separatorów (Kielce: "Mazur- Kałuża" →
    # podwójny dywiz). Worker robi 301 na /profile/{stary}/ gdy S3 nie ma
    # strony.
    profile_redirects = {}
    for p in profiles:
        name = p.get("name", "")
        slug = p.get("slug", "")
        if not name or not slug:
            continue
        for legacy in (
            legacy_nfkd_slug(name),
            legacy_table_slug(name),
            legacy_surname_first_slug(name),
        ):
            if legacy and legacy != slug:
                profile_redirects[legacy] = slug
    with open(redirects_dir / "profiles.json", "w", encoding="utf-8") as f:
        json.dump(profile_redirects, f, ensure_ascii=False, separators=(",", ":"))
    if profile_redirects:
        print(f"  _redirects/profiles.json: {len(profile_redirects)} slugów")


def main():
    parser = argparse.ArgumentParser(description="Generate SEO pages for Radoskop")
    parser.add_argument("--base", required=True, help="Base directory containing radoskop-* city dirs")
    parser.add_argument("--city", default=None, help="Process only this city (e.g. radoskop-gdansk)")
    parser.add_argument(
        "--output-base", default=None,
        help="Optional separate output base directory. Generated SEO pages "
             "will go to {output-base}/{slug}/ instead of {base}/{slug}/docs/. "
             "Use this to keep monorepo working tree free of generated files; "
             "deploy the output dir to S3 separately.",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Generuj strony nawet dla miast z disabled:true w config.json. "
             "Używać przy ręcznym testowaniu nowego miasta przed jego odblokow.",
    )
    parser.add_argument(
        "--sitemap-only", action="store_true",
        help="Pomija pisanie tysięcy HTML SEO pages — generuje tylko sitemap.xml. "
             "Używać po migracji na dynamiczny rendering SEO w Cloudflare Worker "
             "(radoskop-premium/cloudflare/worker.js). Worker robi per-route meta "
             "tagi z S3 template + JSON live, statyczny pre-render zbędny.",
    )
    args = parser.parse_args()

    global SITEMAP_ONLY
    SITEMAP_ONLY = args.sitemap_only

    base = Path(args.base)
    output_base = Path(args.output_base) if args.output_base else None

    if args.city:
        slug = args.city[len("radoskop-"):] if args.city.startswith("radoskop-") else args.city
        cities = [slug]
    else:
        cities = sorted([
            d.name for d in base.iterdir()
            if d.is_dir()
            and (d / "config.json").exists()
            and d.name not in {"radoskop", "_main"}
        ])
        cities = [c[len("radoskop-"):] if c.startswith("radoskop-") else c for c in cities]

    for city in cities:
        city_dir = base / city
        if not city_dir.exists():
            city_dir = base / f"radoskop-{city}"
        if city_dir.exists():
            print(f"\n=== {city} ===")
            slug = city_dir.name.removeprefix("radoskop-")
            output_dir = (output_base / slug) if output_base else None
            process_city(city_dir, output_dir=output_dir, force=args.force)
        else:
            print(f"  Skipping {city}: not found")


if __name__ == "__main__":
    main()
