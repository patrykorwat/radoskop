#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Deep 2: praszka — uchwały/protokoły, tuszyn — HTTP fallback."""
import re
import requests
from bs4 import BeautifulSoup

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Radoskop/1.0 (info@radoskop.eu)",
      "Accept-Language": "pl,en;q=0.8"}

def get(url, **kw):
    return requests.get(url, headers=UA, timeout=30, verify=False, **kw)

import urllib3
urllib3.disable_warnings()

print("=== PRASZKA uchwały kategoria 933/886 ===")
r = get("https://bip.praszka.pl/933/886/uchwaly-rady-miejskiej.html")
print("HTTP", r.status_code, "len", len(r.text))
soup = BeautifulSoup(r.text, "html.parser")
for a in soup.find_all("a", href=True):
    t = re.sub(r"\s+", " ", a.get_text(" ", strip=True))
    h = a["href"]
    if re.search(r"protok|sesj|xvii|glosow|download", t + " " + h, re.I):
        print(f"  A: {t[:70]} || {h[:110]}")

print("\n=== PRASZKA kategoria protokoły (szukamsubmenu) ===")
r = get("https://bip.praszka.pl/")
soup = BeautifulSoup(r.text, "html.parser")
for a in soup.find_all("a", href=True):
    t = re.sub(r"\s+", " ", a.get_text(" ", strip=True))
    h = a["href"]
    if re.search(r"protok|sesj|rada", t + " " + h, re.I):
        print(f"  A: {t[:70]} || {h[:110]}")

print("\n=== TUSZYN http://tuszyn.pl ===")
try:
    r = get("http://tuszyn.pl/")
    print("HTTP", r.status_code, "len", len(r.text), "final", r.url[:90])
    soup = BeautifulSoup(r.text, "html.parser")
    t = soup.find("title")
    print("title", (t.text.strip()[:90] if t else "?"))
    for a in soup.find_all("a", href=True):
        txt = re.sub(r"\s+", " ", a.get_text(" ", strip=True))
        h = a["href"]
        if re.search(r"rada|radn|sesj|glosow|protok|bip", txt + " " + h, re.I):
            print(f"  A: {txt[:60]} || {h[:110]}")
except Exception as e:
    print("ERR", repr(e)[:130])

print("\n=== TUSZYN samorzad.gov.pl SSDIP ===")
for u in ("https://samorzad.gov.pl/web/gmina-tuszyn/rada-miejska",
          "https://samorzad.gov.pl/web/gmina-tuszyn"):
    try:
        r = get(u)
        print(u, "->", r.status_code, "len", len(r.text))
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, "html.parser")
            t = soup.find("title")
            print("  title", (t.text.strip()[:90] if t else "?"))
            for a in soup.find_all("a", href=True):
                txt = re.sub(r"\s+", " ", a.get_text(" ", strip=True))
                h = a["href"]
                if re.search(r"sesj|radn|skład|kadencj|komisj", txt + " " + h, re.I):
                    print(f"  A: {txt[:60]} || {h[:110]}")
    except Exception as e:
        print(u, "ERR", repr(e)[:110])
