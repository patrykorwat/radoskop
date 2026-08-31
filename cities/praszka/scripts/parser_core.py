#!/usr/bin/env python3
"""praszka final: v5-parsere-lineskip numbers as in-page breaks (18, 19, 20) and PRZECEW typo in 'protokol-i2024' — reconcile"""
import re, urllib.request, ssl, hashlib
from pathlib import Path
import fitz
CTX = ssl.create_default_context(); CTX.check_hostname=False; CTX.verify_mode=ssl.CERT_NONE
UA={"User-Agent":"Mozilla/5.0"}
BD = Path("tmp/cache_praszka")

NAME_RE = re.compile(r"^[A-ZŁŚŻŹÓ][\w\-_'’]+(?:\s+[A-ZŁŚŻŹÓ][\w\-_'’]+)+$")

def extract_names(seg, max_n=None):
    names = []
    cur = ""
    for tok in re.split(r",", seg):
        tok = tok.strip()
        for piece in tok.split("\n"):
            piece = piece.strip().strip(".").strip()
            if not piece or re.match(r"^\d{1,4}$", piece):   # page numbers
                continue
            cur = (cur + " " + piece).strip() if cur else piece
            if NAME_RE.match(cur):
                if cur not in names:
                    names.append(cur)
                cur = ""
            elif re.match(r"^[a-ząćęłńóśźż]{1,3}\s", cur):
                cur = ""
    return names[:max_n] if max_n else names

def parse5(text):
    votes = []
    vote_pat = re.compile(
        r"([^\n]{5,250}?)\s*\(?(\d{1,2}:\d{2})\)?\s*\n+"
        r"Wyniki imienne\s*[:.]?\s*\n(.*?)"
        r"(?=\n[^\n]{5,250}?\s*\(?(\d{1,2}:\d{2})\)?\s*\n+Wyniki imienne|\Z)",
        re.S)
    for m in vote_pat.finditer(text):
        topic, hour, content = m.group(1).strip(), m.group(2), m.group(3)
        named = {}
        for label, key in [("ZA", "za"), ("PRZECIW", "przeciw"), ("PRZECEW", "przeciw"),
                           ("WSTRZYMUJĘ SIĘ", "wstrzymal_sie"),
                           ("NIE GŁOSOWALI/NIEOBECNI", "nieobecni"), ("NIE GŁOSOWALI", "nieobecni"),
                           ("NIEOBECNI", "nieobecni")]:
            pm = re.search(rf"{label}\s*\(?(\d+)\)?\s*[:.]?\s*\n(.*?)(?=\n(?:PRZECIW|PRZECEW|WSTRZYMUJĘ|NIE GŁOSOWALI|NIEOBECNI)|\Z)", content, re.S)
            if pm and key not in named:
                nn = int(pm.group(1))
                seg2 = pm.group(2)
                names = extract_names(seg2, nn)
                named[key] = {"n": nn, "names": names}
        votes.append({"topic": f"{topic} ({hour})", "named": named})
    return votes

htmltxt = (BD/(hashlib.md5("https://bip.praszka.pl/6000/protokoly-z-posiedzen-rady-miejskiej-ix-kadencji-2024-2029.html".encode()).hexdigest()+".html")).read_text(encoding="utf-8", errors="ignore")
links = re.findall(r'href="(https://bip\.praszka\.pl/download/attachment/\d+/([^"?]+\.pdf)[^"]*)"', htmltxt)
seen=set(); pdfs=[]
for u,fn in links:
    if re.match(r"protokol-", fn) and u not in seen:
        seen.add(u); pdfs.append((u,fn))

tot=0; ok=0; bad=[]
for u,fn in pdfs:
    key=hashlib.md5(u.encode()).hexdigest()
    cf = BD/(key+".pdf")
    raw = cf.read_bytes() if cf.is_file() else None
    if raw is None:
        with urllib.request.urlopen(urllib.request.Request(u, headers=UA), timeout=60, context=CTX) as r:
            raw=r.read()
        cf.write_bytes(raw)
    doc = fitz.open(stream=raw, filetype="pdf")
    text = "\n".join(pg.get_text("text") for pg in doc)
    for v in parse5(text):
        tot += 1
        za = v["named"].get("za", {}); pr = v["named"].get("przeciw", {}); ws = v["named"].get("wstrzymal_sie", {}); nb = v["named"].get("nieobecni", {})
        zs = len(za.get("names",[])); ps = len(pr.get("names",[])); wss = len(ws.get("names",[])); nbs = len(nb.get("names",[]))
        if za.get("n",0)==zs and pr.get("n",0)==ps and ws.get("n",0)==wss and nb.get("n",0)==nbs:
            ok += 1
        else:
            bad.append((fn, v["topic"][:45], za.get("n"), zs, pr.get("n"), ps, ws.get("n"), wss, nb.get("n"), nbs))
print(f"votes: {tot}, OK: {ok}, BAD: {len(bad)}")
for b in bad[:15]: print("   ", b)
