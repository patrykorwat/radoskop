#!/usr/bin/env python3
"""Check Zlotow protokoly/transmisje categories + Gozdnica eSesja article."""
import sys, re
sys.path.insert(0, "/opt/data/workspace/radoskoppl/radoskop/scripts")
from lib_bip_net import NefeniRaport

# Zlotow: list articles in protokoly (46) and transmisje (47)
nb = NefeniRaport("https://zlotow.bip.net.pl", debug=True)

def articles_in(nb, path):
    html = nb.fetch(nb.base_url + "/" + path.lstrip("/"), use_cache=False)
    arts = []
    for m in re.finditer(r'href="(/kategorie/\d+[^"]*/artykuly/\d+[^"]*)"[^>]*>\s*([^<]{0,140})', html):
        href, title = m.group(1), " ".join(m.group(2).split())
        arts.append((href.split('?')[0], title))
    # dedupe
    seen = {}
    for h, t in arts:
        seen.setdefault(h, t)
    return seen

for path in ["/kategorie/46-protokoly-z-sesji-rady-miejskiej",
             "/kategorie/47-transmisje-z-obrad-rady-miejskiej",
             "/kategorie/44-plan-pracy-rady-miejskiej"]:
    print(f"\n=== ZLOTOW {path} ===")
    for h, t in articles_in(nb, path).items():
        print(f"  {h}  |  {t[:80]}")

# Gozdnica eSesja article 129
nb2 = NefeniRaport("https://gozdnica.bip.net.pl", debug=True)
print("\n=== GOZDNICA article 129 (eSesja - materiały wideo i wyniki głosowań) ===")
html = nb2.fetch(nb2.base_url + "/kategorie/64-rada/artykuly/129-esesja-materialy-wideo-i-wyniki-glosowan", use_cache=False)
# find links
for m in re.finditer(r'href="([^"]+)"[^>]*>([^<]{0,90})', html):
    href, txt = m.group(1), " ".join(m.group(2).split())
    if 'esesja' in href.lower() or 'eSesja' in txt or 'esesja' in txt.lower():
        print(f"  [{href}] {txt[:70]}")
# also show body text around 'głosowa'
for m in re.finditer(r'.{40}glosowa.{60}', html, re.IGNORECASE):
    print("   TXT:", " ".join(m.group(0).split())[:140])
