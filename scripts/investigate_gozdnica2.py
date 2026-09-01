#!/usr/bin/env python3
"""Gozdnica: check eSesja 2026 homepage items + skład rady article attachment."""
import sys, re
sys.path.insert(0, "/opt/data/workspace/radoskoppl/radoskop/scripts")
import requests
from lib_bip_net import NefeniRaport
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"

# eSesja homepage - what are the 2026-dated entries?
r = requests.get("https://gozdnica.esesja.pl/", headers={"User-Agent":UA}, timeout=40)
import html as H
txt = re.sub(r'<script.*?</script>', ' ', r.text, flags=re.DOTALL)
txt = re.sub(r'<[^>]+>', ' ', txt); txt = re.sub(r'\s+', ' ', txt)
for m in re.finditer(r'.{60}(?:18 sierpnia 2026|12 sierpnia 2026|1 lipca 2026|27 lipca 2026).{60}', txt):
    print("HP:", m.group(0)[:160])
print("\n--- links to sessions on esesja homepage ---")
for m in re.finditer(r'href="(/?glosow[^"]*)"', r.text):
    print("GLOS:", m.group(1))
for m in re.finditer(r'href="(/[^"]*sesj[^"]*)"', r.text, re.I):
    print("SESJ:", m.group(1))

# skład rady article 122 raw
nb2 = NefeniRaport("https://gozdnica.bip.net.pl", debug=False)
h = nb2.fetch(nb2.base_url + "/kategorie/64-rada/artykuly/122-sklad-rady-kadencji-20242029", use_cache=False)
print("\n--- article 122 attachment url search ---")
for m in re.finditer(r'https://[^"\'\\ ]+?/api/attachments/\d+', h):
    print("ATT:", m.group(0))
for m in re.finditer(r'"documentRepositoryId":(\d+)', h):
    print("docRepoId:", m.group(1))
# doc id mapping
for m in re.finditer(r'api/attachments/(\d+)[^}]{0,120}', h):
    print("MAP:", m.group(0)[:200])
