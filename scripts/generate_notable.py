#!/usr/bin/env python3
"""Generate krajowe strony "Głośne głosowania" (notable / contested votes).

Czyta zagregowany `radoskop/docs/votes-index.json` (płaska lista wierszy
[topic, citySlug, voteId, date, za, przeciw, wstrzymal] ze wszystkich miast,
budowana przez build_votes_index.py) i mapuje sloty miast na site_url + nazwę
z config.json. Produkuje, w katalogu apex `radoskop/docs/notable/`:

  - /notable/index.html            spis tygodni (najnowsze pierwsze)
  - /notable/{YYYY}-Www/index.html  najbardziej sporne głosowania danego
                                     tygodnia ISO (link do strony /vote/ miasta)

Strony są samodzielnym HTML (nie SPA shellem apexu) — czysta treść dla Google,
bez zależności od JS. Wpisy do sitemapy apexu dokłada build_main_sitemap.py
(skanuje ten katalog). Generator jest częścią publicznego repo radoskop
(AGPL), tak jak generate_seo_pages.py i build_votes_index.py.

Definicja "sporne": głosowanie z realnym sprzeciwem (przeciw > 0) i co najmniej
MIN_ACTIVE oddanymi głosami; ranking w tygodniu wg podzielenia (im bliżej 50/50,
tym wyżej). Nie filtrujemy twardo po marginesie, żeby każdy tydzień miał komplet.

Usage:
    python generate_notable.py --workspace /path/to/monorepo-root
    python generate_notable.py --workspace . --base-url https://radoskop.pl
"""

from __future__ import annotations

import argparse
import datetime
import html
import json
import sys
from pathlib import Path

MIN_ACTIVE = 20        # min oddanych głosów, żeby wynik liczył się krajowo
PER_WEEK = 25          # ile głosowań na stronę tygodnia
MAX_WEEKS = 52         # ile ostatnich tygodni generować (cap backfillu)
DEFAULT_BASE_URL = "https://radoskop.pl"


def esc(text) -> str:
    return html.escape(str(text), quote=True)


def _parse_date(s):
    try:
        return datetime.date.fromisoformat(str(s)[:10])
    except (ValueError, TypeError):
        return None


def divisiveness(za: int, przeciw: int, wstrzymal: int) -> float:
    """0.0 = idealnie podzielone (50/50), rośnie do 50.0 dla jednomyślnych.

    Mniejsza wartość = bardziej sporne. Liczone jako |50 − %za| na aktywnych.
    """
    active = (za or 0) + (przeciw or 0) + (wstrzymal or 0)
    if active <= 0:
        return 50.0
    pct_za = (za or 0) / active * 100
    return abs(50.0 - pct_za)


def iso_week_key(d: datetime.date) -> str:
    """ISO rok-tydzień jako '2026-W23' (zero-padded, sortowalny leksykalnie)."""
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def select_contested(rows, min_active: int = MIN_ACTIVE) -> list[dict]:
    """Z wierszy votes-index.json wybiera kwalifikujące się sporne głosowania.

    Zwraca listę dictów z policzonym `div` (podzielenie) i `_date`. Wiersze
    bez daty albo bez sprzeciwu są pomijane.
    """
    out: list[dict] = []
    for row in rows:
        if not isinstance(row, list) or len(row) < 7:
            continue
        topic, slug, vid, date, za, przeciw, wst = row[:7]
        za = int(za or 0)
        przeciw = int(przeciw or 0)
        wst = int(wst or 0)
        active = za + przeciw + wst
        if active < min_active or przeciw <= 0:
            continue
        d = _parse_date(date)
        if d is None:
            continue
        out.append({
            "topic": topic or "",
            "slug": slug,
            "id": vid,
            "date": date,
            "_date": d,
            "za": za,
            "przeciw": przeciw,
            "wstrzymal": wst,
            "div": divisiveness(za, przeciw, wst),
        })
    return out


def group_by_week(selected, per_week: int = PER_WEEK, max_weeks: int = MAX_WEEKS):
    """Grupuje po tygodniu ISO, sortuje w tygodniu wg podzielenia, tnie do
    per_week. Zwraca listę (week_key, [votes]) od najnowszego tygodnia.
    """
    weeks: dict[str, list[dict]] = {}
    for v in selected:
        weeks.setdefault(iso_week_key(v["_date"]), []).append(v)
    ordered = sorted(weeks.keys(), reverse=True)[:max_weeks]
    result = []
    for wk in ordered:
        votes = sorted(weeks[wk], key=lambda v: (v["div"], -v["_date"].toordinal()))
        result.append((wk, votes[:per_week]))
    return result


