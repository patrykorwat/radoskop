#!/usr/bin/env python3
"""Find real article attachment in the SSR flight payload."""
import sys, re
sys.path.insert(0, "/opt/data/workspace/radoskoppl/radoskop/scripts")
from lib_bip_net import NefeniRaport
nb = NefeniRaport("https://zlotow.bip.net.pl", debug=False)
url = nb.base_url + "/kategorie/46-protokoly-z-sesji-rady-miejskiej/artykuly/2820-protokol-z-xxxiii2026-z-xxxiii-sesji-rady-miejskiej-w-zlotowie-ix-kadencji-z-dnia-28-maja-2026-r"
html = nb.fetch(url, use_cache=False)
# print context around every api/attachments
for m in re.finditer(r'api/attachments/\d+', html):
    i = m.start()
    print("CTX:", html[i-160:i+60].replace('\n',' '))
    print("---")
