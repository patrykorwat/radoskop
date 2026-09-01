#!/usr/bin/env python3
"""OCR full Zlotow protokol XXXIII (7859) 72 pages -> text file."""
import fitz, subprocess
from pathlib import Path
PD = Path("/opt/data/workspace/radoskoppl/radoskop-premium/_investigate")
TMP = PD / "ocr_tmp"; TMP.mkdir(exist_ok=True)
OCR = PD / "ocr"; OCR.mkdir(exist_ok=True)
doc = fitz.open(PD/"pdf"/"a5da80471af0088a0a3c4864.pdf")
out = OCR/"zlotow_protokol_xxxiii.txt"
with open(out, "w") as f:
    for i in range(doc.page_count):
        pix = doc[i].get_pixmap(dpi=200)
        png = TMP/f"q{i:03d}.png"
        pix.save(png)
        txt = subprocess.run(["tesseract", str(png), "stdout", "-l", "pol"],
                             capture_output=True, text=True).stdout
        f.write(f"\n===== PAGE {i+1} =====\n{txt}\n"); f.flush()
doc.close()
print("OCR protokol done", out)
