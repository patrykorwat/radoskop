"""
Radoskop — Scrape & Download session protocol PDFs from BIP Gdańsk.

This script:
1. Scrapes BIP protocol listing pages for all years (both kadencje)
2. Extracts download URLs for protocol PDFs
3. Downloads them to the protokoly/ directory

Usage (run locally, not in sandbox):
    pip install requests beautifulsoup4
    python scripts/scrape_protokoly.py
"""

import requests
from bs4 import BeautifulSoup
import re
import os
import time
import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
PROTOKOLY_DIR = BASE_DIR / "protokoly"
PROTOKOLY_DIR.mkdir(exist_ok=True)

# BIP listing pages for session protocols by year
BIP_LISTING_PAGES = {
    # IX kadencja (2024-2029)
    2026: "https://bip.gdansk.pl/rada-miasta-gdanska/Protokoly-z-sesji-z-2026-r-audio-wideo,a,304531",
    2025: "https://bip.gdansk.pl/rada-miasta-gdanska/Protokoly-z-sesji-z-2025-r-audio-wideo,a,279560",
    2024: "https://bip.gdansk.pl/rada-miasta-gdanska/Protokoly-z-sesji-z-2024-r-audio-wideo,a,257107",
    # VIII kadencja (2018-2024)
    2023: "https://bip.gdansk.pl/rada-miasta-gdanska/Protokoly-z-sesji-z-2023-r-audio-wideo,a,234487",
    2022: "https://bip.gdansk.pl/rada-miasta/Protokoly-z-sesji-z-2022-r-audio-wideo,a,210434",
    2021: "https://bip.gdansk.pl/rada-miasta/Protokoly-z-sesji-z-2021-r-audio-wideo,a,185757",
    2020: "https://bip.gdansk.pl/rada-miasta/Protokoly-z-sesji-z-2020-r-audio-wideo,a,153543",
    2019: "https://bip.gdansk.pl/rada-miasta/Protokoly-z-sesji-z-2019-r-audio-wideo,a,124052",
    2018: "https://bip.gdansk.pl/rada-miasta/Protokoly-z-sesji-z-2018-r-audio-wideo,a,108171",
}

