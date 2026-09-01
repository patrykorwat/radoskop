#!/usr/bin/env python3
"""Deep search for voting categories in both cities' raw HTML."""
import sys, re
sys.path.insert(0, "/opt/data/workspace/radoskoppl/radoskop/scripts")
from lib_bip_net import NefeniRaport

def grep_html(nb, path, pattern):
    html = nb.fetch(nb.base_url + "/" + path.lstrip("/"), use_cache=False)
    print(f"\n--- {nb.base_url} {path} : matches for '{pattern}' ---")
    found = 0
    for m in re.finditer(r'.{60}' + pattern + r'.{40}', html, re.IGNORECASE | re.DOTALL):
        snippet = " ".join(m.group(0).split())
        href = re.search(r'href="(?P<u>[^"]+)"', snippet)
        u = href.group('u') if href else ""
        print(f"  [{u}] {snippet[:130]}")
        found += 1
        if found > 25:
            print("  ... (truncated)")
            break
    if not found:
        print("  (no matches)")

# Zlotow rada page
nb = NefeniRaport("https://zlotow.bip.net.pl", debug=False)
grep_html(nb, "/kategorie/5-rada-miejska", r"[Gg]łosowań")
grep_html(nb, "/kategorie/5-rada-miejska", r"[Gg]losowani")
# protokoły - check if votes are inside
grep_html(nb, "/kategorie/46-protokoly-z-sesji-rady-miejskiej", r"[Gg]łosowań")

# Gozdnica rada page
nb2 = NefeniRaport("https://gozdnica.bip.net.pl", debug=False)
grep_html(nb2, "/kategorie/64-rada", r"[Gg]łosowań")
