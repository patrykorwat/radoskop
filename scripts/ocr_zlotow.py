#!/usr/bin/env python3
"""OCR Zlotow PDFs: kluby (5075) and protokol XXXIII (7859) via fitz+tesseract."""
import fitz, subprocess, sys, os
from pathlib import Path

PD = Path("/opt/data/workspace/radoskoppl/radoskop-premium/_investigate")
TMP = PD / "ocr_tmp"; TMP.mkdir(exist_ok=True)
OCR = PD / "ocr"; OCR.mkdir(exist_ok=True)

def ocr_pdf(pdf_path, out_txt, max_pages=0, page_range=None):
    doc = fitz.open(pdf_path)
    pages = list(range(doc.page_count))
    if max_pages:
        pages = pages[:max_pages]
    if page_range:
        pages = page_range
    with open(out_txt, "w") as f:
        for i in pages:
            pix = doc[i].get_pixmap(dpi=200)
            png = TMP / f"p{i:03d}.png"
            pix.save(png)
            txt = subprocess.run(["tesseract", str(png), "stdout", "-l", "pol"],
                                 capture_output=True, text=True).stdout
            f.write(f"\n===== PAGE {i+1} =====\n{txt}\n")
            f.flush()
            print(f"page {i+1}/{len(pages)} done", flush=True)
    doc.close()

files = {
  "kluby": (PD/"pdf"/"49ea8f08257a6c4fe1d19f0e.pdf", str(OCR/"zlotow_kluby.txt")),
  "protokol": (PD/"pdf"/"a5da80471af0088a0a3c4864.pdf", str(OCR/"zlotow_protokol_xxxiii.txt")),
}
# OCR kluby full first
ocr_pdf(files["kluby"][0], files["kluby"][1])
print("KLUBY DONE")
