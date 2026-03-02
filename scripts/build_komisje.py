#!/usr/bin/env python3
"""Build komisje.json from Neo4j commission data + cross-reference with data.json councilors."""
import json
import os

# Data extracted from Neo4j: MATCH (c:Organization) WHERE c.type = 'Komisja Rady'
# + MATCH (p:Person)-[:MEMBER_OF]->(c) with DISTINCT
KOMISJE_RAW = [
    {
        "name": "Komisja Bezpieczeństwa i Porządku Publicznego",
        "przewodniczacy": "Tomasz Sybilski",
        "members": ["Joanna Wiśniewska-Najgebauer", "Karolina Ziołó-Pużuk", "Michalina Szymborska",
                     "Maciej Binkowski", "Filip Frąckowiak", "Tomasz Sybilski", "Renata Królak",
                     "Anna Auksel-Sekutowicz"]
    },
    {
        "name": "Komisja Budżetu i Finansów",
        "przewodniczacy": "Dorota Lutomirska",
        "members": ["Magdalena Gogol", "Dariusz Figura", "Martyna Jałoszyńska", "Damian Kowalczyk",
                     "Agnieszka Borowska", "Alicja Żebrowska", "Agnieszka Wyrwał",
                     "Jarosław Szostakowski", "Piotr Wertenstein-Żuławski", "Ewa Malinowska-Grupińska",
                     "Zofia Smełka-Leszczyńska", "Dorota Lutomirska", "Tomasz Herbich",
                     "Sławomir Potapowicz", "Michalina Szymborska", "Marcin Kluś"]
    },
    {
        "name": "Komisja Edukacji",
        "przewodniczacy": "Beata Michalec",
        "members": ["Agnieszka Gierzyńska-Kierwińska", "Iwona Pawłowska", "Piotr Mazurek",
                     "Paweł Lech", "Christian Młynarek", "Sandra Spinkiewicz", "Elżbieta Łaniewska",
                     "Damian Kowalczyk", "Sylwia Krajewska", "Renata Niewitecka",
                     "Agata Diduszko-Zyglewska", "Beata Michalec", "Jarosław Szostakowski",
                     "Anna Auksel-Sekutowicz", "Barbara Socha", "Joanna Krzemień", "Agata Korc"]
    },
    {
        "name": "Komisja Etyki",
        "przewodniczacy": "Joanna Wiśniewska-Najgebauer",
        "members": ["Jacek Cieślikowski", "Ewa Janczar", "Joanna Wiśniewska-Najgebauer",
                     "Ewa Malinowska-Grupińska", "Kamila Gołębiowska"]
    },
    {
        "name": "Komisja Infrastruktury i Inwestycji",
        "przewodniczacy": "Piotr Wertenstein-Żuławski",
        "members": ["Christian Młynarek", "Melania Łuczak", "Damian Kowalczyk", "Marta Szczepańska",
                     "Maciej Binkowski", "Sylwia Krajewska", "Magdalena Gogol", "Sandra Spinkiewicz",
                     "Zofia Smełka-Leszczyńska", "Iwona Wujastyk", "Sławomir Potapowicz",
                     "Jan Mencwel", "Agnieszka Borowska", "Iwona Pawłowska",
                     "Agnieszka Miękwicz", "Piotr Wertenstein-Żuławski"]
    },
    {
        "name": "Komisja Inwentaryzacyjna",
        "przewodniczacy": "Tomasz Herbich",
        "members": ["Dariusz Figura", "Jarosław Szostakowski", "Jan Mencwel",
                     "Tomasz Herbich", "Ewa Malinowska-Grupińska"]
    },
    {
        "name": "Komisja Kultury i Promocji Miasta",
        "przewodniczacy": "Agnieszka Wyrwał",
        "members": ["Piotr Mazurek", "Joanna Staniszkis", "Filip Frąckowiak", "Jarosław Jóźwiak",
                     "Ewa Malinowska-Grupińska", "Renata Królak", "Agnieszka Wyrwał",
                     "Beata Michalec", "Agata Diduszko-Zyglewska", "Anna Nehrebecka-Byczewska",
                     "Iwona Wujastyk", "Sylwia Krajewska", "Małgorzata Zakrzewska"]
    },
    {
        "name": "Komisja Ładu Przestrzennego",
        "przewodniczacy": "Michał Matejka",
        "members": ["Ewa Janczar", "Elżbieta Łaniewska", "Barbara Socha", "Michał Matejka",
                     "Melania Łuczak", "Jarosław Jóźwiak", "Krystian Wilk", "Tomasz Herbich",
                     "Marta Szczepańska", "Beata Michalec", "Joanna Krzemień"]
    },
    {
        "name": "Komisja Ochrony Środowiska",
        "przewodniczacy": "Renata Niewitecka",
        "members": ["Piotr Szyszko", "Jan Mencwel", "Grażyna Wereszczyńska", "Beata Michalec",
                     "Agnieszka Miękwicz", "Marta Szczepańska", "Justyna Zając", "Wojciech Zabłocki",
                     "Melania Łuczak", "Krystian Wilk", "Joanna Staniszkis", "Tomasz Sybilski",
                     "Sandra Spinkiewicz", "Renata Niewitecka"]
    },
    {
        "name": "Komisja Polityki Społecznej i Rodziny",
        "przewodniczacy": "Martyna Jałoszyńska",
        "members": ["Jarosław Szostakowski", "Piotr Mazurek", "Marta Jabłońska",
                     "Martyna Jałoszyńska", "Mariusz Budziszewski", "Elżbieta Łaniewska",
                     "Grażyna Wereszczyńska", "Agata Diduszko-Zyglewska", "Iwona Pawłowska"]
    },
    {
        "name": "Komisja Rewizyjna",
        "przewodniczacy": "Marcin Kluś",
        "members": ["Magdalena Gogol", "Marcin Kluś", "Christian Młynarek", "Justyna Zając",
                     "Agnieszka Borowska", "Iwona Wujastyk", "Wojciech Zabłocki", "Jan Mencwel"]
    },
    {
        "name": "Komisja Rozwoju Gospodarczego, Mieszkalnictwa i Cyfryzacji",
        "przewodniczacy": "Karolina Ziołó-Pużuk",
        "members": ["Karolina Ziołó-Pużuk", "Agnieszka Miękwicz", "Dariusz Figura",
                     "Sławomir Potapowicz", "Agnieszka Gierzyńska-Kierwińska", "Ewa Janczar",
                     "Melania Łuczak", "Michał Matejka", "Kamila Gołębiowska"]
    },
    {
        "name": "Komisja Samorządowa i Integracji Europejskiej",
        "przewodniczacy": "Wojciech Zabłocki",
        "members": ["Mariusz Budziszewski", "Małgorzata Zakrzewska", "Marta Jabłońska",
                     "Wojciech Zabłocki", "Martyna Jałoszyńska"]
    },
    {
        "name": "Komisja Skarg, Wniosków i Petycji",
        "przewodniczacy": "Zofia Smełka-Leszczyńska",
        "members": ["Agata Korc", "Joanna Wiśniewska-Najgebauer", "Tomasz Herbich",
                     "Zofia Smełka-Leszczyńska"]
    },
    {
        "name": "Komisja Sportu, Rekreacji i Turystyki",
        "przewodniczacy": "Mariusz Budziszewski",
        "members": ["Justyna Zając", "Agata Korc", "Tomasz Sybilski", "Piotr Szyszko",
                     "Martyna Jałoszyńska", "Jacek Cieślikowski", "Piotr Wertenstein-Żuławski",
                     "Joanna Krzemień", "Barbara Socha", "Paweł Lech", "Alicja Żebrowska",
                     "Mariusz Budziszewski"]
    },
    {
        "name": "Komisja Statutowo-Regulaminowa",
        "przewodniczacy": "Iwona Wujastyk",
        "members": ["Marcin Kluś", "Jarosław Jóźwiak", "Christian Młynarek",
                     "Kamila Gołębiowska", "Iwona Wujastyk", "Jarosław Szostakowski"]
    },
    {
        "name": "Komisja Zdrowia",
        "przewodniczacy": "Jarosław Jóźwiak",
        "members": ["Marta Jabłońska", "Agnieszka Wyrwał", "Agnieszka Gierzyńska-Kierwińska",
                     "Alicja Żebrowska", "Dorota Lutomirska", "Anna Auksel-Sekutowicz",
                     "Agata Diduszko-Zyglewska", "Paweł Lech", "Renata Niewitecka",
                     "Grażyna Wereszczyńska", "Michał Matejka", "Krystian Wilk",
                     "Jarosław Jóźwiak"]
    },
    {
        "name": "Komisja ds. Nazewnictwa Miejskiego",
        "przewodniczacy": "Anna Nehrebecka-Byczewska",
        "members": ["Joanna Staniszkis", "Karolina Ziołó-Pużuk", "Renata Królak",
                     "Filip Frąckowiak", "Anna Nehrebecka-Byczewska"]
    },
]

