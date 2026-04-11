# Generative AI Prompt Log

This document describes how generative AI was used in the development of Radoskop.

## Models used

- **Claude Sonnet 4** (Anthropic, via Claude Code CLI and Cowork desktop app): primary tool for code generation, refactoring, and data analysis
- **GitHub Copilot** (GitHub, VS Code integration): inline code completion during development

## Scope of AI usage

AI was used as a development accelerator across the following areas:

**Code generation (substantial):** Python scripts for PDF parsing (parse_pdf.py, parse_protokoly.py), metrics computation (build_metrics.py, build_cross_city.py), site generation (generate_site.py, generate_seo_pages.py, generate_sitemap.py), interpellation scrapers (scrape_interpelacje.py per city), RSS feed generation (generate_feed.py), and OG image generation (generate_og_images.py). All generated code was reviewed, tested against real data, and modified by the maintainer before committing.

**Data pipeline:** Scraping scripts for 13 cities were generated with city-specific adaptations (different BIP endpoints, date formats, HTML structures). The scrape_all.py orchestrator was AI-assisted.

**Frontend:** HTML/CSS/JS dashboard templates, Chart.js visualizations, responsive layout, cookie consent implementation. AI generated initial versions, maintainer iterated on styling and UX.

**Documentation:** README, privacy policy (polityka prywatności), terms of service (regulamin). AI drafted initial versions in Polish, maintainer edited for accuracy.

**Grant application:** The NLnet proposal draft (12-wniosek-nlnet-radoskop-draft.md) was developed iteratively with Claude. The maintainer provided project context, strategic direction, budget structure, and all factual claims. Claude helped with English prose, section structure, and ensuring alignment with NLnet's evaluation criteria. All content was reviewed and edited by the maintainer.

## What AI was NOT used for

- Voting data itself (scraped from official BIP sources)
- Political analysis or interpretation of voting patterns
- Decisions about which cities to include or which metrics to compute
- Budget allocation and hourly rate calculations
- Partnership and outreach strategy

## Development timeline

Project started 2026-03-02, developed over ~5 weeks. 87 commits total. AI was used throughout, with the heaviest usage in the initial scaffolding phase (first 2 weeks) and scraper development (weeks 3-4).

## Prompt interaction style

Most interactions followed this pattern:
1. Maintainer describes the task in natural language (e.g., "write a scraper for Gdańsk BIP interpellations, they use this URL format: ...")
2. AI generates initial implementation
3. Maintainer tests against real data, identifies issues
4. Iterative refinement until the script works correctly on production data

Conversations were conducted via Claude Code (CLI) and Cowork (desktop app). Session transcripts are stored locally and available on request.

## Contact

Patryk Orwat (patrykorwat@gmail.com)
