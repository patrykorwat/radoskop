#!/usr/bin/env python3
"""
Generate Open Graph images for Radoskop city instances.

Creates 1200x630 PNG images for:
  - Each councillor profile (/profil/{slug}/og.png)
  - Each vote (/glosowanie/{id}/og.png)

These images appear as rich previews when links are shared on
Facebook, Twitter, LinkedIn, Slack, etc.

Usage:
    python generate_og_images.py --base /path/to/gdansk-network
    python generate_og_images.py --base /path/to/gdansk-network --city radoskop-gdansk
"""

import argparse
import json
import math
import os
import textwrap
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

# ── Dimensions ──────────────────────────────────────────
W, H = 1200, 630
PADDING = 48
ACCENT = (79, 70, 229)       # #4f46e5
GREEN = (22, 163, 74)        # #16a34a
RED = (220, 38, 38)          # #dc2626
YELLOW = (202, 138, 4)       # #ca8a04
GRAY = (107, 114, 128)       # #6b7280
BG_DARK = (15, 23, 42)       # #0f172a  (slate-900)
BG_CARD = (30, 41, 59)       # #1e293b  (slate-800)
WHITE = (255, 255, 255)
MUTED = (148, 163, 184)      # #94a3b8  (slate-400)
BORDER = (51, 65, 85)        # #334155  (slate-700)

# ── Fonts ───────────────────────────────────────────────
FONT_DIR = "/usr/share/fonts/truetype/dejavu"

def font(size, bold=False):
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    return ImageFont.truetype(os.path.join(FONT_DIR, name), size)

FONT_TITLE = font(36, bold=True)
FONT_SUBTITLE = font(22)
FONT_STAT_VALUE = font(48, bold=True)
FONT_STAT_LABEL = font(16)
FONT_SMALL = font(14)
FONT_BRAND = font(20, bold=True)
FONT_TOPIC = font(24)
FONT_TOPIC_SM = font(20)
FONT_BAR_LABEL = font(18, bold=True)


def draw_rounded_rect(draw, xy, radius, fill):
    """Draw a rounded rectangle."""
    x0, y0, x1, y1 = xy
    draw.rounded_rectangle(xy, radius=radius, fill=fill)


def draw_brand(draw, city_name, site_url):
    """Draw Radoskop branding at the bottom."""
    y = H - PADDING - 20
    draw.text((PADDING, y), "Rado", fill=WHITE, font=FONT_BRAND)
    w1 = draw.textlength("Rado", font=FONT_BRAND)
    draw.text((PADDING + w1, y), "skop", fill=ACCENT, font=FONT_BRAND)
    w2 = draw.textlength("skop", font=FONT_BRAND)
    draw.text((PADDING + w1 + w2 + 10, y), city_name, fill=MUTED, font=FONT_SMALL)

    # URL on the right
    url_text = site_url.replace("https://", "")
    url_w = draw.textlength(url_text, font=FONT_SMALL)
    draw.text((W - PADDING - url_w, y + 3), url_text, fill=MUTED, font=FONT_SMALL)


def truncate_text(text, font_obj, max_width, draw):
    """Truncate text with ellipsis if it exceeds max_width."""
    if draw.textlength(text, font=font_obj) <= max_width:
        return text
    while len(text) > 0 and draw.textlength(text + "...", font=font_obj) > max_width:
        text = text[:-1]
    return text.rstrip() + "..."


def wrap_text(text, font_obj, max_width, draw, max_lines=3):
    """Wrap text to fit within max_width, up to max_lines."""
    words = text.split()
    lines = []
    current = ""
    for word in words:
        test = (current + " " + word).strip()
        if draw.textlength(test, font=font_obj) <= max_width:
            current = test
        else:
            if current:
                lines.append(current)
            current = word
            if len(lines) >= max_lines:
                break
    if current and len(lines) < max_lines:
        lines.append(current)
    elif current and len(lines) >= max_lines:
        # Truncate last line
        lines[-1] = truncate_text(lines[-1] + " " + current, font_obj, max_width, draw)
    return lines


# ── Vote OG Image ──────────────────────────────────────

