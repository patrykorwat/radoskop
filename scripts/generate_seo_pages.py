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
from lib_slug import legacy_nfkd_slug, legacy_table_slug


def esc(text):
    """HTML-escape text for safe embedding."""
    return html.escape(str(text), quote=True)


def make_page(main_html, canonical_url, title, description, og_image=None, extra_body=""):
    """Create a page variant with unique SEO tags and optional body content."""
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

    # Statyczna lista radnych (/councillors/) gdy miasto ma landing.
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
        title = f"{name}, {club} \u2013 Radoskop {city_name}"
        desc = (
            f"{name}, klub {club_full}. "
            f"Frekwencja {frekwencja:.0f}%, aktywnosc {aktywnosc:.0f}%, "
            f"zgodnosc z klubem {zgodnosc:.0f}%. "
            f"Rada Miasta {city_gen}."
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

        body = (
            f"<h1>{esc(name)}</h1>\n"
            f"<p>Klub: {esc(club_full)}</p>\n"
            f"<p>Frekwencja: {frekwencja:.0f}% · "
            f"Aktywnosc: {aktywnosc:.0f}% · "
            f"Zgodnosc z klubem: {zgodnosc:.0f}%</p>\n"
            f"<p>Za: {votes_za} · Przeciw: {votes_przeciw} · Wstrzymal sie: {votes_wstrzymal}</p>\n"
            f"<p><a href=\"{site_url}/\">Radoskop {esc(city_name)}</a></p>\n"
        )

        page = make_page(main_html, canonical, title, desc, og_image=og_img, extra_body=body)
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
        _cnames: list[str] = []
        for _c in kad_data.get("councilors") or []:
            if isinstance(_c, dict):
                _nm = _c.get("name", "") or ""
                _cl = (_c.get("club") or "").strip()
                _cnames.append(f"{_nm} ({_cl})" if _cl and _cl != "?" else _nm)
            else:
                _cnames.append(str(_c))

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
                f"Wynik: za {za}, przeciw {przeciw}, wstrzymal sie {wstrzymal}. "
                f"{sess_label}. Imienne glosy radnych."
            )

            og_img = f"{site_url}/{SLUG['vote']}/{vid}/og.png"
            og_img_path = docs / SLUG["vote"] / vid / "og.png"
            if not og_img_path.exists():
                og_img_legacy = docs / "glosowanie" / vid / "og.png"
                if og_img_legacy.exists():
                    og_img_path = og_img_legacy
                else:
                    og_img = None

            # Imienna rozpiska głosów — unikalny, merytoryczny content per
            # strona (anty-duplikat + realna wartość: kto jak głosował).
            nv = vote.get("named_votes") or {}
            nv_html = ""
            if isinstance(nv, dict):
                for _nkey, _nlabel in (
                    ("za", "Za"),
                    ("przeciw", "Przeciw"),
                    ("wstrzymal_sie", "Wstrzymali sie"),
                    ("brak_glosu", "Brak glosu"),
                    ("nieobecni", "Nieobecni"),
                ):
                    _names = _nv_names(nv.get(_nkey))
                    if _names:
                        nv_html += (
                            f"<h3>{_nlabel} ({len(_names)})</h3>\n"
                            "<p>" + ", ".join(esc(n) for n in _names) + "</p>\n"
                        )
            if nv_html:
                nv_html = "<h2>Jak glosowali radni</h2>\n" + nv_html

            ref_html = ""
            if vote.get("resolution"):
                ref_html += f"<p>Uchwala: {esc(str(vote['resolution']))}</p>\n"
            if vote.get("druk"):
                ref_html += f"<p>Druk: {esc(str(vote['druk']))}</p>\n"

            body = (
                f"<h1>{esc(topic or f'Glosowanie {vid}')}</h1>\n"
                f"<p>{esc(sess_label)}"
                + (f" · Glosowanie nr {esc(vote_no)}" if vote_no else "")
                + "</p>\n"
                f"<p>Wynik: <strong>{result}</strong></p>\n"
                f"<p>Za: {za} · Przeciw: {przeciw} · Wstrzymal sie: {wstrzymal}</p>\n"
                + ref_html
                + nv_html
                + f"<p><a href=\"{site_url}/\">Radoskop {esc(city_name)}</a></p>\n"
            )

            page = make_page(main_html, canonical, title, desc, og_image=og_img, extra_body=body)
            _maybe_write_page(out / SLUG["vote"] / vid / "index.html", page)
            vote_count += 1

            sitemap_entries.append({"loc": canonical, "changefreq": "monthly", "priority": "0.5"})

    print(f"  {vote_count} vote pages")

    # ════════════════════════════════════════════
    # 3. Session pages
    # ════════════════════════════════════════════
    session_count = 0
    for k in kadencje:
        kid = k.get("id", "")
        kad_file = docs / f"kadencja-{kid}.json"
        if not kad_file.exists():
            continue

        with open(kad_file, "r", encoding="utf-8") as f:
            kad_data = json.load(f)

        # Lista głosowań per sesja: unikalna treść strony sesji (wcześniej
        # body to były 3 linijki i Google klastrował strony sesji jako
        # duplikaty wybierając własny canonical).
        _votes_by_date: dict[str, list] = {}
        for _v in kad_data.get("votes", []) or []:
            _votes_by_date.setdefault(_v.get("session_date", ""), []).append(_v)

        for s in kad_data.get("sessions", []):
            snum = s.get("number", "")
            if not snum:
                continue
            # Guard: number powinien być krótkim identyfikatorem (rzymski
            # numer "XXIII", numer arabski "23", albo data ISO "2024-05-07").
            # Jeśli zawiera spacje, słowo "Sesja", "Rada", "Miast" albo jest
            # dłuższy niż 30 znaków, scrape miał problem z ekstrakcją —
            # nie generujemy SEO page, bo brzydki URL utknie w Google index.
            snum_str = str(snum).strip()
            if len(snum_str) > 30 or " " in snum_str:
                print(f"  skipping invalid session number: {snum_str!r}")
                continue
            lower = snum_str.lower()
            if any(bad in lower for bad in ("sesja", "rada", "miast", "rady")):
                print(f"  skipping suspicious session number: {snum_str!r}")
                continue

            sdate = s.get("date", "")
            vote_cnt = s.get("vote_count", 0)
            attendee_cnt = s.get("attendee_count", 0)

            canonical = f"{site_url}/{SLUG['session']}/{snum}/"
            title = f"Sesja {snum} ({sdate}) \u2013 Radoskop {city_name}"
            desc = (
                f"Sesja {snum} Rady Miasta {city_gen}, {sdate}. "
                f"{vote_cnt} glosowan, {attendee_cnt} obecnych radnych."
            )

            votes_html = ""
            _sess_votes = _votes_by_date.get(sdate, [])
            if _sess_votes:
                _items = []
                for _v in _sess_votes:
                    _vid = _v.get("id", "")
                    _vt = (_v.get("topic") or "").strip() or f"Glosowanie {_vid}"
                    _c = _v.get("counts", {}) or {}
                    _items.append(
                        f"<li><a href=\"{site_url}/{SLUG['vote']}/{_vid}/\">{esc(_vt[:140])}</a>"
                        f" (za {_c.get('za', 0)}, przeciw {_c.get('przeciw', 0)},"
                        f" wstrzymalo sie {_c.get('wstrzymal_sie', 0)})</li>"
                    )
                votes_html = (
                    "<h2>Glosowania na tej sesji</h2>\n<ol>\n"
                    + "\n".join(_items) + "\n</ol>\n"
                )

            body = (
                f"<h1>Sesja {esc(snum)}</h1>\n"
                f"<p>Data: {esc(sdate)}</p>\n"
                f"<p>Glosowan: {vote_cnt} · Obecnych: {attendee_cnt}</p>\n"
                + votes_html
                + f"<p><a href=\"{site_url}/\">Radoskop {esc(city_name)}</a></p>\n"
            )

            page = make_page(main_html, canonical, title, desc, extra_body=body)
            _maybe_write_page(out / SLUG["session"] / snum / "index.html", page)
            session_count += 1

            sitemap_entries.append({"loc": canonical, "changefreq": "monthly", "priority": "0.5"})

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
        if not kad_data:
            return head_html
        councilors = kad_data.get("councilors") or []
        sessions = kad_data.get("sessions") or []
        votes = kad_data.get("votes") or []

        def _cl(c):
            club = (c.get("club") or "").strip()
            return f" ({esc(club)})" if club and club != "?" else ""

        if tab_slug == "ranking":
            ranked = sorted(
                (c for c in councilors if isinstance(c, dict)),
                key=lambda c: (c.get("aktywnosc") or 0), reverse=True,
            )
            items = [
                f"<li>{esc(c.get('name', ''))}{_cl(c)}: aktywnosc "
                f"{(c.get('aktywnosc') or 0):.0f}%, frekwencja "
                f"{(c.get('frekwencja') or 0):.0f}%</li>"
                for c in ranked
            ]
            return head_html + "<ol>\n" + "\n".join(items) + "\n</ol>\n"
        if tab_slug == SLUG["tab_profiles"]:
            items = [
                f"<li>{esc(c.get('name', ''))}{_cl(c)}</li>"
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

        page = make_page(main_html, canonical, title, desc, extra_body=term_body)
        _maybe_write_page(out / SLUG["term"] / kslug / "index.html", page)

        sitemap_entries.append({"loc": canonical, "changefreq": "weekly", "priority": "0.8"})

        for tab_slug, tab_name in TAB_NAMES.items():
            tab_canonical = f"{site_url}/{SLUG['term']}/{kslug}/{tab_slug}/"
            tab_title = f"{tab_name}, kadencja {kid} \u2013 Radoskop {city_name}"
            tab_desc = f"{tab_name} Rady Miasta {city_gen}, kadencja {kid}."

            tab_page = make_page(
                main_html, tab_canonical, tab_title, tab_desc,
                extra_body=_tab_body(tab_slug, kid, kad_data),
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

        page = make_page(main_html, canonical, title, desc)
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
            page = make_page(main_html, canonical, title, desc_part)
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
    reports_page = make_page(main_html, reports_canonical, reports_title, reports_desc)
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
        sitemap_lines.append(
            f'  <url>\n    <loc>{entry["loc"]}</loc>\n'
            f'    <changefreq>{entry["changefreq"]}</changefreq>\n'
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
        for legacy in (legacy_nfkd_slug(name), legacy_table_slug(name)):
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
