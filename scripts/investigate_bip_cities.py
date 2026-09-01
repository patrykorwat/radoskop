#!/usr/bin/env python3
"""Investigate Zlotow + Gozdnica on bip.net.pl for Radoskop (voting reports)."""
import sys, re, json
from pathlib import Path
sys.path.insert(0, "/opt/data/workspace/radoskoppl/radoskop/scripts")
from lib_bip_net import NefeniRaport

CITIES = {
    "zlotow": "https://zlotow.bip.net.pl",
    "gozdnica": "https://gozdnica.bip.net.pl",
}

def dump_categories(nb):
    html = nb.fetch(nb.base_url + "/", use_cache=False)
    cats = []
    for m in re.finditer(r'href="(/kategorie/\d+-[^"]*)"[^>]*>\s*([^<]{0,120})', html):
        href, title = m.group(1), " ".join(m.group(2).split())
        cats.append((href, title))
    # dedupe
    seen = {}
    for href, title in cats:
        key = href.split('?')[0]
        seen.setdefault(key, title)
    return seen

for slug, base in CITIES.items():
    print(f"\n========== {slug.upper()} ==========")
    nb = NefeniRaport(base, debug=True)
    cats = dump_categories(nb)
    print(f"Total category links: {len(cats)}")
    for href, title in sorted(cats.items()):
        print(f"  {href}  |  {title[:70]}")