def _city_map(workspace: Path) -> dict[str, dict]:
    """slug → {site_url, city_name} z config.json każdego miasta."""
    out: dict[str, dict] = {}
    mono = workspace / "radoskop" / "cities"
    dirs = []
    if mono.is_dir():
        dirs = [d for d in mono.iterdir() if d.is_dir()]
    else:
        dirs = [d for d in workspace.iterdir()
                if d.is_dir() and d.name.startswith("radoskop-") and d.name != "radoskop-premium"]
    for d in dirs:
        cfg = d / "config.json"
        if not cfg.is_file():
            continue
        try:
            with cfg.open(encoding="utf-8") as f:
                c = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        slug = d.name[len("radoskop-"):] if d.name.startswith("radoskop-") else d.name
        out[slug] = {
            "site_url": (c.get("site_url") or f"https://{slug}.radoskop.pl").rstrip("/"),
            "city_name": c.get("city_name") or slug.capitalize(),
        }
    return out


def _week_label(week_key: str) -> str:
    """'2026-W23' → 'tydzień 23, 2026'."""
    try:
        y, w = week_key.split("-W")
        return f"tydzień {int(w)}, {y}"
    except (ValueError, IndexError):
        return week_key


# ── Render samodzielnego HTML ────────────────────────────────────────────
def _doc(title: str, description: str, canonical: str, body: str, jsonld: list) -> str:
    ld = ""
    if jsonld:
        payload = json.dumps(jsonld if len(jsonld) > 1 else jsonld[0],
                             ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
        ld = f'<script type="application/ld+json">{payload}</script>\n'
    return f"""<!DOCTYPE html>
<html lang="pl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{esc(title)}</title>
<meta name="description" content="{esc(description)}">
<link rel="canonical" href="{canonical}">
<meta property="og:type" content="website">
<meta property="og:title" content="{esc(title)}">
<meta property="og:description" content="{esc(description)}">
<meta property="og:url" content="{canonical}">
<meta property="og:site_name" content="Radoskop">
<meta name="twitter:card" content="summary">
{ld}<style>
  body {{ margin:0; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif; background:#f8f9fa; color:#1a1d27; line-height:1.6; }}
  main {{ max-width:820px; margin:0 auto; padding:32px 20px 64px; }}
  h1 {{ font-size:1.7rem; }}
  h2 {{ font-size:1.15rem; margin-top:32px; }}
  a {{ color:#4f46e5; text-decoration:none; }}
  a:hover {{ text-decoration:underline; }}
  .vote {{ padding:14px 0; border-bottom:1px solid #e5e7eb; }}
  .meta {{ color:#6b7280; font-size:.9rem; }}
  .city {{ font-weight:600; }}
  ul.weeks {{ list-style:none; padding:0; }}
  ul.weeks li {{ padding:10px 0; border-bottom:1px solid #e5e7eb; }}
  @media (prefers-color-scheme: dark) {{
    body {{ background:#0f1117; color:#e4e4e7; }}
    a {{ color:#818cf8; }}
    .vote, ul.weeks li {{ border-color:#27272a; }}
    .meta {{ color:#a1a1aa; }}
  }}
</style>
</head>
<body>
<main>
{body}
</main>
</body>
</html>
"""


def _result(za: int, przeciw: int) -> str:
    if za > przeciw:
        return "przyjete"
    if przeciw > za:
        return "odrzucone"
    return "remis"


def build_week_page(week_key, votes, city_map, base_url) -> str:
    canonical = f"{base_url}/notable/{week_key}/"
    label = _week_label(week_key)
    title = f"Głośne głosowania – {label} – Radoskop"
    desc = (f"Najbardziej sporne głosowania rad miejskich w Polsce, {label}. "
            f"Tam gdzie rady były najbardziej podzielone.")

    rows_html = []
    item_list = []
    for i, v in enumerate(votes, 1):
        city = city_map.get(v["slug"], {})
        city_name = city.get("city_name", v["slug"])
        site_url = city.get("site_url", f"https://{v['slug']}.radoskop.pl")
        vote_url = f"{site_url}/vote/{v['id']}/"
        topic = (v["topic"] or "").strip() or "Glosowanie"
        rows_html.append(
            f'<div class="vote"><span class="city">{esc(city_name)}</span>: '
            f'<a href="{vote_url}">{esc(topic[:180])}</a><br>'
            f'<span class="meta">{_result(v["za"], v["przeciw"])} · '
            f'za {v["za"]}, przeciw {v["przeciw"]}, wstrzymało się {v["wstrzymal"]} · '
            f'{esc(v["date"])}</span></div>'
        )
        item_list.append({"@type": "ListItem", "position": i, "url": vote_url,
                           "name": f"{city_name}: {topic[:120]}"})

    body = (
        f"<h1>Głośne głosowania – {esc(label)}</h1>\n"
        f"<p>Głosowania rad miejskich w Polsce, w których rada była najbardziej "
        f"podzielona. Ranking wg tego, jak blisko wynik był podziału po połowie.</p>\n"
        f"<p><a href=\"{base_url}/notable/\">← wszystkie tygodnie</a></p>\n"
        + "\n".join(rows_html) + "\n"
    )
    jsonld = [
        {"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "Radoskop", "item": f"{base_url}/"},
            {"@type": "ListItem", "position": 2, "name": "Głośne głosowania", "item": f"{base_url}/notable/"},
            {"@type": "ListItem", "position": 3, "name": label, "item": canonical},
        ]},
        {"@context": "https://schema.org", "@type": "ItemList", "itemListElement": item_list},
    ]
    return _doc(title, desc, canonical, body, jsonld)


