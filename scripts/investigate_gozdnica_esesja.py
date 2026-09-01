#!/usr/bin/env python3
"""Probe Gozdnica eSesja portal freshness (/glosowania)."""
import sys, re
sys.path.insert(0, "/opt/data/workspace/radoskoppl/radoskop/scripts")
import requests
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
for url in ["https://gozdnica.esesja.pl/glosowania", "https://gozdnica.esesja.pl/"]:
    try:
        r = requests.get(url, headers={"User-Agent":UA}, timeout=40)
        print("=== ", url, r.status_code, len(r.text))
        # look for session titles/dates
        txt = re.sub(r'<[^>]+>', ' ', r.text)
        txt = re.sub(r'\s+', ' ', txt)
        for m in re.finditer(r'(Sesja[^A-Z]{0,60})', txt):
            print("   SES:", m.group(1).strip()[:80])
        for m in re.finditer(r'(\d{1,2}\s+(?:stycznia|lutego|marca|kwietnia|maja|czerwca|lipca|sierpnia|września|października|listopada|grudnia)\s+20\d\d)', txt):
            print("   DATE:", m.group(1))
    except Exception as e:
        print("ERR", url, e)
