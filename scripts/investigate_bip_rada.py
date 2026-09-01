#!/usr/bin/env python3
"""Explore Rada category subcategories for both cities."""
import sys, re, json
sys.path.insert(0, "/opt/data/workspace/radoskoppl/radoskop/scripts")
from lib_bip_net import NefeniRaport

def links_in(nb, path):
    html = nb.fetch(nb.base_url + "/" + path.lstrip("/"), use_cache=False)
    out = {}
    for m in re.finditer(r'href="(/kategorie/\d+[^"]*)"[^>]*>\s*([^<]{0,140})', html):
        href, title = m.group(1), " ".join(m.group(2).split())
        key = href.split('?')[0]
        out.setdefault(key, title)
    return out

# Zlotow: Rada Miejska id 5
nb = NefeniRaport("https://zlotow.bip.net.pl", debug=True)
print("=== ZLOTOW /kategorie/5-rada-miejska ===")
for href, t in links_in(nb, "/kategorie/5-rada-miejska").items():
    print(f"  {href}  |  {t[:80]}")

# Gozdnica: Organy id 7 and rada id 64
nb2 = NefeniRaport("https://gozdnica.bip.net.pl", debug=True)
print("\n=== GOZDNICA /kategorie/7-organy ===")
for href, t in links_in(nb2, "/kategorie/7-organy").items():
    print(f"  {href}  |  {t[:80]}")
print("\n=== GOZDNICA /kategorie/64-rada ===")
for href, t in links_in(nb2, "/kategorie/64-rada").items():
    print(f"  {href}  |  {t[:80]}")