def generate_vote_image(vote, city_name, site_url, output_path):
    """Generate OG image for a single vote."""
    img = Image.new("RGB", (W, H), BG_DARK)
    draw = ImageDraw.Draw(img)

    topic = vote.get("topic", "Glosowanie")
    counts = vote.get("counts", {})
    za = counts.get("za", 0)
    przeciw = counts.get("przeciw", 0)
    wstrzymal = counts.get("wstrzymal_sie", 0)
    brak = counts.get("brak_glosu", 0)
    nieobecni = counts.get("nieobecni", 0)
    total = za + przeciw + wstrzymal + brak + nieobecni
    session_date = vote.get("session_date", "")
    session_number = vote.get("session_number", "")

    # Result label
    if za > przeciw:
        result_text = "PRZYJETE"
        result_color = GREEN
    elif przeciw > za:
        result_text = "ODRZUCONE"
        result_color = RED
    else:
        result_text = "REMIS"
        result_color = YELLOW

    # ── Header area ──
    y = PADDING

    # Session info line
    session_info = f"Sesja {session_number}  ·  {session_date}" if session_number else session_date
    draw.text((PADDING, y), session_info, fill=MUTED, font=FONT_SMALL)
    y += 30

    # Topic (wrapped, up to 3 lines)
    topic_clean = topic.replace(";", "").strip()
    max_w = W - 2 * PADDING
    lines = wrap_text(topic_clean, FONT_TOPIC, max_w, draw, max_lines=3)
    for line in lines:
        draw.text((PADDING, y), line, fill=WHITE, font=FONT_TOPIC)
        y += 34
    y += 16

    # ── Result bar ──
    bar_y = y
    bar_h = 48
    bar_w = W - 2 * PADDING

    # Background
    draw_rounded_rect(draw, (PADDING, bar_y, W - PADDING, bar_y + bar_h), 8, BG_CARD)

    if total > 0:
        # Segments
        segments = [
            (za, GREEN, f"Za {za}"),
            (przeciw, RED, f"Przeciw {przeciw}"),
            (wstrzymal, YELLOW, f"Wstrzym. {wstrzymal}"),
        ]
        x = PADDING
        for count, color, label in segments:
            if count == 0:
                continue
            seg_w = max(int(bar_w * count / total), 2)
            draw_rounded_rect(draw, (x, bar_y, x + seg_w, bar_y + bar_h), 0, color)
            # Label inside bar
            label_w = draw.textlength(label, font=FONT_BAR_LABEL)
            if label_w < seg_w - 10:
                lx = x + (seg_w - label_w) / 2
                draw.text((lx, bar_y + 13), label, fill=WHITE, font=FONT_BAR_LABEL)
            x += seg_w

    y = bar_y + bar_h + 20

    # ── Result badge ──
    draw.text((PADDING, y), result_text, fill=result_color, font=FONT_STAT_VALUE)
    y += 60

    # ── Stats row ──
    stats = [
        (str(za), "Za", GREEN),
        (str(przeciw), "Przeciw", RED),
        (str(wstrzymal), "Wstrzym.", YELLOW),
        (str(brak), "Brak gl.", GRAY),
        (str(nieobecni), "Nieobecni", GRAY),
    ]
    stat_w = (W - 2 * PADDING) // len(stats)
    for i, (val, label, color) in enumerate(stats):
        sx = PADDING + i * stat_w
        draw.text((sx, y), val, fill=color, font=FONT_STAT_VALUE)
        val_h = 50
        draw.text((sx, y + val_h + 2), label, fill=MUTED, font=FONT_STAT_LABEL)

    # ── Brand ──
    draw_brand(draw, city_name, site_url)

    # ── Divider ──
    draw.line([(PADDING, H - PADDING - 50), (W - PADDING, H - PADDING - 50)], fill=BORDER, width=1)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(str(output_path), "PNG", optimize=True)


# ── Councillor OG Image ───────────────────────────────

