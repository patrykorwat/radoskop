#!/usr/bin/env python3
"""
Radoskop city OG card (1200x630) — the ONE pre-rendered OG image per site.

History: this module used to batch-generate a PNG per councillor profile,
per vote and per session (thousands of files per city) that then got synced
to S3. That was waste: since the "dynamic UI" migration (2026-05) those
per-route cards are rendered on demand by radoskop-premium data_api.py
(/api/{city}/og/...) behind the Cloudflare worker proxy with 24h edge cache,
and deploy_all_s3.py --delete already pruned the legacy files from the
bucket. The batch generators were removed; only the city card remains
because it cannot be rendered on demand at zero cost — head.html points
every page without its own OG at {{SITE_URL}}/og.png (static S3 object) and
bluesky_bot.py uploads it directly with each city post.

Deterministic per domain, so regeneration never churns S3 or CF cache.

Usage (called from generate_site.py per city):
    from generate_og_images import render_city_card
    render_city_card("Gdańsk", "gdansk.radoskop.pl", docs / "og.png")
"""

import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# ── Dimensions ──────────────────────────────────────────
W, H = 1200, 630
PADDING = 48
ACCENT = (79, 70, 229)       # #4f46e5
GRAY = (107, 114, 128)       # #6b7280
WHITE = (255, 255, 255)

# ── Fonts ───────────────────────────────────────────────
FONT_DIR = "/usr/share/fonts/truetype/dejavu"

def font(size, bold=False):
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    return ImageFont.truetype(os.path.join(FONT_DIR, name), size)


# ── Domyślna karta miasta (OG) ─────────────────────────
# Lekka, jasna karta marki dla stron bez własnego dynamicznego OG: strona
# główna miasta, lista interpelacji, sesje (po fallbacku w workerze). Spójna
# wizualnie z bannerem/avatarem na X i Bluesky. Słupki "głosowań" są
# deterministyczne per slug, żeby regeneracja nie zmieniała obrazka i nie
# powodowała churnu na S3 / cache-bustu.

_CARD_BG_TOP = (248, 249, 251)     # #f8f9fb
_CARD_BG_BOTTOM = (236, 238, 243)  # #eceef3
_CARD_DARK = (26, 29, 39)          # #1a1d27
_CARD_BARS = [
    (79, 70, 229), (220, 38, 38), (124, 58, 237), (234, 88, 12),
    (2, 132, 199), (8, 145, 178), (5, 150, 105), (245, 158, 11),
]


def _vertical_gradient(top, bottom):
    """Pionowy gradient W×H między dwoma kolorami."""
    base = Image.new("RGB", (W, H))
    px = base.load()
    for y in range(H):
        t = y / (H - 1)
        r = int(top[0] + (bottom[0] - top[0]) * t)
        g = int(top[1] + (bottom[1] - top[1]) * t)
        b = int(top[2] + (bottom[2] - top[2]) * t)
        for x in range(W):
            px[x, y] = (r, g, b)
    return base


def render_city_card(city_name, domain, output_path):
    """Domyślna karta OG miasta (1200×630). `domain` bez schematu, np.
    'warszawa.radoskop.pl'. Deterministyczna względem domeny."""
    import random as _random
    img = _vertical_gradient(_CARD_BG_TOP, _CARD_BG_BOTTOM)
    draw = ImageDraw.Draw(img)

    # Górny pasek marki.
    draw.rectangle([0, 0, W, 10], fill=ACCENT)

    # Kafelek "R".
    draw.rounded_rectangle([PADDING, 70, PADDING + 84, 154], radius=18, fill=ACCENT)
    f_r = font(56, bold=True)
    rb = draw.textbbox((0, 0), "R", font=f_r)
    draw.text((PADDING + 42 - (rb[2] - rb[0]) / 2, 112 - (rb[3] - rb[1]) / 2 - rb[1]),
              "R", fill=WHITE, font=f_r)

    # Wordmark "Radoskop {Miasto}".
    f_title = font(58, bold=True)
    draw.text((PADDING + 104, 78), f"Radoskop {city_name}", fill=_CARD_DARK, font=f_title)

    # Tagline + lista funkcji + domena.
    draw.text((PADDING, 222), "Zobacz, jak Twoje miasto podejmuje decyzje", fill=_CARD_DARK, font=font(40))
    draw.text((PADDING, 292), "Frekwencja  ·  głosowania  ·  interpelacje  ·  profile radnych",
              fill=GRAY, font=font(27))
    draw.text((PADDING, 350), domain, fill=ACCENT, font=font(26, bold=True))

    # Motyw słupków głosowań na dole (deterministyczny per domena).
    rnd = _random.Random(domain)
    x = PADDING
    base_y = H - 40
    while x < W - PADDING:
        bw = rnd.choice([10, 12, 13, 15])
        bh = rnd.randint(12, 120)
        col = rnd.choice(_CARD_BARS)
        draw.rounded_rectangle([x, base_y - bh, x + bw, base_y], radius=3, fill=col)
        x += bw + rnd.choice([12, 15, 18, 20])

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    img.save(str(output_path), "PNG")
    return output_path


if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Regenerate a city's docs/og.png card")
    parser.add_argument("--config", required=True, help="city config.json")
    parser.add_argument("--output", required=True, help="city docs/ dir")
    args = parser.parse_args()

    with open(args.config, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    name = cfg.get("city_name") or cfg.get("voivodeship_name", "")
    domain = cfg["site_url"].replace("https://", "").replace("http://", "").rstrip("/")
    out = render_city_card(name, domain, Path(args.output) / "og.png")
    print(f"og.png → {out}")
