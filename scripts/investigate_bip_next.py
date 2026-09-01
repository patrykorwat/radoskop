#!/usr/bin/env python3
"""Inspect Zlotow article page scripts to find real attachment + content API."""
import sys, re, json
sys.path.insert(0, "/opt/data/workspace/radoskoppl/radoskop/scripts")
from lib_bip_net import NefeniRaport

nb = NefeniRaport("https://zlotow.bip.net.pl", debug=False)
url = nb.base_url + "/kategorie/46-protokoly-z-sesji-rady-miejskiej/artykuly/2820-protokol-z-xxxiii2026-z-xxxiii-sesji-rady-miejskiej-w-zlotowie-ix-kadencji-z-dnia-28-maja-2026-r"
html = nb.fetch(url, use_cache=False)

# find __NEXT_DATA__ or any JSON with attachments
for m in re.finditer(r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL):
    print("NEXT_DATA len:", len(m.group(1)))
    d = json.loads(m.group(1))
    print("keys:", list(d.keys()))

# search for '7859' and surrounding - maybe more attachment ids
print("occurrences of 'attachments':", html.count('attachments'))
# find the article content rendered - look for 'Protokół' body text
for m in re.finditer(r'\.pdf', html):
    pass

# Search all numeric ids near "attachment" 
for m in re.finditer(r'attach\w*"?:\s*\[([^\]]{0,500})', html, re.DOTALL):
    print("ATTACHARR:", m.group(1)[:400])

# Look for api host and any REST api paths
for m in set(re.findall(r'/(?:api|_next)[^"\'\\\s]{0,80}', html)):
    if any(x in m for x in ['attach','article','content','kategorie']):
        print("API path:", m)
print("\n--- searching for article content in script flight data ---")
# RSC flight data often contains \u escaped. Search literal 'Protok' chunk
i = html.find('XXXIII')
print("XXXIII idx:", i)
# find a big JSON blob
for m in re.finditer(r'"articleId":\d+', html):
    print("articleId:", m.group(0))
