#!/usr/bin/env python3
"""Build budget.json from Neo4j data + link budget votes from data.json."""
import json
import re
import os

DOCS_DIR = os.path.join(os.path.dirname(__file__), '..', 'docs')

# --- Budget totals (from Neo4j export) ---
BUDGET_TOTALS = [
    {"year": 2015, "revenue": 3850000000, "expenditure": 4100000000, "deficit": 250000000, "estimated": True, "coverage_percentage": 78.5},
    {"year": 2016, "revenue": 4050000000, "expenditure": 4300000000, "deficit": 250000000, "estimated": True, "coverage_percentage": 83},
    {"year": 2017, "revenue": 4200000000, "expenditure": 4450000000, "deficit": 250000000, "estimated": True, "coverage_percentage": 66},
    {"year": 2018, "revenue": 4400000000, "expenditure": 4650000000, "deficit": 250000000, "estimated": True, "coverage_percentage": 69},
    {"year": 2019, "revenue": 4600000000, "expenditure": 4900000000, "deficit": 300000000, "estimated": True, "coverage_percentage": 74},
    {"year": 2020, "revenue": 4750000000, "expenditure": 5100000000, "deficit": 350000000, "estimated": True, "coverage_percentage": 60},
    {"year": 2021, "revenue": 4850000000, "expenditure": 5250000000, "deficit": 400000000, "estimated": True, "coverage_percentage": 64},
    {"year": 2022, "revenue": 5100000000, "expenditure": 5500000000, "deficit": 400000000},
    {"year": 2023, "revenue": 4890000000, "expenditure": 5380000000, "deficit": 490000000},
    {"year": 2024, "revenue": 5156000000, "expenditure": 5712000000, "deficit": 556000000},
    {"year": 2025, "revenue": 5433092723, "expenditure": 6032444352, "deficit": 599351629},
]

# --- Category breakdown (from Neo4j BudgetLine aggregation) ---
CATEGORIES_RAW = [
    (2015, "Transport", 519688776), (2015, "Edukacja", 476836243), (2015, "Administracja", 189310500),
    (2015, "Gospodarka komunalna", 172887888), (2015, "Kultura", 163763026), (2015, "Pomoc społeczna", 151346330),
    (2015, "Gospodarka mieszkaniowa", 98963179), (2015, "Sport", 84643645), (2015, "Obsługa długu", 41993038),
    (2015, "Inne", 56591849), (2015, "Rolnictwo", 31138248),
    (2016, "Transport", 611045258), (2016, "Edukacja", 489731055), (2016, "Gospodarka komunalna", 170050825),
    (2016, "Pomoc społeczna", 159592480), (2016, "Administracja", 122435208), (2016, "Sport", 111093336),
    (2016, "Gospodarka mieszkaniowa", 94297189), (2016, "Kultura", 82385928), (2016, "Inne", 65687531),
    (2016, "Obsługa długu", 30698635),
    (2017, "Edukacja", 514611765), (2017, "Transport", 411335331), (2017, "Rodzina", 226821725),
    (2017, "Gospodarka komunalna", 193143029), (2017, "Administracja", 137000154), (2017, "Sport", 130323080),
    (2017, "Pomoc społeczna", 86990572), (2017, "Gospodarka mieszkaniowa", 76469868), (2017, "Kultura", 67645995),
    (2017, "Inne", 67694014), (2017, "Obsługa długu", 27418359),
    (2018, "Edukacja", 573702341), (2018, "Transport", 433547394), (2018, "Rodzina", 311445504),
    (2018, "Gospodarka komunalna", 229356991), (2018, "Administracja", 157103597), (2018, "Sport", 122756338),
    (2018, "Gospodarka mieszkaniowa", 95187465), (2018, "Pomoc społeczna", 84221827), (2018, "Kultura", 70772993),
    (2018, "Inne", 63724809), (2018, "Obsługa długu", 23148851),
    (2019, "Edukacja", 642878199), (2019, "Transport", 619786004), (2019, "Rodzina", 314522911),
    (2019, "Gospodarka komunalna", 279830048), (2019, "Administracja", 179975856), (2019, "Sport", 120516457),
    (2019, "Gospodarka mieszkaniowa", 104034310), (2019, "Pomoc społeczna", 91320043), (2019, "Kultura", 88675917),
    (2019, "Inne", 119734575), (2019, "Obsługa długu", 19308623),
    (2020, "Edukacja", 707104147), (2020, "Transport", 677578536), (2020, "Gospodarka komunalna", 282669752),
    (2020, "Administracja", 205516201), (2020, "Sport", 123117082), (2020, "Gospodarka mieszkaniowa", 105087589),
    (2020, "Pomoc społeczna", 103480132), (2020, "Kultura", 91360893), (2020, "Inne", 121489782),
    (2020, "Obsługa długu", 20012840),
    (2021, "Edukacja", 747748860), (2021, "Transport", 730604569), (2021, "Gospodarka komunalna", 310882624),
    (2021, "Gospodarka mieszkaniowa", 155361095), (2021, "Kultura", 128105713), (2021, "Pomoc społeczna", 121406414),
    (2021, "Sport", 117111203), (2021, "Administracja", 97651917), (2021, "Inne", 157951159),
    (2021, "Obsługa długu", 19148211),
    (2022, "Edukacja", 879547797), (2022, "Transport", 760499472), (2022, "Gospodarka komunalna", 387019585),
    (2022, "Rodzina", 326581397), (2022, "Administracja", 243756563), (2022, "Gospodarka mieszkaniowa", 228358053),
    (2022, "Kultura", 154276434), (2022, "Pomoc społeczna", 138491297), (2022, "Sport", 136652285),
    (2022, "Inne", 124642693), (2022, "Obsługa długu", 33506277),
    (2023, "Edukacja", 878874055), (2023, "Transport", 776515663), (2023, "Gospodarka komunalna", 381334541),
    (2023, "Gospodarka mieszkaniowa", 220033400), (2023, "Pomoc społeczna", 186865004), (2023, "Kultura", 142429230),
    (2023, "Sport", 120425904), (2023, "Inne", 163441932), (2023, "Obsługa długu", 0),
    (2024, "Edukacja", 1652723097), (2024, "Transport", 1035532834), (2024, "Gospodarka komunalna", 458518886),
    (2024, "Administracja", 381241398), (2024, "Rodzina", 244422643), (2024, "Pomoc społeczna", 233598114),
    (2024, "Gospodarka mieszkaniowa", 230407378), (2024, "Kultura", 159918341), (2024, "Sport", 97374284),
    (2024, "Inne", 220243352), (2024, "Obsługa długu", 95486288),
    (2025, "Edukacja", 2043350175), (2025, "Transport", 1193457216), (2025, "Gospodarka komunalna", 555371630),
    (2025, "Administracja", 456435415), (2025, "Gospodarka mieszkaniowa", 294712502), (2025, "Rodzina", 294254385),
    (2025, "Pomoc społeczna", 263237261), (2025, "Kultura", 201925805), (2025, "Inne", 161386177),
    (2025, "Sport", 81642561), (2025, "Obsługa długu", 97466017),
]