def build():
    # Load data.json councilors for cross-reference
    docs = os.path.join(os.path.dirname(__file__), '..', 'docs')
    with open(os.path.join(docs, 'data.json'), 'r') as f:
        data = json.load(f)

    # Get IX kadencja councilors
    kad_ix = next((k for k in data['kadencje'] if k['id'] == '2024-2029'), None)
    councilor_names = set()
    if kad_ix:
        councilor_names = {c['name'] for c in kad_ix['councilors']}

    komisje = []
    for k in KOMISJE_RAW:
        members = sorted(set(k['members']))
        # Mark which members are councilors in data.json
        members_data = []
        for m in members:
            members_data.append({
                "name": m,
                "is_councilor": m in councilor_names,
                "is_przewodniczacy": m == k["przewodniczacy"]
            })

        komisje.append({
            "name": k["name"],
            "przewodniczacy": k["przewodniczacy"],
            "member_count": len(members),
            "members": members_data,
        })

    # Build per-councilor index: name -> [komisja names]
    councilor_komisje = {}
    for k in komisje:
        for m in k["members"]:
            name = m["name"]
            if name not in councilor_komisje:
                councilor_komisje[name] = []
            entry = {"komisja": k["name"]}
            if m["is_przewodniczacy"]:
                entry["przewodniczacy"] = True
            councilor_komisje[name].append(entry)

    output = {
        "kadencja": "2024-2029",
        "komisje": komisje,
        "councilor_komisje": councilor_komisje
    }

    out_path = os.path.join(docs, 'komisje.json')
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"Written {out_path}")
    print(f"  {len(komisje)} komisji")
    print(f"  {len(councilor_komisje)} osób w komisjach")

    # Show councilors NOT found in data.json
    all_members = set()
    for k in komisje:
        for m in k["members"]:
            all_members.add(m["name"])
    not_in_data = all_members - councilor_names
    if not_in_data:
        print(f"  Members NOT in data.json councilors: {sorted(not_in_data)}")

if __name__ == '__main__':
    build()