# Known protocol URLs from web search (fallback if scraping fails)
KNOWN_URLS = {
    "XLVII/2018": "https://download.cloudgdansk.pl/gdansk-pl/d/201805107878/protokol-xlvii-2018_01-25.pdf",
    "XLVIII/2018": "https://download.cloudgdansk.pl/gdansk-pl/d/201810116346/protokol-nr-xlviii.pdf",
    "XLIX/2018": "https://download.cloudgdansk.pl/gdansk-pl/d/201808112564/protokol-xlix-2018_03-02.pdf",
    "L/2018": "https://download.cloudgdansk.pl/gdansk-pl/d/201808112563/protokol-l-2018_03-29.pdf",
    "LI/2018": "https://download.cloudgdansk.pl/gdansk-pl/d/201808113150/protokol-li-2018_04_23.pdf",
    "LII/2018": "https://download.cloudgdansk.pl/gdansk-pl/d/201810115634/protokol-lii-2018_04-23.pdf",
    "LIII/2018": "https://download.cloudgdansk.pl/gdansk-pl/d/201810115431/protokol-liii-2018_05_24.pdf",
    "LV/2018": "https://download.cloudgdansk.pl/gdansk-pl/d/201905128661/protokol-nr-lv-czerwiec-2018.pdf",
    "LVI/2018": "https://download.cloudgdansk.pl/gdansk-pl/d/201810116361/protokol-lvi-2018_08_30.pdf",
    "LVII/2018": "https://download.cloudgdansk.pl/gdansk-pl/d/201905128058/protokol-lvii-2018_09_27-skonwertowany.pdf",
    "LVIIII/2018": "https://download.cloudgdansk.pl/gdansk-pl/d/201901120380/protokol-lviiii-2018_10_25.pdf",
    "LIX/2018": "https://download.cloudgdansk.pl/gdansk-pl/d/201901120379/protokol-lix-2018_11_08.pdf",
    "I/2018": "https://download.cloudgdansk.pl/gdansk-pl/d/201908133892/protokol-i-2018_11_19.pdf",
    "II/2018": "https://download.cloudgdansk.pl/gdansk-pl/d/201908133891/protokol-nr-ii-2018-11-22.pdf",
    "III/2018": "https://download.cloudgdansk.pl/gdansk-pl/d/201908133721/protokol-iii-sesja-rmg-11-grudnia-2018.pdf",
    "V/2019": "https://download.cloudgdansk.pl/gdansk-pl/d/201906130030/protokol-v-2019_01_31.pdf",
    "VI/2019": "https://download.cloudgdansk.pl/gdansk-pl/d/201908133893/protokol-vi-2019_03_07.pdf",
    "VIII/2019": "https://download.cloudgdansk.pl/gdansk-pl/d/201909134505/protokol-viii-2019_03_28.pdf",
    "25-04-2019/2019": "https://download.cloudgdansk.pl/gdansk-pl/d/202001141677/sesja-protokol-25-04-2019.pdf",
    "XI/2019": "https://download.cloudgdansk.pl/gdansk-pl/d/202001141467/protokol-nr-xi.pdf",
    "XII/2019": "https://download.cloudgdansk.pl/gdansk-pl/d/202009156135/protokol-nr-xii-227-czerwca-2019-rok.pdf",
    "29-08-2019/2019": "https://download.cloudgdansk.pl/gdansk-pl/d/202003145942/protokol-z-29-08-2019-xiii-sesja.pdf",
    "protokol-z-sesji-ix-2019/2019": "https://download.cloudgdansk.pl/gdansk-pl/d/202101163261/protokol-z-sesji-ix-2019.pdf",
    "XV/2019": "https://download.cloudgdansk.pl/gdansk-pl/d/202001142468/protokol-nr-xv.pdf",
    "XVII/2019": "https://download.cloudgdansk.pl/gdansk-pl/d/202108174827/protokol-xvii-28-11-2019.pdf",
    "XVIII/2019": "https://download.cloudgdansk.pl/gdansk-pl/d/202009156360/protokol-xviii-sesja-rmg-19_12_2020.pdf",
    "30-01-2020/2020": "https://download.cloudgdansk.pl/gdansk-pl/d/202211199376/protokol-30-01-2020.pdf",
    "XX/2020": "https://download.cloudgdansk.pl/gdansk-pl/d/202011160882/protokol-nr-xx-sesja-27_02_2020.pdf",
    "30-04-2020/2020": "https://download.cloudgdansk.pl/gdansk-pl/d/202203186522/protokol-30-04-2020.pdf",
    "28-05-2020/2020": "https://download.cloudgdansk.pl/gdansk-pl/d/202111180142/protokol-28-05-2020.pdf",
    "XXIV/2020": "https://download.cloudgdansk.pl/gdansk-pl/d/202010157676/protokol-nr-xxiv.pdf",
    "16-07-2020/2020": "https://download.cloudgdansk.pl/gdansk-pl/d/202209194822/protokol-16-07-2020.pdf",
    "27-08-2020/2020": "https://download.cloudgdansk.pl/gdansk-pl/d/202011159200/protokol-z-sesji-rmg-27-08-2020.pdf",
    "XXVIII/2020": "https://download.cloudgdansk.pl/gdansk-pl/d/202011159195/protokol-nr-xxviii.pdf",
    "XXIX/2020": "https://download.cloudgdansk.pl/gdansk-pl/d/202101163655/protokol-nr-xxix.pdf",
    "XXX/2020": "https://download.cloudgdansk.pl/gdansk-pl/d/202106171305/protokol-nr-xxx-z-sesji-rmg.pdf",
    "protokol-z-esesji/2020": "https://download.cloudgdansk.pl/gdansk-pl/d/202012162271/protokol-z-esesji.pdf",
    "XXXI/2020": "https://download.cloudgdansk.pl/gdansk-pl/d/202106171781/protokol-nr-xxxi.pdf",
    "17-12-2020/2020": "https://download.cloudgdansk.pl/gdansk-pl/d/202012162269/protokol-e-sesja-17-12-2020.pdf",
    "XXXII/2021": "https://download.cloudgdansk.pl/gdansk-pl/d/202104168244/protokol-xxxii-sesja-rmg.pdf",
    "XXXIII/2021": "https://download.cloudgdansk.pl/gdansk-pl/d/202111179456/protokol-xxxiii-sesji-rmg.pdf",
    "protokol-z-numerami-uchwal/2021": "https://download.cloudgdansk.pl/gdansk-pl/d/202104167344/protokol-z-numerami-uchwal.pdf",
    "XXXIV/2021": "https://download.cloudgdansk.pl/gdansk-pl/d/202104168498/protokol-nr-xxxiv.pdf",
    "XXXV/2021": "https://download.cloudgdansk.pl/gdansk-pl/d/202110178268/protokol-xxxv-z-sesji-rmg.pdf",
    "protokol-esesja-doc/2021": "https://download.cloudgdansk.pl/gdansk-pl/d/202105168843/protokol-esesja-doc.pdf",
    "XXXVI/2021": "https://download.cloudgdansk.pl/gdansk-pl/d/202108174704/protokol-xxxvi-sesji-rmg.pdf",
    "24-06-2021/2021": "https://download.cloudgdansk.pl/gdansk-pl/d/202209194824/protokol-24-06-2021.pdf",
    "XXXVIII/2021": "https://download.cloudgdansk.pl/gdansk-pl/d/202210196826/protokol-xxxviii-sesji-rmg.pdf",
    "XXXIX/2021": "https://download.cloudgdansk.pl/gdansk-pl/d/202208193436/protokol-xxxix-sesji-rmg.pdf",
    "XL/2021": "https://download.cloudgdansk.pl/gdansk-pl/d/202111179719/protokol-xl-sesji-rmg.pdf",
    "XLI/2021": "https://download.cloudgdansk.pl/gdansk-pl/d/202112181442/protokol-xli-sesji-rmg.pdf",
    "XLIV/2022": "https://download.cloudgdansk.pl/gdansk-pl/d/202210197491/protokol-xliv-sesji-rmg.pdf",
    "XLV/2022": "https://download.cloudgdansk.pl/gdansk-pl/d/202204186880/protokol-xlv-sesji-rmg.pdf",
    "XLVI/2022": "https://download.cloudgdansk.pl/gdansk-pl/d/202203185668/protokol-xlvi-sesji-rmg.pdf",
    "XLVII/2022": "https://download.cloudgdansk.pl/gdansk-pl/d/202309217517/protokol-xlvii-sesji-rmg.pdf",
    "XLVIII/2022": "https://download.cloudgdansk.pl/gdansk-pl/d/202303204852/protokol-xlviii-sesji-rmg.pdf",
    "XLIX/2022": "https://download.cloudgdansk.pl/gdansk-pl/d/202303204851/protokol-xlix-sesji-rmg.pdf",
    "L/2022": "https://download.cloudgdansk.pl/gdansk-pl/d/202304207243/protokol-l-sesji-rmg.pdf",
    "LI/2022": "https://download.cloudgdansk.pl/gdansk-pl/d/202308215127/protokol-li-sesji-rmg.pdf",
    "LII/2022": "https://download.cloudgdansk.pl/gdansk-pl/d/202402225903/protokol-lii-sesji-rmg.pdf",
    "LIII/2022": "https://download.cloudgdansk.pl/gdansk-pl/d/202309217516/protokol-liii-sesji-rmg.pdf",
    "LIV/2022": "https://download.cloudgdansk.pl/gdansk-pl/d/202402225902/protokol-liv-sesji-rmg.pdf",
    "LV/2022": "https://download.cloudgdansk.pl/gdansk-pl/d/202402225500/protokol-lv-sesji-rmg.pdf",
    "LVI/2022": "https://download.cloudgdansk.pl/gdansk-pl/d/202402225499/protokol-lvi-sesji-rmg.pdf",
    "LVII/2022": "https://download.cloudgdansk.pl/gdansk-pl/d/202404228466/protokol-lvii-z-sesji-rmg.pdf",
    "LIX/2023": "https://download.cloudgdansk.pl/gdansk-pl/d/202304207248/protokol-lix-sesji-rmg.pdf",
    "LX/2023": "https://download.cloudgdansk.pl/gdansk-pl/d/202308215206/protokol-lx-sesji-rmg.pdf",
    "LXI/2023": "https://download.cloudgdansk.pl/gdansk-pl/d/202402225498/protokol-lxi-sesji-rmg.pdf",
    "LXII/2023": "https://download.cloudgdansk.pl/gdansk-pl/d/202403226835/protokol-lxii-sesji-rmg.pdf",
    "LXIII/2023": "https://download.cloudgdansk.pl/gdansk-pl/d/202308215129/protokol-lxiii-sesji-rmg.pdf",
    "LXIV/2023": "https://download.cloudgdansk.pl/gdansk-pl/d/202309216516/protokol-lxiv-sesji-rmg.pdf",
    "protokol-22-czerwca-2023r/2023": "https://download.cloudgdansk.pl/gdansk-pl/d/202312222373/protokol-22-czerwca-2023r.pdf",
    "LXVI/2023": "https://download.cloudgdansk.pl/gdansk-pl/d/202402225556/protokol-lxvi-sesji-rmg.pdf",
    "LXVII/2023": "https://download.cloudgdansk.pl/gdansk-pl/d/202403227557/protokol-lxvii-sesji-rmg.pdf",
    "LXVIII/2023": "https://download.cloudgdansk.pl/gdansk-pl/d/202403226836/protokol-lxviii-sesji-rmg.pdf",
    "LXIX/2023": "https://download.cloudgdansk.pl/gdansk-pl/d/202403226691/protokol-lxix-sesji-rmg.pdf",
    "LXX/2023": "https://download.cloudgdansk.pl/gdansk-pl/d/202404229708/protokol-lxx-sesji-rmg.pdf",
    "LXXI/2023": "https://download.cloudgdansk.pl/gdansk-pl/d/202403227558/protokol-lxxi-sesji-rmg.pdf",
    "LXXII/2024": "https://download.cloudgdansk.pl/gdansk-pl/d/202402225501/protokol-lxxii-sesji-rmg.pdf",
    "LXXIII/2024": "https://download.cloudgdansk.pl/gdansk-pl/d/202403226811/protokol-lxxiii-sesji-rmg.pdf",
    "LXXIV/2024": "https://download.cloudgdansk.pl/gdansk-pl/d/202404228464/protokol-lxxiv-sesji-rmg.pdf",
    "LXXV/2024": "https://download.cloudgdansk.pl/gdansk-pl/d/202404228465/protokol-lxxv-sesji-rmg.pdf",
    "LXXVI/2024": "https://download.cloudgdansk.pl/gdansk-pl/d/202405231167/protokol-lxxvi-sesji-rmg.pdf",
    "I/2024": "https://download.cloudgdansk.pl/gdansk-pl/d/202408234840/protokol-i-sesji-rmg.pdf",
    "II/2024": "https://download.cloudgdansk.pl/gdansk-pl/d/202406232452/protokol-ii-sesji-rmg.pdf",
    "III/2024": "https://download.cloudgdansk.pl/gdansk-pl/d/202407234148/protokol-iii-sesji-rmg.pdf",
    "IV/2024": "https://download.cloudgdansk.pl/gdansk-pl/d/202410238991/protokol-iv-sesji-rmg.pdf",
    "V/2024": "https://download.cloudgdansk.pl/gdansk-pl/d/202501244707/protokol-v-sesji-rmg.pdf",
    "VI/2024": "https://download.cloudgdansk.pl/gdansk-pl/d/202410239368/protokol-z-vi-sesji-rmg.pdf",
    "VII/2024": "https://download.cloudgdansk.pl/gdansk-pl/d/202410239069/protokol-vii-sesji-rmg.pdf",
    "VIII/2024": "https://download.cloudgdansk.pl/gdansk-pl/d/202412243117/protokol-viii-sesji-rmg.pdf",
    "IX/2024": "https://download.cloudgdansk.pl/gdansk-pl/d/202501244070/protokol-ix-sesji-rmg.pdf",
    "X/2024": "https://download.cloudgdansk.pl/gdansk-pl/d/202501244702/protokol-x-sesji-rmg.pdf",
    "XI/2024": "https://download.cloudgdansk.pl/gdansk-pl/d/202501244701/protokol-xi-sesji-rmg.pdf",
    "XII/2025": "https://download.cloudgdansk.pl/gdansk-pl/d/202505253413/protokol-xii-sesja-rmg.pdf",
    "XIII/2025": "https://download.cloudgdansk.pl/gdansk-pl/d/202506254701/protokol-xiii-sesji-rmg.pdf",
    "XIV/2025": "https://download.cloudgdansk.pl/gdansk-pl/d/202508259994/protokol-xiv-sesji-rmg.pdf",
    "XV/2025": "https://download.cloudgdansk.pl/gdansk-pl/d/202508258945/protokol-xv-sesji-rmg.pdf",
    "XVI/2025": "https://download.cloudgdansk.pl/gdansk-pl/d/202509261377/protokol-z-xvi-sesji-rmg.pdf",
    "XVII/2025": "https://download.cloudgdansk.pl/gdansk-pl/d/202601268442/protokol-xvii-sesji-rmg.pdf",
    "XVIII/2025": "https://download.cloudgdansk.pl/gdansk-pl/d/202511265255/protokol-z-xviii-sesji-rmg.pdf",
    "XIX/2025": "https://download.cloudgdansk.pl/gdansk-pl/d/202601268444/protokol-xix-sesji-rmg.pdf",
    "XX/2025": "https://download.cloudgdansk.pl/gdansk-pl/d/202601267660/protokol-xx-sesji-rmg.pdf",
    "XXI/2025": "https://download.cloudgdansk.pl/gdansk-pl/d/202601268438/protokol-z-xxi-sesji-rmg.pdf",
    "XXII/2025": "https://download.cloudgdansk.pl/gdansk-pl/d/202602269547/protokol-z-xxii-sesji-rmg.pdf",
    "XXIII/2026": "https://download.cloudgdansk.pl/gdansk-pl/d/202604272884/protokol-xxiii-sesji-rmg.pdf",
    "XXIV/2026": "https://download.cloudgdansk.pl/gdansk-pl/d/202604272885/protokol-xxiv-sesji-rmg.pdf",
}


