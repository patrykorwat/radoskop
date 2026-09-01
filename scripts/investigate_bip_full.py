#!/usr/bin/env python3
"""Full protokoly list for Zlotow + Gozdnica councilor article + eSesja attachment."""
import sys, re, json
from pathlib import Path
sys.path.insert(0, "/opt/data/workspace/radoskoppl/radoskop/scripts")
from lib_bip_net import NefeniRaport

# ZLOTOW: all protokoly articles with pagination
nb = NefeniRaport("https://zlotow.bip.net.pl", debug=True)
CAT = "kategorie/46-protokoly-z-sesji-rady-miejskiej"
arts = nb.articles_in_category(CAT, require="Protokół")
# Some titles use 'Protokol' (no ó) - also catch
print("=== ZLOTOW Protokoly (full) ===")
dated = []
for a in arts:
    m = re.search(r"z dnia (\d{1,2})[\-.]([а-яa-ząćęłńóśźż]+)", a.title.lower())
    print(f"  {a.number:>5} | {a.date} | {a.title[:95]}")
    if a.date:
        dated.append((a.date, a.number, a.article_url, a.title))
dated.sort()
print(f"\nTotal protokoly articles: {len(arts)}")
if dated:
    print("Oldest:", dated[0][0], "| Latest:", dated[-1][0])

# GOZDNICA: skład rady article 122
nb2 = NefeniRaport("https://gozdnica.bip.net.pl", debug=True)
print("\n=== GOZDNICA skład rady kadencji 2024-2029 (article 122) ===")
html = nb2.fetch(nb2.base_url + "/kategorie/64-rada/artykuly/122-sklad-rady-kadencji-20242029", use_cache=False)
# get attachments
for m in re.finditer(r'"attachments":\[(.*?)\]', html, re.DOTALL):
    print("ATTACH JSON:", m.group(1)[:400])
# printable body text
body = re.sub(r'<script.*?</script>', ' ', html, flags=re.DOTALL)
body = re.sub(r'<[^>]+>', ' ', body)
body = re.sub(r'\s+', ' ', body)
i = body.lower().find('sklad rady')
print("BODYTEXT:", body[i:i+500] if i>=0 else body[:400])

# GOZDNICA eSesja article 129 attachments
print("\n=== GOZDNICA eSesja article 129 attachments ===")
html = nb2.fetch(nb2.base_url + "/kategorie/64-rada/artykuly/129-esesja-materialy-wideo-i-wyniki-glosowan", use_cache=False)
for m in re.finditer(r'"attachments":\[(\{.*?\})\],', html, re.DOTALL):
    print("ATT:", m.group(1)[:500])
