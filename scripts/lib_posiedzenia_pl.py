#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Radoskop adapter: portal-posiedzenia.pl (posiedzenia.pl / "System Posiedzenia").

Miasta (zweryfikowane): nowasol (2026-09), kwidzyn (2026-09).
Platforma: {subdomena}.posiedzenia.pl osadza iframe portal-posiedzenia.pl/{sub}.
Sesja anonima: GET portal-posiedzenia.pl/{sub}?ignoreIFrame=true&action=glosowania
ustawia ciastko PHPSESSID_{sub}; potem:
  GET /admin/start.php?ScreenSize=1920_1080            -> danejson.podmiot
  GET /admin/zawartosc.php?action=O3&podmiot=N...      -> JSON list.sessions[].points[]
  GET /admin/zawartosc.php?action=O7&parametr={punktId} -> HTML + <chartScores src='base64'>
     base64 JSON: persons[{firstName,lastName,partia,present,voted,votes:[k]}],
     options{o1:'Za',o2:'Przeciw',o3:'Wstrzymane',notVoting:'Brak głosu',absent:'Nieobecni'}
     votes [1]=ZA [2]=PRZECIW [3]=WSTRZYMAŁ; present=false => nieobecny.
Kod głosów weryfikowany krzyżowo z tabelą HTML "Wyniki imienne" (ZA/PRZECIW/WSTRZYMAŁ(A/SIĘ)).
"""
import base64
import http.cookiejar
import json
import re
import time
import urllib.request
from collections import defaultdict

PORTAL = "https://portal-posiedzenia.pl"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
REQ_DELAY = 0.45
_LAST = 0.0


def _rate():
    global _LAST
    d = time.time() - _LAST
    if d < REQ_DELAY:
        time.sleep(REQ_DELAY - d)
    _LAST = time.time()


class PosiedzeniaClient:
    def __init__(self, sub):
        self.sub = sub
        self.cj = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cj))
        self.op.addheaders = [("User-Agent", UA),
                              ("Referer", f"{PORTAL}/{sub}?action=glosowania")]
        self.podmiot = None
        self.nazwa = None

    def login(self):
        _rate()
        self.op.open(f"{PORTAL}/{self.sub}?ignoreIFrame=true&action=glosowania",
                     timeout=30).read()
        _rate()
        r = self.op.open(PORTAL + "/admin/start.php?ScreenSize=1920_1080",
                         timeout=30)
        j = json.loads(r.read().decode("utf-8", "replace"))
        dj = j.get("danejson")
        if not dj or dj == "null":
            raise RuntimeError(f"brak anonimowej sesji dla subdomeny {self.sub}")
        d = json.loads(dj)
        self.podmiot = d["podmiot"]
        self.nazwa = d.get("nazwapodmiotu", "")
        return d

    def _zawartosc(self, action, parametr="null"):
        _rate()
        url = (f"{PORTAL}/admin/zawartosc.php?action={action}"
               f"&parametr={parametr}&podmiot={self.podmiot}&osoba=0"
               f"&time=1_1_1&tabId=tab-radoskop")
        return self.op.open(url, timeout=40).read().decode("utf-8", "replace")

    def sessions(self):
        """[{id,name,date,points:[{id,name,type}]}] — newest first."""
        html = self._zawartosc("O3")
        m = re.search(r'\{"points".*', html, re.S)
        if not m:
            raise RuntimeError("O3: brak JSON listy sesji")
        txt = m.group(0)
        try:
            data = json.loads(txt)
        except json.JSONDecodeError:
            data = json.loads(txt[: _balanced_end(txt)])
        return data["list"]["sessions"]

    def vote_detail(self, point_id):
        """-> dict chart json (may lack 'persons' when resultExists false)."""
        html = self._zawartosc("O7", str(point_id))
        m = re.search(r"chartScores src='([^']+)'", html)
        if not m:
            return None
        try:
            chart = json.loads(base64.b64decode(m.group(1)).decode("utf-8"))
        except Exception:
            return None
        # krzyżowa walidacja z tabelą HTML (tylko gdy jest)
        rows = re.findall(r"<tr.*?</tr>", html, re.S)
        table = {}
        for r in rows:
            cells = [re.sub(r"<[^>]+>", "", c).strip()
                     for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", r, re.S)]
            if len(cells) == 4 and cells[0].isdigit():
                table[(cells[1], cells[2])] = cells[3].upper()
        if table and chart and chart.get("persons"):
            mis = 0
            for p in chart["persons"]:
                key = (p["lastName"], p["firstName"])
                tv = table.get(key)
                cv = _vote_label(chart, p)
                if tv and cv and _norm_lbl(tv) != _norm_lbl(cv):
                    mis += 1
            chart["_table_mismatch"] = mis
            chart["_table_rows"] = len(table)
        return chart


def _balanced_end(txt):
    depth = 0
    for i, ch in enumerate(txt):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i + 1
    return len(txt)


_VNORM = {"ZA": "za", "PRZECIW": "przeciw", "WSTRZ": "wstrz",
          "WSTRZYMA": "wstrz", "BRAK": "brak", "NIEOB": "nieob"}


def _norm_lbl(s):
    s = re.sub(r"[^A-ZĄĆĘŁŃÓŚŹŻ]", "", (s or "").upper())
    for pref, out in _VNORM.items():
        if s.startswith(pref):
            return out
    return s


def _vote_label(chart, p):
    if not p.get("present"):
        return "NIEOBECNY"
    votes = p.get("votes") or []
    if not votes:
        return "BRAK"
    k = votes[0]
    return {1: "ZA", 2: "PRZECIW", 3: "WSTRZYMAL"}.get(k, f"?{k}")


def classify_sessions(sessions, kad_start):
    """Filtr: kadencja start + odrzuć sesje testowe/nieplinarne."""
    keep = []
    for s in sessions:
        nm = (s.get("name") or "").lower()
        if "test" in nm:
            continue
        if not any(k in nm for k in ("sesja", "sesji")):
            continue
        date = (s.get("date") or "")[:10]
        if not date or date < kad_start:
            continue
        keep.append(s)
    keep.sort(key=lambda s: s["date"])
    return keep


def session_num(name):
    m = re.search(r"([IVXLCDM]+)\s*(?:sesja|sesji)?", name or "")
    m2 = re.match(r"\s*(?:Sesja nr\s*)?([IVXLCDM]+)", (name or "").strip(), re.I)
    if m2:
        return m2.group(1).upper()
    return (name or "").strip()[:12]


def votes_from_chart(chart, roster_names):
    """chart -> (named{za,przeciw,wstrzymal_sie}, nieobecni, clubs{name:partia})
    albo None gdy niedostępne/niespójne."""
    if not chart or not chart.get("resultExists") or "persons" not in chart:
        return None
    named = {"za": [], "przeciw": [], "wstrzymal_sie": []}
    nieob = []
    clubs = {}
    code_map = {1: "za", 2: "przeciw", 3: "wstrzymal_sie"}
    for p in chart["persons"]:
        nm = f"{p['firstName']} {p['lastName']}".strip()
        if p.get("partia"):
            clubs[nm] = p["partia"].strip()
        if not p.get("present"):
            nieob.append(nm)
            continue
        votes = p.get("votes") or []
        if not votes:
            continue
        cat = code_map.get(votes[0])
        if cat:
            named[cat].append(nm)
    # spójność z licznikami charta
    cnt = (len(named["za"]), len(named["przeciw"]), len(named["wstrzymal_sie"]))
    opts = chart.get("options", {})
    exp = (opts.get("o1", {}).get("counter"), opts.get("o2", {}).get("counter"),
           opts.get("o3", {}).get("counter"))
    if any(e is not None for e in exp) and exp != cnt:
        return None
    if chart.get("_table_mismatch"):
        return None
    return named, nieob, clubs
