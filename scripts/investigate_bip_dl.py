#!/usr/bin/env python3
"""Download Zlotow protokol XXXIII + check roll-call; councilor names via articles."""
import sys, re, json
from pathlib import Path
sys.path.insert(0, "/opt/data/workspace/radoskoppl/radoskop/scripts")
from lib_bip_net import NefeniRaport, download_pdf

SCRATCH = Path("/opt/data/workspace/radoskoppl/radoskop-premium/_investigate")
(SCRATCH / "pdf").mkdir(parents=True, exist_ok=True)

# ZLOTOW
nb = NefeniRaport("https://zlotow.bip.net.pl", debug=False)
print("=== ZLOTOW XXXIII protokol (article 2820) ===")
pdf_url, pdf_name = nb.attachment_for_article(
    nb.base_url + "/kategorie/46-protokoly-z-sesji-rady-miejskiej/artykuly/2820-protokol-z-xxxiii2026-z-xxxiii-sesji-rady-miejskiej-w-zlotowie-ix-kadencji-z-dnia-28-maja-2026-r")
print("PDF URL:", pdf_url, "| NAME:", pdf_name)
if pdf_url:
    p = download_pdf(nb._session, pdf_url, SCRATCH / "pdf")
    if p:
        magic = p.read_bytes()[:4]
        print("saved:", p, p.stat().st_size, "B, magic:", magic)
        if magic.startswith(b"%PDF"):
            import pdfplumber
            with pdfplumber.open(p) as pdf:
                txt = "\n".join((pg.extract_text() or "") for pg in pdf.pages)
            print("PAGES:", len(pdf.pages), "CHARS:", len(txt))
            print("--- grep 'imien' / 'Zalicz' / 'PRZECIW' ---")
            for kw in ["imien", "ZA", "PRZECIW", "Wstrzym", "Głosowan", "Glosowan"]:
                idx = txt.lower().find(kw.lower())
                print(f"  [{kw}] idx={idx}")
            # print a chunk around first 'imienne'
            i = txt.lower().find("imienne")
            print("\n--- sample around 'imienne' ---")
            print(txt[i-400:i+600] if i>=0 else txt[:600])

# Councilor names - kluby radnych article 111
print("\n\n=== ZLOTOW kluby radnych IX kadencji (article 111) ===")
u, n = nb.attachment_for_article(nb.base_url + "/kategorie/43-kluby-radnych-rady-miejskiej-w-zlotowie-/artykuly/111-kluby-radnych-ix-kadencji-rady-miejskiej-w-zlotowie-")
print("PDF:", u, "|", n)
if u:
    p = download_pdf(nb._session, u, SCRATCH / "pdf")
    if p and p.read_bytes()[:4].startswith(b"%PDF"):
        import pdfplumber
        with pdfplumber.open(p) as pdf:
            t2 = "\n".join((pg.extract_text() or "") for pg in pdf.pages)
        print(t2[:2000])
