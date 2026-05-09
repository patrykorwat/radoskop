#!/usr/bin/env python3
"""
Generate social media card images (PNG, 1200x630) for each councilor.

Usage:
    python3 generate_councilor_cards.py --base /path/to/gdansk-network [--city radoskop-gdansk]
"""

import argparse
import json
import os
import sys
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont


def get_initials(name: str) -> str:
    """Extract initials from a name (first letter of first and last name)."""
    parts = name.strip().split()
    if len(parts) < 2:
        return name[:2].upper()
    return (parts[0][0] + parts[-1][0]).upper()


def get_metric_color(value: float) -> str:
    """
    Get color for a metric based on its value.

    Args:
        value: Float between 0 and 1 (or 0-100 in percentage terms)

    Returns:
        Hex color string
    """
    # Normalize if value is 0-100
    if value > 1:
        value = value / 100

    if value > 0.8:
        return "#10b981"  # Green
    elif value > 0.6:
        return "#f59e0b"  # Yellow
    else:
        return "#ef4444"  # Red


def get_club_color(club: str, config: dict) -> str:
    """Get the club color from config."""
    club_config = config.get("clubs", {}).get(club, {})
    return club_config.get("avatar_bg", "#6b7280")  # Default gray


def load_font(size: int) -> ImageFont.FreeTypeFont:
    """Load a TrueType font, with fallback to default."""
    font_paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/System/Library/Fonts/Arial.ttf",  # macOS
    ]

    for font_path in font_paths:
        if os.path.exists(font_path):
            try:
                return ImageFont.truetype(font_path, size)
            except Exception:
                continue

    # Fallback to default font
    return ImageFont.load_default()


def generate_card(
    name: str,
    club: str,
    frekwencja: float,
    aktywnosc: float,
    zgodnosc_z_klubem: float,
    config: dict,
    city_name: str,
    output_path: Path,
) -> None:
    """
    Generate a single councilor card.

    Args:
        name: Councilor's name
        club: Club abbreviation
        frekwencja: Attendance rate (0-1)
        aktywnosc: Activity rate (0-1)
        zgodnosc_z_klubem: Club agreement rate (0-1)
        config: City config dict
        city_name: City name for display
        output_path: Where to save the PNG
    """
    # Create image
    width, height = 1200, 630
    img = Image.new("RGB", (width, height), color="#f8f9fa")
    draw = ImageDraw.Draw(img)

    # Load fonts
    font_large = load_font(80)  # For initials
    font_name = load_font(48)   # For councilor name
    font_club = load_font(32)   # For club name
    font_metric_label = load_font(28)  # For metric labels
    font_metric_value = load_font(52)  # For metric values
    font_small = load_font(24)  # For bottom text

    # Left section: Circle with initials
    circle_radius = 120
    circle_x = 140
    circle_y = 315

    club_color = get_club_color(club, config)
    draw.ellipse(
        [circle_x - circle_radius, circle_y - circle_radius,
         circle_x + circle_radius, circle_y + circle_radius],
        fill=club_color
    )

    # Draw initials
    initials = get_initials(name)
    bbox = draw.textbbox((0, 0), initials, font=font_large)
    text_width = bbox[2] - bbox[0]
    text_height = bbox[3] - bbox[1]
    text_x = circle_x - text_width // 2
    text_y = circle_y - text_height // 2
    draw.text((text_x, text_y), initials, fill="white", font=font_large)

    # Right section: Text content
    text_x_start = 320

    # Councilor name
    draw.text((text_x_start, 80), name, fill="#111827", font=font_name)

    # Club name
    draw.text((text_x_start, 155), club, fill="#6b7280", font=font_club)

    # Metrics section (bottom third)
    metric_y = 380
    metric_box_width = 250
    metric_box_height = 180
    metric_spacing = 30

    metrics = [
        ("Frekwencja", frekwencja),
        ("Aktywność", aktywnosc),
        ("Zgodność", zgodnosc_z_klubem),
    ]

    for idx, (label, value) in enumerate(metrics):
        # Normalize: if value is 0-100 scale, convert to 0-1 for bar rendering
        norm_value = value / 100 if value > 1 else value
        box_x = text_x_start + idx * (metric_box_width + metric_spacing)

        # Box background
        draw.rectangle(
            [box_x, metric_y, box_x + metric_box_width, metric_y + metric_box_height],
            fill="#ffffff",
            outline="#e5e7eb",
            width=2
        )

        # Percentage value
        percentage = int(round(value)) if value > 1 else int(round(value * 100))
        value_text = f"{percentage}%"
        bbox = draw.textbbox((0, 0), value_text, font=font_metric_value)
        value_width = bbox[2] - bbox[0]
        value_x = box_x + (metric_box_width - value_width) // 2
        draw.text((value_x, metric_y + 30), value_text, fill="#111827", font=font_metric_value)

        # Label
        bbox = draw.textbbox((0, 0), label, font=font_metric_label)
        label_width = bbox[2] - bbox[0]
        label_x = box_x + (metric_box_width - label_width) // 2
        draw.text((label_x, metric_y + 100), label, fill="#6b7280", font=font_metric_label)

        # Color bar underneath
        bar_y = metric_y + 150
        bar_color = get_metric_color(norm_value)
        draw.rectangle(
            [box_x, bar_y, box_x + metric_box_width, bar_y + 12],
            fill="#e5e7eb"
        )
        # Filled portion
        fill_width = metric_box_width * norm_value
        draw.rectangle(
            [box_x, bar_y, box_x + fill_width, bar_y + 12],
            fill=bar_color
        )

    # Top right: City name
    city_text = city_name
    bbox = draw.textbbox((0, 0), city_text, font=font_metric_label)
    text_width = bbox[2] - bbox[0]
    draw.text((width - text_width - 30, 30), city_text, fill="#6b7280", font=font_metric_label)

    # Bottom right: Branding
    branding_text = "radoskop.pl"
    bbox = draw.textbbox((0, 0), branding_text, font=font_small)
    text_width = bbox[2] - bbox[0]
    draw.text((width - text_width - 30, height - 50), branding_text, fill="#9ca3af", font=font_small)

    # Ensure output directory exists
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Save image
    img.save(output_path, "PNG")
    print(f"Generated: {output_path}")