def scrape_bip_page(url, year):
    """Scrape a BIP protocol listing page for protocol PDF download URLs."""
    print(f"  Scraping {url} ...")
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        print(f"  FAILED: {e}")
        return {}

    soup = BeautifulSoup(resp.text, "html.parser")
    protocols = {}

    # Look for links containing 'protokol' and pointing to cloudgdansk.pl
    for a in soup.find_all("a", href=True):
        href = a["href"]
        text = a.get_text(strip=True).lower()

        # Match protocol PDF links
        if "protokol" in text or "protokół" in text:
            # Resolve relative URLs
            if href.startswith("/"):
                href = "https://bip.gdansk.pl" + href

            # If it's a direct cloudgdansk download link
            if "download.cloudgdansk.pl" in href and href.endswith(".pdf"):
                # Extract session number from link text or URL
                num_match = re.search(r'([IVXLCDM]+)\s+sesji', text, re.IGNORECASE)
                if num_match:
                    key = f"{num_match.group(1).upper()}/{year}"
                else:
                    # Try from URL
                    url_match = re.search(r'protokol[_-]([ivxlcdm]+)', href, re.IGNORECASE)
                    if url_match:
                        key = f"{url_match.group(1).upper()}/{year}"
                    else:
                        key = f"unknown-{len(protocols)}/{year}"
                protocols[key] = href

            # If it's a BIP metryczka/detail page, follow it to find the download URL
            elif "bip.gdansk.pl" in href or href.startswith("/"):
                # Could be an intermediate page — try to follow
                try:
                    detail = requests.get(href if href.startswith("http") else "https://bip.gdansk.pl" + href, timeout=15)
                    detail_soup = BeautifulSoup(detail.text, "html.parser")
                    for da in detail_soup.find_all("a", href=True):
                        dh = da["href"]
                        if "download.cloudgdansk.pl" in dh and "protokol" in dh.lower() and dh.endswith(".pdf"):
                            num_match = re.search(r'([IVXLCDM]+)\s+sesji', text, re.IGNORECASE)
                            key = f"{num_match.group(1).upper()}/{year}" if num_match else f"scraped-{len(protocols)}/{year}"
                            protocols[key] = dh
                            break
                    time.sleep(0.5)
                except Exception:
                    pass

    # Also search for cloudgdansk links anywhere in the page
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "download.cloudgdansk.pl" in href and "protokol" in href.lower() and href.endswith(".pdf"):
            # Extract session info from URL
            url_match = re.search(r'protokol[_-](?:z[_-])?([ivxlcdm]+)', href, re.IGNORECASE)
            if url_match:
                key = f"{url_match.group(1).upper()}/{year}"
                if key not in protocols:
                    protocols[key] = href

    return protocols


