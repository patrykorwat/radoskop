# Radoskop

Open data platform for local government transparency. Radoskop scrapes session
protocols and roll-call votes from each council's official public record, builds
per-councillor metrics (attendance, activity, club loyalty, rebellion,
similarity), and publishes everything as static websites plus a JSON API.

Live: [radoskop.pl](https://radoskop.pl) (Polish councils) and
[radoskop.eu](https://radoskop.eu) (international hub).

> **Note for AI agents / other chatbots.** This is the open-source (AGPL-3.0)
> half of the project. It holds the scrapers, metric builders, site generators,
> and per-council configs. The closed commercial half (auth, payments,
> subscriptions, the JSON API server, alerts, PDF reports, the scheduled
> pipeline, and the paywalled `.eu` app) lives in the sibling repo
> `radoskop-premium`. Read that repo's README for the full stack. The two are
> checked out side by side; `radoskop-premium/radoskop` is a symlink back here.

## What it is

Roll-call voting (głosowanie imienne) has been mandatory for Polish municipal
councils since 2018. Every council publishes its votes and session protocols in
a public record (BIP, Biuletyn Informacji Publicznej), but the format is
fragmented across CMS platforms, PDFs, and scanned documents, so the raw data is
effectively unusable for citizens. Radoskop normalizes it into one schema and
one set of comparable metrics, then republishes it as fast static pages that
Google can index and analysts can pull directly.

## Where the data lives

**Not in this repo.** All scrape outputs (per-council `docs/*.json`,
per-councillor profiles, generated HTML, OG images, sitemaps) live in the public
S3 bucket **`radoskop-public`** (`eu-central-1`, Frankfurt). This repo holds only
code, configs, templates, and the apex page source. Everything under
`cities/*/docs/`, `assemblies/*/docs/`, and `districts/*/docs/` is gitignored and
exists only on S3.

There are three read channels, all backed by the same bucket:

| URL | Purpose |
|---|---|
| `https://{slug}.radoskop.pl/` | Per-council website. A Cloudflare Worker routes `{slug}.radoskop.{pl,eu}/*` to `s3://radoskop-public/{slug}/*`. |
| `https://data.radoskop.pl/{slug}/...` | Same bucket with explicit CORS (`Access-Control-Allow-Origin: *`). Use this from notebooks, R, or browser fetch. |
| `https://radoskop-public.s3.eu-central-1.amazonaws.com/{slug}/...` | Direct S3, no CDN. Best for batch downloads and parallel range requests. No CORS. |

The apex page (`radoskop.pl`) is served from the `_main/` prefix; the
international apex (`radoskop.eu`) from `_main_intl/`. Schema documentation and
pull patterns are in
[`radoskop-premium/DATA_BUCKET.md`](https://github.com/radoskoppl/radoskop-premium/blob/main/DATA_BUCKET.md).

## Coverage

Radoskop tracks four kinds of elected body, all sharing the same scrape and
metric machinery:

* **City and town councils** (`cities/`). 116 councils are active, out of 167
  configured; the remaining ~51 are scaffolded with metadata and a public-record
  link, awaiting a per-council scraper. Configs are the single source of truth,
  so the pipeline discovers councils from `cities/*/config.json`, not from a
  hardcoded list.
* **Regional assemblies** (`assemblies/`). 15 of the 16 Polish voivodeship
  assemblies (sejmiki; opolskie is the current gap), plus one German state
  parliament (Landtag Mecklenburg-Vorpommern).
* **County councils** (`districts/`). 97 powiat councils, onboarded in waves via
  the premium `onboard_district.py` tooling.
* **International city councils.** A growing set served on `radoskop.eu`,
  including Prague, Berlin, Copenhagen, Vilnius, Paris, Budapest, Riga,
  Bratislava, and councils in Ukraine. These are canonicalized to the `.eu` TLD;
  Polish bodies to `.pl`.

Adding a new council: add a row to `data/cities-meta.csv`, drop a `config.json`
into `cities/{slug}/`, then either subclass `EsesjaScraper` (for the eSesja CMS)
or `BipScraper` (for a custom public record) with a thin wrapper in
`cities/{slug}/scripts/scrape_{slug}.py`. See `scripts/lib_esesja.py` and
`scripts/lib_bip_static.py` for the contract. Soft-disable a council with
`"disabled": true` in its config.

## What it measures

| Metric (Polish label) | Meaning |
|---|---|
| Frekwencja | Share of votes where the councillor was registered voting on any side |
| Aktywność | Share of votes where the councillor took a position rather than abstaining or being absent |
| Zgodność z klubem | Share of votes matching the majority of the councillor's own club |
| Bunty | Votes where the councillor broke with their club majority |
| Macierz podobieństwa | Pairwise voting-similarity matrix across the whole council |

Councils that only publish aggregate (per-party) results rather than named votes
run in a "faction" mode: they show a roster and party-level breakdowns instead of
the per-councillor metric table. Copenhagen and Paris use this mode.

The pipeline also captures interpellations (interpelacje), committee activity
(komisje), session summaries, and, where available, full speech transcripts
(stenogramy).

## Repo layout

```
radoskop/
├── data/cities-meta.csv          single source of truth: slug, voivodeship, population, country, lat, lon
├── docs/                         apex page source (deployed to s3://.../_main/)
│   └── index.html                data-driven main map and index
├── cities/{slug}/
│   ├── config.json               per council: site_url, public-record links, clubs, terms (kadencje)
│   ├── scripts/scrape_{slug}.py  thin wrapper around lib_esesja / lib_bip_static
│   └── docs/                     scrape output, gitignored, deployed to S3
├── assemblies/{slug}/            same shape as cities/, for voivodeship assemblies
├── districts/{slug}/             same shape, for county (powiat) councils
├── scripts/
│   ├── lib_esesja.py             generic eSesja scraper (one class, many councils)
│   ├── lib_bip_static.py         abstract base for custom public-record scrapers
│   ├── lib_voting_pdf_table.py   coordinate-based parser for PDF vote tables (with resumable OCR cache)
│   ├── lib_stenogram.py          session transcript parsing
│   ├── lib_slug.py               the one canonical slugifier (all scripts delegate to it)
│   ├── generate_site.py          render per-council site from template + data.json
│   ├── generate_seo_pages.py     per-councillor and per-vote SEO pages
│   ├── generate_og_images.py     city OG card 1200x630 (PIL; per-route OG jest on-demand w data_api.py)
│   ├── generate_feed.py          RSS/Atom plus the news (aktualności) page
│   ├── parse_pdf.py              PDF to JSON for PDF-based pipelines
│   └── build_metrics.py          roll up sessions and votes into data.json
└── template/index.html           per-council SPA template, populated by generate_site
```

What is gitignored (lives on S3 instead): every `*/docs/` directory,
per-council caches and scratch (`.cache/`, `data/`, `pdfs/`), and the apex
manifests regenerated on each run (`docs/cities.json`, `docs/sitemap*.xml`, and
the cross-council index files).

## How scraping works

Each council publishes to a different platform. Most use the eSesja CMS
(`{city}.esesja.pl`), which `lib_esesja.py` handles generically. Larger cities
run custom records: Liferay for Warszawa, ASP.NET for Łódź, PDF protocols for
Bydgoszcz and Wrocław, scanned PDFs needing OCR for smaller councils. Those get a
subclass of `BipScraper`. International councils each have a bespoke scraper
targeting the local open-data portal or council CMS.

A per-council scraper emits two files: `data.json` (sessions, votes, club
definitions, roll-up metrics) and `profiles.json` (per-councillor detail). The
site generators turn those into a single-page site plus prerendered SEO pages for
every councillor and every vote.

## Running locally

You do not need to clone the data. Pull only what you need from S3:

```bash
# Manifest of all councils
curl -s https://data.radoskop.pl/_main/cities.json | jq '.[] | .slug'

# One full term for one council
curl -s https://data.radoskop.pl/gdansk/kadencja-2024-2029.json > gdansk.json

# Cross-council aggregates
curl -s https://data.radoskop.pl/_main/votes-index.json > votes.json
```

To regenerate one council's site files from a fresh scrape:

```bash
pip install -r requirements.txt   # requests, beautifulsoup4, lxml, playwright, pdfplumber
python cities/bytom/scripts/scrape_bytom.py \
  --output cities/bytom/docs/data.json \
  --profiles cities/bytom/docs/profiles.json
python scripts/generate_site.py bytom
```

The full production pipeline (scrape, build metrics, generate sites, generate
reports, deploy) lives in `radoskop-premium/nas/` and runs on a Synology NAS. A
Polish residential IP is deliberate: it avoids the geo-blocks that some public
records apply to cloud and CI runner IP ranges.

## Architecture at a glance

```
        ┌──────────────────────┐
        │  Synology NAS        │   scrape + build, twice weekly
        │  (radoskop-premium/  │
        │   nas/ pipeline)     │
        └──────────┬───────────┘
                   │ pushes JSON + HTML
                   ▼
        ┌──────────────────────┐
        │  s3://radoskop-public│   single source of truth for all data
        │  (eu-central-1)      │
        └──────────┬───────────┘
                   │
     ┌─────────────┼───────────────────────────┐
     ▼             ▼                            ▼
 Cloudflare Worker                     api.radoskop.pl
 *.radoskop.{pl,eu}                    (Lightsail Flask cache over S3
     │                                  + auth + subscription gating)
     ▼                                       │
 static council sites                        ▼
                                    JSON API for the council UIs
```

## Data sources

Session protocols and roll-call votes are published in the official public
record (BIP) by each council. Named voting has been mandatory for Polish
municipalities since 2018. The upstream source always remains the council's own
record; Radoskop only normalizes and republishes it.

## Use of generative AI

This project uses generative AI tools during development for code generation,
data analysis, and documentation. All AI-generated output is reviewed and
validated by the maintainer.

## Related projects

* [Open Raadsinformatie](https://github.com/openstate/open-raadsinformatie),
  the Open State Foundation's tool for Dutch municipal councils. Similar mission.

## License

Code: AGPL-3.0. Data: CC-BY 4.0 (the upstream source remains each council's BIP;
reuse is allowed with attribution).