def main():
    parser = argparse.ArgumentParser(
        description="Generate social media cards for councilors"
    )
    parser.add_argument(
        "--base",
        required=True,
        help="Path to gdansk-network directory"
    )
    parser.add_argument(
        "--city",
        help="Specific city to process (e.g., radoskop-gdansk). If not provided, process all."
    )

    args = parser.parse_args()

    base_path = Path(args.base)

    if not base_path.exists():
        print(f"Error: Base path does not exist: {base_path}")
        sys.exit(1)

    # Find cities to process
    if args.city:
        cities = [args.city]
    else:
        # Find all radoskop-* directories
        cities = [
            d.name for d in base_path.iterdir()
            if d.is_dir() and d.name.startswith("radoskop-")
        ]

    if not cities:
        print("Error: No cities found to process")
        sys.exit(1)

    print(f"Processing {len(cities)} city/cities: {', '.join(cities)}")

    for city_dir in cities:
        city_path = base_path / city_dir
        config_path = city_path / "config.json"
        data_path = city_path / "docs" / "data.json"
        profiles_path = city_path / "docs" / "profiles.json"

        if not config_path.exists():
            print(f"Warning: config.json not found in {city_path}, skipping")
            continue

        if not data_path.exists():
            print(f"Warning: data.json not found in {city_path}, skipping")
            continue

        # Load config
        with open(config_path) as f:
            config = json.load(f)

        # Load data
        with open(data_path) as f:
            data = json.load(f)

        # Load profiles if available
        profiles_by_name = {}
        if profiles_path.exists():
            with open(profiles_path) as f:
                profiles_data = json.load(f)
                for profile in profiles_data.get("profiles", []):
                    profiles_by_name[profile["name"]] = profile

        # Get default kadencja
        default_kadencja_id = data.get("default_kadencja")
        if not default_kadencja_id:
            # Use the last kadencja if no default specified
            kadencje = data.get("kadencje", [])
            if not kadencje:
                print(f"Warning: No kadencje found in {city_path}, skipping")
                continue
            default_kadencja_id = kadencje[-1]["id"]

        print(f"\nProcessing {city_dir} (kadencja: {default_kadencja_id})")

        # Find the default kadencja
        target_kadencja = None
        for k in data.get("kadencje", []):
            if k["id"] == default_kadencja_id:
                target_kadencja = k
                break

        if not target_kadencja:
            print(f"Warning: Kadencja {default_kadencja_id} not found in {city_path}, skipping")
            continue

        # Process each councilor
        councilors = target_kadencja.get("councilors", [])
        city_name = config.get("city_name", city_dir.replace("radoskop-", ""))

        for councilor in councilors:
            name = councilor.get("name")
            club = councilor.get("club")
            frekwencja = councilor.get("frekwencja", 0)
            aktywnosc = councilor.get("aktywnosc", 0)
            zgodnosc_z_klubem = councilor.get("zgodnosc_z_klubem", 0)

            # Get slug from profiles
            slug = None
            if name in profiles_by_name:
                slug = profiles_by_name[name].get("slug")

            if not slug:
                # Generate slug from name
                slug = name.lower().replace(" ", "-")
                slug = "".join(c if c.isalnum() or c == "-" else "" for c in slug)

            # Output path. Po migracji 2026-05 angielski URL slug /profile/.
            output_path = city_path / "docs" / "profile" / slug / "og.png"

            # Generate card
            generate_card(
                name=name,
                club=club,
                frekwencja=frekwencja,
                aktywnosc=aktywnosc,
                zgodnosc_z_klubem=zgodnosc_z_klubem,
                config=config,
                city_name=city_name,
                output_path=output_path,
            )

        print(f"Completed {city_dir}: {len(councilors)} cards generated")


if __name__ == "__main__":
    main()