def discover_all_protocols():
    """Discover all protocol URLs from BIP listing pages + known URLs."""
    all_protocols = dict(KNOWN_URLS)

    print("Scraping BIP listing pages for protocol URLs...")
    for year, url in sorted(BIP_LISTING_PAGES.items()):
        print(f"\n[{year}]")
        scraped = scrape_bip_page(url, year)
        for key, dl_url in scraped.items():
            if key not in all_protocols:
                all_protocols[key] = dl_url
                print(f"  Found: {key} -> {dl_url}")
            else:
                print(f"  Already known: {key}")
        time.sleep(1)

    print(f"\nTotal protocols discovered: {len(all_protocols)}")
    return all_protocols


def download_protocols(protocols):
    """Download all protocol PDFs to protokoly/ directory."""
    print(f"\nDownloading {len(protocols)} protocols to {PROTOKOLY_DIR}/...")

    downloaded = 0
    skipped = 0
    failed = 0

    for key, url in sorted(protocols.items()):
        # Create filename from key and URL
        session_num = key.replace("/", "-")
        url_filename = url.split("/")[-1]
        filename = f"{session_num}_{url_filename}"
        filepath = PROTOKOLY_DIR / filename

        if filepath.exists() and filepath.stat().st_size > 1000:
            print(f"  Skipping (exists): {filename}")
            skipped += 1
            continue

        print(f"  Downloading: {key} -> {filename} ... ", end="")
        try:
            resp = requests.get(url, timeout=60)
            resp.raise_for_status()
            if resp.headers.get("content-type", "").startswith("application/pdf") or len(resp.content) > 5000:
                filepath.write_bytes(resp.content)
                size_kb = len(resp.content) / 1024
                print(f"OK ({size_kb:.0f} KB)")
                downloaded += 1
            else:
                print(f"SKIP (not a PDF, {len(resp.content)} bytes)")
                failed += 1
        except Exception as e:
            print(f"FAILED: {e}")
            failed += 1

        time.sleep(0.5)

    print(f"\nDone: {downloaded} downloaded, {skipped} skipped, {failed} failed")

    # Save URL index for reference
    index_path = PROTOKOLY_DIR / "protocol_urls.json"
    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(protocols, f, ensure_ascii=False, indent=2)
    print(f"URL index saved to {index_path}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Scrape & download RMG session protocols from BIP")
    parser.add_argument("--scrape-only", action="store_true", help="Only discover URLs, don't download")
    parser.add_argument("--known-only", action="store_true", help="Only use known URLs, skip BIP scraping")
    parser.add_argument("--out", default=None,
                        help="Katalog docelowy PDF-ów (domyślnie protokoly/ w repo). "
                             "NAS używa scratch poza repo, żeby PDF nie trafiały na S3.")
    args = parser.parse_args()

    # Override katalogu wyjściowego (NAS scratch). Reszta kodu używa PROTOKOLY_DIR.
    if args.out:
        PROTOKOLY_DIR = Path(args.out)
        PROTOKOLY_DIR.mkdir(parents=True, exist_ok=True)

    if args.known_only:
        protocols = KNOWN_URLS
        print(f"Using {len(protocols)} known protocol URLs (skipping BIP scraping)")
    else:
        protocols = discover_all_protocols()

    if args.scrape_only:
        # Just save the URL index
        index_path = PROTOKOLY_DIR / "protocol_urls.json"
        with open(index_path, "w", encoding="utf-8") as f:
            json.dump(protocols, f, ensure_ascii=False, indent=2)
        print(f"URL index saved to {index_path}")
    else:
        download_protocols(protocols)