def build_index_page(weeks, base_url) -> str:
    canonical = f"{base_url}/notable/"
    title = "Głośne głosowania rad miejskich w Polsce – Radoskop"
    desc = ("Najbardziej sporne głosowania rad miejskich w Polsce, tydzień po "
            "tygodniu. Tam gdzie rady były najbardziej podzielone.")
    li = []
    for wk, votes in weeks:
        li.append(
            f'<li><a href="{base_url}/notable/{wk}/">{esc(_week_label(wk))}</a> '
            f'<span class="meta">· {len(votes)} głosowań</span></li>'
        )
    body = (
        "<h1>Głośne głosowania rad miejskich w Polsce</h1>\n"
        "<p>Tam gdzie rady miast były najbardziej podzielone. Co tydzień zbieramy "
        "najbardziej sporne głosowania ze wszystkich monitorowanych rad.</p>\n"
        "<ul class=\"weeks\">\n" + "\n".join(li) + "\n</ul>\n"
    )
    jsonld = [{"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": [
        {"@type": "ListItem", "position": 1, "name": "Radoskop", "item": f"{base_url}/"},
        {"@type": "ListItem", "position": 2, "name": "Głośne głosowania", "item": canonical},
    ]}]
    return _doc(title, desc, canonical, body, jsonld)


def generate(workspace: Path, output_dir: Path, base_url: str = DEFAULT_BASE_URL) -> int:
    votes_index = workspace / "radoskop" / "docs" / "votes-index.json"
    if not votes_index.is_file():
        print(f"generate_notable: brak {votes_index}, pomijam", file=sys.stderr)
        return 0
    try:
        with votes_index.open(encoding="utf-8") as f:
            rows = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"generate_notable: nie moge wczytac votes-index.json: {e}", file=sys.stderr)
        return 0

    selected = select_contested(rows)
    weeks = group_by_week(selected)
    if not weeks:
        print("generate_notable: brak spornych glosowan, nic nie generuje", file=sys.stderr)
        return 0

    city_map = _city_map(workspace)
    notable_dir = output_dir / "notable"
    notable_dir.mkdir(parents=True, exist_ok=True)

    (notable_dir / "index.html").write_text(
        build_index_page(weeks, base_url), encoding="utf-8")
    for wk, votes in weeks:
        wk_dir = notable_dir / wk
        wk_dir.mkdir(parents=True, exist_ok=True)
        (wk_dir / "index.html").write_text(
            build_week_page(wk, votes, city_map, base_url), encoding="utf-8")

    print(f"generate_notable: {len(weeks)} tygodni, "
          f"{sum(len(v) for _, v in weeks)} glosowan → {notable_dir}", file=sys.stderr)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Generate national notable-votes pages")
    ap.add_argument("--workspace", required=True,
                    help="Katalog z radoskop/ (monorepo root albo katalog z radoskop-* repo)")
    ap.add_argument("--output-dir", default=None,
                    help="Gdzie pisac (default: <workspace>/radoskop/docs)")
    ap.add_argument("--base-url", default=DEFAULT_BASE_URL,
                    help=f"Bazowy URL apexu (default: {DEFAULT_BASE_URL})")
    args = ap.parse_args()

    workspace = Path(args.workspace).resolve()
    output_dir = (Path(args.output_dir).resolve() if args.output_dir
                  else workspace / "radoskop" / "docs")
    return generate(workspace, output_dir, args.base_url.rstrip("/"))


if __name__ == "__main__":
    raise SystemExit(main())