def generate_councillor_image(profile, kadencja_data, city_name, site_url, clubs_config, output_path):
    """Generate OG image for a councillor profile."""
    img = Image.new("RGB", (W, H), BG_DARK)
    draw = ImageDraw.Draw(img)

    name = profile.get("name", "Radny")
    # Get the most recent kadencja data
    kad_keys = sorted(profile.get("kadencje", {}).keys(), reverse=True)
    kad = profile["kadencje"][kad_keys[0]] if kad_keys else {}

    club = kad.get("club", "")
    club_full = kad.get("club_full", club)
    frekwencja = kad.get("frekwencja", 0)
    aktywnosc = kad.get("aktywnosc", 0)
    zgodnosc = kad.get("zgodnosc_z_klubem", 0)
    votes_za = kad.get("votes_za", 0)
    votes_przeciw = kad.get("votes_przeciw", 0)
    votes_wstrzymal = kad.get("votes_wstrzymal", 0)
    votes_total = kad.get("votes_total", 0)

    # ── Avatar circle ──
    avatar_size = 100
    avatar_x = PADDING
    avatar_y = PADDING
    club_cfg = clubs_config.get(club, {})
    avatar_bg = parse_hex(club_cfg.get("avatar_bg", "#4338ca"))

    # Draw circle
    draw.ellipse(
        [avatar_x, avatar_y, avatar_x + avatar_size, avatar_y + avatar_size],
        fill=avatar_bg
    )
    # Initials
    parts = name.split()
    initials = (parts[0][0] + parts[-1][0]).upper() if len(parts) >= 2 else name[0].upper()
    init_font = font(36, bold=True)
    init_w = draw.textlength(initials, font=init_font)
    init_bbox = init_font.getbbox(initials)
    init_h = init_bbox[3] - init_bbox[1]
    draw.text(
        (avatar_x + (avatar_size - init_w) / 2, avatar_y + (avatar_size - init_h) / 2 - 5),
        initials, fill=WHITE, font=init_font
    )

    # ── Name and club ──
    name_x = avatar_x + avatar_size + 24
    draw.text((name_x, PADDING + 10), name, fill=WHITE, font=FONT_TITLE)
    subtitle = f"Klub {club_full}" if club_full else "Radny"
    draw.text((name_x, PADDING + 55), subtitle, fill=MUTED, font=FONT_SUBTITLE)

    # ── Stats cards ──
    card_y = PADDING + avatar_size + 40
    card_h = 130
    card_w = (W - 2 * PADDING - 2 * 16) // 3  # 3 cards with gaps

    cards = [
        (f"{frekwencja:.0f}%", "Frekwencja", frek_color(frekwencja)),
        (f"{aktywnosc:.0f}%", "Aktywnosc", ACCENT),
        (f"{zgodnosc:.0f}%", "Zgodnosc z klubem", zgodnosc_color(zgodnosc)),
    ]

    for i, (val, label, color) in enumerate(cards):
        cx = PADDING + i * (card_w + 16)
        draw_rounded_rect(draw, (cx, card_y, cx + card_w, card_y + card_h), 12, BG_CARD)
        # Value
        draw.text((cx + 20, card_y + 20), val, fill=color, font=FONT_STAT_VALUE)
        # Label
        draw.text((cx + 20, card_y + 80), label, fill=MUTED, font=FONT_STAT_LABEL)

    # ── Vote breakdown bar ──
    bar_y = card_y + card_h + 30
    bar_h = 36
    bar_w = W - 2 * PADDING

    draw_rounded_rect(draw, (PADDING, bar_y, W - PADDING, bar_y + bar_h), 6, BG_CARD)

    if votes_total > 0:
        segments = [
            (votes_za, GREEN),
            (votes_przeciw, RED),
            (votes_wstrzymal, YELLOW),
        ]
        x = PADDING
        for count, color in segments:
            if count == 0:
                continue
            seg_w = max(int(bar_w * count / votes_total), 2)
            draw_rounded_rect(draw, (x, bar_y, x + seg_w, bar_y + bar_h), 0, color)
            x += seg_w

    # Legend
    legend_y = bar_y + bar_h + 10
    legend_items = [
        (f"Za {votes_za}", GREEN),
        (f"Przeciw {votes_przeciw}", RED),
        (f"Wstrzym. {votes_wstrzymal}", YELLOW),
    ]
    lx = PADDING
    for text, color in legend_items:
        draw.rectangle([lx, legend_y + 3, lx + 12, legend_y + 15], fill=color)
        lx += 16
        draw.text((lx, legend_y), text, fill=MUTED, font=FONT_SMALL)
        lx += draw.textlength(text, font=FONT_SMALL) + 24

    # ── Brand ──
    draw_brand(draw, city_name, site_url)
    draw.line([(PADDING, H - PADDING - 50), (W - PADDING, H - PADDING - 50)], fill=BORDER, width=1)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(str(output_path), "PNG", optimize=True)