# Build categories per year
categories_by_year = {}
for year, cat, amt in CATEGORIES_RAW:
    categories_by_year.setdefault(year, []).append({"name": cat, "amount": amt})

for year in categories_by_year:
    categories_by_year[year].sort(key=lambda x: -x["amount"])

# --- Link budget votes ---
with open(os.path.join(DOCS_DIR, 'data.json'), 'r') as f:
    data = json.load(f)

budget_votes = {}  # year -> list of vote IDs

for kad in data['kadencje']:
    for v in kad['votes']:
        topic = (v.get('topic') or '').lower()
        if 'budżet' not in topic and 'budzet' not in topic:
            continue
        # Match "uchwalenia budżetu ... na XXXX rok" or "budżetu ... na XXXX rok"
        # or "budżetu na XXXX rok"
        m = re.search(r'budżet\w*\s+.*?na\s+(\d{4})\s+rok', topic)
        if not m:
            # Try "budżetu Miasta Gdańska za XXXX"
            m = re.search(r'budżet\w*\s+.*?za\s+(\d{4})', topic)
        if m:
            budget_year = int(m.group(1))
            budget_votes.setdefault(budget_year, []).append({
                "id": v['id'],
                "topic": v.get('topic', ''),
                "date": v.get('session_date', ''),
                "za": (v.get('counts') or {}).get('za', 0),
                "przeciw": (v.get('counts') or {}).get('przeciw', 0),
            })

# Build final structure
budget_data = {
    "totals": BUDGET_TOTALS,
    "categories": {str(y): cats for y, cats in categories_by_year.items()},
    "votes": {str(y): votes for y, votes in sorted(budget_votes.items())},
}

out_path = os.path.join(DOCS_DIR, 'budget.json')
with open(out_path, 'w') as f:
    json.dump(budget_data, f, ensure_ascii=False)

print(f"Written {out_path}")
for y in sorted(budget_votes):
    print(f"  {y}: {len(budget_votes[y])} budget votes")
print(f"  {len(BUDGET_TOTALS)} years of totals")
print(f"  {len(categories_by_year)} years of category breakdowns")
