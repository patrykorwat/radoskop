#!/usr/bin/env python3
"""Pull councilor-list attachments (both cities) + Gozdnica eSesja content."""
import sys, re, json
from pathlib import Path
sys.path.insert(0, "/opt/data/workspace/radoskoppl/radoskop/scripts")
from lib_bip_net import NefeniRaport, download_pdf

SCRATCH = Path("/opt/data/workspace/radoskoppl/radoskop-premium/_investigate")
(SCRATCH / "pdf").mkdir(parents=True, exist_ok=True)

def get_attach_url(nb, article_url):
    html = nb.fetch(article_url, use_cache=False)
    m = re.search(r'https://[^"\'\\ ]+?/api/attachments/(\d+)', html)
    return m.group(0) if m else "", m.group(1) if m else ""

def extract_text(path):
    raw = path.read_bytes()[:4]
    if raw.startswith(b"%PDF"):
        try:
            import pdfplumber
            with pdfplumber.open(path) as pdf:
                return "\n".join((pg.extract_text() or "") for pg in pdf.pages)
        except Exception as e:
            return f"<pdf err {e}>"
    elif raw.startswith(b"PK"):
        try:
            import docx
            d = docx.Document(path)
            return "\n".join(p.text for p in d.paragraphs)
        except Exception as e:
            return f"<docx err {e}>"
    return "<other>"

# ZLOTOW kluby radnych (111)
nb = NefeniRaport("https://zlotow.bip.net.pl", debug=False)
print("=== ZLOTOW kluby radnych IX kadencji (111) ===")
u, aid = get_attach_url(nb, nb.base_url + "/kategorie/43-kluby-radnych-rady-miejskiej-w-zlotowie-/artykuly/111-kluby-radnych-ix-kadencji-rady-miejskiej-w-zlotowie-")
print("url:", u, "id:", aid)
p = download_pdf(nb._session, u, SCRATCH/"pdf") if u else None
if p:
    print("type magic:", p.read_bytes()[:4])
    print(extract_text(p)[:2500])

# GOZDNICA skład rady (122)
nb2 = NefeniRaport("https://gozdnica.bip.net.pl", debug=False)
print("\n\n=== GOZDNICA skład rady 2024-2029 (122) ===")
u2, aid2 = get_attach_url(nb2, nb2.base_url + "/kategorie/64-rada/artykuly/122-sklad-rady-kadencji-20242029")
print("url:", u2, "id:", aid2)
if u2:
    p2 = download_pdf(nb2._session, u2, SCRATCH/"pdf")
    if p2:
        print("magic:", p2.read_bytes()[:4])
        print(extract_text(p2)[:2500])

# GOZDNICA eSesja article 129 content + attachment
print("\n\n=== GOZDNICA eSesja article 129 ===")
h = nb2.fetch(nb2.base_url + "/kategorie/64-rada/artykuly/129-esesja-materialy-wideo-i-wyniki-glosowan", use_cache=False)
for m in re.finditer(r'"attachments":\[(.*?)\]\s*,\s*"', h, re.DOTALL):
    print("ATTACH JSON:", m.group(1)[:800])
# links to esesja
for m in set(re.findall(r'https?://[^"\'\\ ]*esesja[^"\'\\ ]*', h)):
    print("ESESJA LINK:", m)