def parse_hex(hex_str):
    """Parse #RRGGBB to (R, G, B) tuple."""
    hex_str = hex_str.lstrip("#")
    return tuple(int(hex_str[i:i+2], 16) for i in (0, 2, 4))


def frek_color(v):
    if v >= 90:
        return GREEN
    if v >= 70:
        return YELLOW
    return RED


def zgodnosc_color(v):
    if v >= 90:
        return GREEN
    if v >= 70:
        return YELLOW
    return RED


# ── City processing ────────────────────────────────────

def process_city(city_dir: Path, force=False):
    """Generate all OG images for a city."""
    docs = city_dir / "docs"
    config_path = city_dir / "config.json"

    if not docs.exists() or not config_path.exists():
        print(f"  Skipping {city_dir.name}: missing docs/ or config.json")
        return

    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)

    city_name = config["city_name"]
    site_url = config["site_url"].rstrip("/")
    clubs = config.get("clubs", {})

    # Load profiles
    profiles_path = docs / "profiles.json"
    profiles = []
    if profiles_path.exists():
        with open(profiles_path, "r", encoding="utf-8") as f:
            profiles = json.load(f).get("profiles", [])

    # Load kadencja data for votes
    data_path = docs / "data.json"
    kadencje = []
    if data_path.exists():
        with open(data_path, "r", encoding="utf-8") as f:
            kadencje = json.load(f).get("kadencje", [])

    # ── Councillor images ──
    councillor_count = 0
    for p in profiles:
        slug = p["slug"]
        out = docs / "profil" / slug / "og.png"
        if out.exists() and not force:
            councillor_count += 1
            continue
        try:
            generate_councillor_image(p, kadencje, city_name, site_url, clubs, out)
            councillor_count += 1
        except Exception as e:
            print(f"  ERROR councillor {slug}: {e}")

    print(f"  {councillor_count} councillor OG images")

    # ── Vote images ──
    # Load full kadencja files for vote details
    vote_count = 0
    for k in kadencje:
        kid = k.get("id", "")
        kad_file = docs / f"kadencja-{kid}.json"
        if not kad_file.exists():
            continue

        with open(kad_file, "r", encoding="utf-8") as f:
            kad_data = json.load(f)

        for vote in kad_data.get("votes", []):
            vid = vote.get("id", "")
            if not vid:
                continue
            out = docs / "glosowanie" / vid / "og.png"
            if out.exists() and not force:
                vote_count += 1
                continue
            try:
                generate_vote_image(vote, city_name, site_url, out)
                vote_count += 1
            except Exception as e:
                print(f"  ERROR vote {vid}: {e}")

    print(f"  {vote_count} vote OG images")


def main():
    parser = argparse.ArgumentParser(description="Generate OG images for Radoskop")
    parser.add_argument("--base", required=True, help="Base directory containing radoskop-* city dirs")
    parser.add_argument("--city", default=None, help="Process only this city (e.g. radoskop-gdansk)")
    parser.add_argument("--force", action="store_true", help="Regenerate even if images exist")
    args = parser.parse_args()

    base = Path(args.base)

    if args.city:
        cities = [args.city]
    else:
        cities = sorted([
            d.name for d in base.iterdir()
            if d.is_dir() and d.name.startswith("radoskop-") and d.name != "radoskop"
        ])

    for city in cities:
        city_dir = base / city
        if city_dir.exists():
            print(f"\n=== {city} ===")
            process_city(city_dir, force=args.force)
        else:
            print(f"  Skipping {city}: not found")


if __name__ == "__main__":
    main()
