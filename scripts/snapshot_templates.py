#!/usr/bin/env python3
"""Golden-file harness dla template/index.html.

Renderuje index.html dla reprezentatywnego zestawu miast/sejmików (po jednym na
locale + faction + sejmik) przez właściwy generator i liczy sha256. Refactor
template (wyniesienie CSS/head/JS do plików) NIE powinien zmienić outputu —
`--check` musi dać zielono. Jeśli czerwono, lista miast z różnicą = dryf do debug.

  python3 scripts/snapshot_templates.py            # zapisz baseline
  python3 scripts/snapshot_templates.py --check     # porównaj z baseline
  python3 scripts/snapshot_templates.py --all       # pełne pokrycie (wszystkie nie-disabled)

Refactor strukturalny template jest data-niezależny, więc próbka per-locale
wystarcza do wykrycia dryfu. --all dla pewności przed dużym krokiem.
"""
import argparse
import glob
import hashlib
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
BASELINE = os.path.join(REPO, "docs", "_template_snapshots.json")

# Reprezentatywna próbka: każdy locale + faction + miasto/sejmik (dwa generatory).
SAMPLE_CITIES = [
    "katowice", "wroclaw",            # pl
    "berlin", "schwerin",             # de
    "praha",                          # cs
    "amsterdam",                      # nl
    "bratislava",                     # sk
    "budapest",                       # hu
    "vilnius",                        # lt
    "riga",                           # lv
    "tallinn",                        # et
    "kyiv",                           # uk
    "copenhagen",                     # da (faction)
    "paris",                          # fr (faction)
]
SAMPLE_ASSEMBLIES = ["mazowieckie", "landtag-mv"]


def render(generator, config, out_dir):
    """Uruchom generator, zwróć (sha256, err) dla out_dir/index.html."""
    cmd = ["python3", os.path.join(HERE, generator), "--config", config, "--output", out_dir]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return None, "timeout"
    idx = os.path.join(out_dir, "index.html")
    if not os.path.exists(idx):
        return None, (r.stderr.strip().splitlines() or ["no index.html"])[-1]
    h = hashlib.sha256(open(idx, "rb").read()).hexdigest()
    return h, None


def targets(all_mode):
    out = []
    if all_mode:
        for cf in sorted(glob.glob(os.path.join(REPO, "cities", "*", "config.json"))):
            slug = os.path.basename(os.path.dirname(cf))
            if not json.load(open(cf)).get("disabled"):
                out.append(("city", slug, "generate_site.py", cf))
        for cf in sorted(glob.glob(os.path.join(REPO, "assemblies", "*", "config.json"))):
            slug = os.path.basename(os.path.dirname(cf))
            if not json.load(open(cf)).get("disabled"):
                out.append(("assembly", slug, "generate_assembly_site.py", cf))
    else:
        for slug in SAMPLE_CITIES:
            cf = os.path.join(REPO, "cities", slug, "config.json")
            if os.path.exists(cf):
                out.append(("city", slug, "generate_site.py", cf))
        for slug in SAMPLE_ASSEMBLIES:
            cf = os.path.join(REPO, "assemblies", slug, "config.json")
            if os.path.exists(cf):
                out.append(("assembly", slug, "generate_assembly_site.py", cf))
    return out


def build(all_mode):
    res = {}
    with tempfile.TemporaryDirectory() as tmp:
        for kind, slug, gen, cf in targets(all_mode):
            out_dir = os.path.join(tmp, slug)
            os.makedirs(out_dir, exist_ok=True)
            h, err = render(gen, cf, out_dir)
            res[slug] = {"kind": kind, "sha256": h, "error": err}
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="porównaj z baseline zamiast zapisywać")
    ap.add_argument("--all", action="store_true", help="pełne pokrycie (wszystkie nie-disabled)")
    args = ap.parse_args()

    current = build(args.all)
    errs = {s: v["error"] for s, v in current.items() if v["error"]}

    if not args.check:
        os.makedirs(os.path.dirname(BASELINE), exist_ok=True)
        json.dump(current, open(BASELINE, "w"), indent=2, sort_keys=True)
        ok = sum(1 for v in current.values() if v["sha256"])
        print(f"BASELINE zapisany: {ok}/{len(current)} renderów OK -> {BASELINE}")
        for s, e in errs.items():
            print(f"  [render-error] {s}: {e}")
        return 0

    if not os.path.exists(BASELINE):
        print("BRAK baseline — uruchom bez --check najpierw."); return 2
    base = json.load(open(BASELINE))
    drift, missing = [], []
    for slug, v in current.items():
        b = base.get(slug)
        if not b:
            missing.append(slug); continue
        if v["sha256"] != b["sha256"]:
            drift.append((slug, b["sha256"], v["sha256"], v["error"]))
    if errs:
        for s, e in errs.items():
            print(f"  [render-error] {s}: {e}")
    if drift:
        print(f"❌ DRYF w {len(drift)} miastach (output != baseline):")
        for slug, b, c, e in drift:
            print(f"  {slug}: {b[:12]} -> {c[:12]}" + (f"  ({e})" if e else ""))
        return 1
    if missing:
        print(f"⚠ {len(missing)} nowych celów bez baseline: {', '.join(missing)}")
    print(f"✅ ZIELONO: {len(current)} renderów identycznych z baseline.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
