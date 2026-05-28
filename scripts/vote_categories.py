#!/usr/bin/env python3
"""Reguły kategoryzacji głosowań per język.

Template index.html kategoryzuje głosowania po stronie klienta (JS funkcja
categorizeVote) na podstawie regexów keyword w tytule uchwały. Oryginalnie
reguły były hardcoded po polsku, więc dla miast zagranicznych (Wilno LT,
Bratysława SK, Berlin/Landtag MV DE, Ryga LV, Praga CS, Tallin ET) każde
głosowanie wpadało w kategorię "inne".

Ten moduł trzyma reguły per locale i generuje JS array literal wstrzykiwany
przez placeholder {{CAT_RULES_JS}} w generate_site.py / generate_assembly_site.py.

Kategorie (kolejność = priorytet matchowania, pierwsza wygrywa):
  budzet, inwestycje, planowanie, nieruchomosci, transport, oswiata,
  zdrowie, srodowisko, kultura, skarga, nazwy, procedura
Fallback gdy nic nie pasuje: "inne".

Regexy są case-insensitive (flaga 'i' dodawana w JS). Wzorce per język są
mniej szczegółowe niż polski (który ma ~15 lat dopracowywania), ale pokrywają
główne tematy samorządowe.
"""

# Każda reguła to (kategoria, wzorzec_regex_string).
# Wzorce NIE mają delimiterów ani flag - JS dodaje /.../ i 'i'.

CAT_RULES_BY_LOCALE: dict[str, list[tuple[str, str]]] = {
    # ── Polski (oryginał, pełna szczegółowość) ──────────────────────────
    "pl": [
        ("budzet", r"budże[tc]|finansow|dochodów|wydatków|podatk|opłat|skarbnik|absolutorium|WPF|wieloletni.*prognoz|dotacj|umarzani.*spłat|rozkładani.*rat|kredyt|pożyczk|zaciągnięci|stawek.*jednostkow|ekwiwalent.*pienięż|średni.*cen.*paliw|czynszów|odpłatności za pobyt"),
        ("inwestycje", r"inwestycj|budow[aąęy]|przebudow|remont|modernizacj|spółk.*kapitał|objęci.*udziałów"),
        ("planowanie", r"plan.*zagospodarowania|miejscowego planu|studium|zagospodarowania przestrzennego|rewitaliz|obszar.*zdegradowan"),
        ("nieruchomosci", r"nieruchom|gruntu|działk|dzierżaw|użytkowania wieczyst|sprzedaż.*lokalu|bonifikat|lokali użytkow|zasad.*gospodarowan.*zasob|lokali.*mieszkaln|wynajmowan.*lokali|mieszkaniow"),
        ("transport", r"transport|komunikacj|kategorii dróg|drogi gminnej|tramwaj|autobus|metro|parking|ścieżk.*rowerow|stref.*płatnego parkowania|elektromobil|zarząd.*dróg"),
        ("oswiata", r"szkoł|przedszkol|żłob|oświat|edukacj|stypend|nadania imienia|sieci.*szkół|liceum|branżow.*szkół|godzin zajęć|nagrod.*edukacyjn"),
        ("zdrowie", r"zdrow|szpital|społeczn|pomoc.*społeczn|bezdomn|niepełnospraw|senioraln|opiek|alkohol|profilaktyk|piecz.*zastępcz|mieszkani.*wspomagany|mieszkani.*chronionych|organizacj.*pozarządow"),
        ("srodowisko", r"środowisk|zieleń|park[uói]|ekolog|odpady|klimat|wycink|kąpielisk|sezon.*kąpielow"),
        ("kultura", r"kultur|bibliotek|zabytk|pomnik|muzeum|teatr|nagrod.*miasta|nagrody.*miasta|konkurs.*literack|konserwatorsk|nagrod.*historyczn|nagrod.*literack"),
        ("skarga", r"skarg|petycj|rozpatrzenia skargi|rozpatrzenia wniosku"),
        ("nazwy", r"nadania nazwy|nazwy? obiektowi|nazewnictw|zniesieni.*nazwy?|zmiany? nazw|nadania ulic"),
        ("procedura", r"protokoł|porządk.*obrad|ślubowani|włącz?enie druku|komisj.*rewizyjn|powołani|regulamin|statut.*dzielnicy|statut.*zakładu|przyjęci.*regulamin|odesłanie do komisji|zamknięci.*obrad|otwarci.*sesji|okręg.*wyborczy|podział.*dzielnicy|ławnik|rezolucj|oświadczeni|apel|upoważnien.*dyrektor|tekst.*jednolity|stanowisko nr|przewodniczą|wiceprzewodniczą|porozumieni.*gmin|współdziałan.*gmin|kasyn.*gry|wyrażeni.*opinii|stwierdzeni.*nieważności|wyznaczeni.*termin|przedstawiciel.*rady|powierzeni|referendum|zmian.*siedziby|członk.*jury|kandydat.*na członk"),
    ],
    # ── Litewski (Wilno) ────────────────────────────────────────────────
    "lt": [
        ("budzet", r"biudžet|finans|mokest|mokesči|asignav|dotacij|subsidij|paskol|kredit|skol|įmok|tarif|atlygin|kompensac"),
        ("inwestycje", r"investicij|statyb|rekonstrukcij|remont|modernizav|kapital|įrengim"),
        ("planowanie", r"teritorij.*planav|bendr.*plan|detal.*plan|saugom.*teritorij|urbanist|kraštovaizdž"),
        ("nieruchomosci", r"nekilnojam|žemės sklyp|sklyp|nuom|patalp|pastat|turto|būst|gyvenam.*plot|servitut"),
        ("transport", r"transport|susisiekim|maršrut|gatv|keli[ao]|dvirač|automobili|parkav|viešojo transporto|eismo|stovėjim"),
        ("oswiata", r"mokykl|darželi|švietim|ugdym|stipendij|gimnazij|studij|akadem|lopšel"),
        ("zdrowie", r"sveikat|ligonin|socialin|globos|neįgali|senjor|param|slaug|narkoman|alkohol|priklausomyb"),
        ("srodowisko", r"aplink|želdyn|park[ao]|ekolog|atliek|klimat|tarš|gamtos|miškų|vanden"),
        ("kultura", r"kultūr|bibliotek|paveld|paminkl|muziej|teatr|men[ao]|festival"),
        ("skarga", r"skund|peticij|prašym.*nagrin"),
        ("nazwy", r"pavadinim|gatvės pavadinim|vardo suteik"),
        ("procedura", r"protokol|darbotvark|reglament|statut|komitet|komisij|sprendim.*pakeit|tvark.*apraš|nuostat|posėdž|įgaliojim|deleguot|nutarim|priesaik|kandidat|skyrim|paskyr"),
    ],
    # ── Słowacki (Bratysława) ───────────────────────────────────────────
    "sk": [
        ("budzet", r"rozpočet|financ|dotáci|úver|pôžičk|poplat|dan[eí]|finančn|príspevok|odmen"),
        ("inwestycje", r"investíci|výstavb|rekonštrukci|oprav|modernizác|kapitál|zriaden"),
        ("planowanie", r"územn.*plán|územného plánu|urbanist|regulač|rozvoj.*mest"),
        ("nieruchomosci", r"nehnuteľnost|pozemk|pozemok|nájom|priestor|budov|byt[uy]|prenájom|vecné bremeno"),
        ("transport", r"doprav|komunikáci|cest[ay]|ulic|električk|autobus|parkov|cyklotras|MHD|premávk"),
        ("oswiata", r"škol|matersk.*škol|vzdeláv|štipend|gymnáziu|jasl"),
        ("zdrowie", r"zdrav|nemocnic|sociáln|opatrovateľ|zdravotn|senior|pomoc|závislos"),
        ("srodowisko", r"životné prostredie|zeleň|park|ekológi|odpad|klím|znečisten|prírod"),
        ("kultura", r"kultúr|knižnic|pamiatk|múze|divadl|umeni|festival"),
        ("skarga", r"sťažnos|petíci|žiados.*prerokovan"),
        ("nazwy", r"názov|pomenovani|ulice.*názov|premenovani"),
        ("procedura", r"zápisnic|program rokovani|rokovací poriadok|štatút|komisi|výbor|uzneseni.*zmen|všeobecne záväzné|VZN|menovani|poverenie|delegovani|voľb|kandidát|zloženie sľubu"),
    ],
    # ── Niemiecki (Berlin, Landtag MV) ──────────────────────────────────
    "de": [
        ("budzet", r"Haushalt|Finanz|Steuer|Gebühr|Zuschuss|Darlehen|Kredit|Etat|Abgabe|Entgelt|Vergütung"),
        ("inwestycje", r"Investition|Bau\b|Sanierung|Modernisierung|Umbau|Neubau|Errichtung"),
        ("planowanie", r"Bebauungsplan|Flächennutzungsplan|Stadtplanung|Bauleitplan|Raumordnung|Entwicklungsplan"),
        ("nieruchomosci", r"Grundstück|Immobilie|Liegenschaft|Pacht|Miete|Gebäude|Wohnung|Erbbaurecht"),
        ("transport", r"Verkehr|Nahverkehr|Straße|Bus\b|Bahn|U-Bahn|S-Bahn|Parken|Radweg|ÖPNV|Mobilität|Tram"),
        ("oswiata", r"Schule|Kita|Kindergarten|Bildung|Stipendium|Hochschule|Gymnasium|Ausbildung"),
        ("zdrowie", r"Gesundheit|Krankenhaus|Sozial|Pflege|Behindert|Senior|Klinik|Sucht|Drogen"),
        ("srodowisko", r"Umwelt|Grün|Park\b|Ökolog|Abfall|Klima|Naturschutz|Müll|Gewässer"),
        ("kultura", r"Kultur|Bibliothek|Denkmal|Museum|Theater|Kunst|Festival"),
        ("skarga", r"Beschwerde|Petition|Eingabe|Bürgerantrag"),
        ("nazwy", r"Benennung|Straßenname|Namensgebung|Umbenennung"),
        ("procedura", r"Protokoll|Tagesordnung|Geschäftsordnung|Satzung|Ausschuss|Wahlperiode|Beschlussfassung|Verordnung|Bestellung|Ernennung|Wahl\b|Drucksache|Antrag der Fraktion|Resolution|Entschließung|Stellungnahme|Vereidigung"),
    ],
    # ── Łotewski (Ryga) ─────────────────────────────────────────────────
    "lv": [
        ("budzet", r"budžet|finans|nodok|maksāj|dotācij|aizdevum|kredīt|nodev|atlīdzīb"),
        ("inwestycje", r"investīcij|būvniecīb|rekonstrukcij|remont|moderniz|izbūv"),
        ("planowanie", r"teritorij.*plān|detālplān|attīstības plān|pilsētbūvniec|lokālplān"),
        ("nieruchomosci", r"nekustam|zemes gabal|zemesgabal|nom[au]|telp|ēk[au]|dzīvokl|īpašum|servitūt"),
        ("transport", r"transport|satiksm|iel[au]|autobus|tramvaj|stāvviet|veloceļ|sabiedrisk.*transport"),
        ("oswiata", r"skol|bērnudārz|izglītīb|stipendij|ģimnāzij|pirmsskol"),
        ("zdrowie", r"veselīb|slimnīc|sociāl|aprūp|invalīd|senior|palīdzīb|atkarīb"),
        ("srodowisko", r"vid[ei]s|apstādījum|park[su]|ekoloģij|atkritum|klimat|dab[au]"),
        ("kultura", r"kultūr|bibliotēk|pieminek|muzej|teātr|māksl|festivāl"),
        ("skarga", r"sūdzīb|petīcij|iesniegum.*izskat"),
        ("nazwy", r"nosaukum|ielas nosaukum|vārda piešķir|pārdēvē"),
        ("procedura", r"protokol|darba kārtīb|reglament|nolikum|komitej|komisij|lēmum.*groz|saistošo noteikum|iecelš|pilnvaroj|deleģ|ievēlē|kandidāt|zvērest"),
    ],
    # ── Czeski (Praga) ──────────────────────────────────────────────────
    "cs": [
        ("budzet", r"rozpočet|finanč|daň|poplat|dotac|úvěr|půjčk|příspěvek|odměn"),
        ("inwestycje", r"investic|výstavb|rekonstrukc|oprav|moderniz|stavb|zřízen"),
        ("planowanie", r"územní plán|územního plánu|urbanist|regulačn|rozvoj.*měst"),
        ("nieruchomosci", r"nemovitost|pozemk|pozemek|nájem|prostor|budov|byt[uy]|pronájem|věcné břemeno"),
        ("transport", r"doprav|komunikac|ulic|tramvaj|autobus|metro|parkov|cyklostezk|MHD|provoz"),
        ("oswiata", r"škol|mateřsk.*škol|vzděláv|stipend|gymnáziu|jesl"),
        ("zdrowie", r"zdrav|nemocnic|sociáln|pečovatel|zdravotn|senior|pomoc|závislost"),
        ("srodowisko", r"životní prostředí|zeleň|park|ekolog|odpad|klima|znečišt|přírod"),
        ("kultura", r"kultur|knihovn|památk|muze|divadl|uměn|festival"),
        ("skarga", r"stížnost|petic|žádost.*projedn"),
        ("nazwy", r"název|pojmenován|ulice.*název|přejmenován"),
        ("procedura", r"zápis|program jednání|jednací řád|statut|komis|výbor|usnesen.*změn|obecně závazn|jmenován|pověřen|delegován|volb|kandidát|složení slibu"),
    ],
    # ── Estoński (Tallin) ───────────────────────────────────────────────
    "et": [
        ("budzet", r"eelarve|finants|maks[ua]|toetus|laen|tasu|hüvitis"),
        ("inwestycje", r"investeering|ehitus|rekonstrueeri|remont|moderniseeri|rajamine"),
        ("planowanie", r"planeering|üldplaneering|detailplaneering|ruumiline|arengukava"),
        ("nieruchomosci", r"kinnisvara|maa-ala|krunt|rent|ruum|hoone|korter|üür|servituut"),
        ("transport", r"transport|ühistransport|tänav|buss|tramm|parkimine|rattatee|liiklus"),
        ("oswiata", r"kool|lasteaed|haridus|stipendium|gümnaasium"),
        ("zdrowie", r"tervis|haigla|sotsiaal|hooldus|puudega|eakas|abi|sõltuvus"),
        ("srodowisko", r"keskkond|haljastus|park|ökoloog|jäätmed|kliima|loodus"),
        ("kultura", r"kultuur|raamatukogu|mälestis|muuseum|teater|kunst|festival"),
        ("skarga", r"kaebus|petitsioon|taotlus.*läbivaat"),
        ("nazwy", r"nimetus|tänava nimi|nime andmine|ümbernimetamine"),
        ("procedura", r"protokoll|päevakord|kodukord|põhikiri|komisjon|otsuse muutmine|määrus|nimetamine|volitamine|valimine|kandidaat|ametivanne"),
    ],
    # ── Duński (Kopenhaga) ──────────────────────────────────────────────
    # Krótkie duńskie słowa (havn, vej, bus, tog, lan, gade, park) muszą mieć
    # granice słów (\b) bo inaczej trafiają w środki innych słów (np. 'havn'
    # w 'Københavns'). 'park' osobno od 'parkering' (środowisko vs transport).
    # planowanie i nieruchomosci PRZED budzet, bo 'lokalplan' zawiera 'lan'.
    "da": [
        # Krótkie duńskie słowa (vej, havn, bus, tog, gade, park) muszą mieć
        # granice słów żeby nie trafiać w środki innych słów (np. 'havn' w
        # 'Københavns'). Dla typowych prefiksów w compoundach (klima, kultur,
        # bibliotek) używamy tylko lewej granicy. Kolejność: nazwy PRZED
        # transport (vejnavn -> nazwy, nie vej -> transport); planowanie i
        # nieruchomosci PRZED budzet (lokalplan zawiera 'lan').
        ("nazwy", r"\bnavngivning\b|\bvejnavn|\bgadenavn|\bomd[øo]bning\b|\bstednavn|\bnavne?[æae]ndring\b"),
        ("planowanie", r"\blokalplan|\bkommuneplan|\bbyudvikling|\bbyplan\b|\bbyomdannelse|\budviklingsplan|\bbyrum\b|\bgenopretning|\bhelhedsplan|\bbygge ?og bevaring"),
        ("nieruchomosci", r"\bejendom|\bgrund\b|\bmatrikel|\balmene boliger|\budlejning|\blejebolig|\blejekontrakt|\blejem[åa]l|\bareal\b|\bsalg af ejendom|\bk[øo]b af ejendom|\bfamilieboliger|\bungdomsbolig|\bplejebolig|\bandelsbolig"),
        ("oswiata", r"\bskole|\bfolkeskole|\bspecialskole|\bb[øo]rnehave|\bvuggestue|\bdaginstitution|\bdagtilbud|\bfritidsklub|\bfritidsordning|\bfritidshjem|\buddannelse|\bgymnasium|\belev\b|\belever\b|\bp[æae]dagogisk"),
        ("zdrowie", r"\bsundhed|\bhospital|\bsygehus|\bsocial\b|\bsociale\b|\bsocialudvalg|\b[æae]ldre\b|\b[æae]ldreomsorg|\b[æae]ldrepleje|\bomsorg\b|\bhandicap|\budsatte\b|\bhjeml[øo]se\b|\bmisbrug|\bensomhed|\bbost[øo]tte|\bsundhedshus|\btandpleje"),
        ("srodowisko", r"\bmilj[øo]|\bklima|\bgr[øo]n\b|\bgr[øo]nt\b|\bgr[øo]nne\b|\bb[æae]redygtig|\baffald|\bskraldespand|\bnatur\b|\bnatur[bcdfg-z]|\bbiodivers|\bpark\b|\bparken\b|\bparker\b|\bparkerne\b|\btr[æae]er\b|\btr[æae]plantning|\bjordforuren|\bvandkvalitet|\bst[øo]j\b|\bkystsikring"),
        ("kultura", r"\bkultur|\bbibliotek|\bmuseum|\bkunst\b|\bkunstner|\bteater|\bmusik\b|\bfestival|\bfortidsminde|\bfredet\b|\bkulturarv|\bungdomskultur|\bbiograf\b|\bspillested"),
        ("transport", r"\btrafik|\bvej\b|\bveje\b|\bvejnet|\bgade\b|\bgader\b|\bcykel|\bcyklist|\bcyklisme|\bmetro\b|\bbus\b|\bbusser\b|\bletbane|\bs-tog\b|\btog\b|\bparkering|\bp-plads|\bstoppested|\bmobilitet\b|\bhavn\b|\bhavnen\b|\bhavne\b|\bhavneudvikling|\bkanal\b|\bkanaler\b|\bf[æae]rge|\bbro\b|\bbroer\b|\bkryds\b|\bhastighedsgr[æae]nse|\bhastighedsd[æae]mp|\bkollektiv trafik|\blinje \d+[A-Z]?\b|\bfredelig"),
        ("budzet", r"\bbudget|\bfinansier|\bfinansiel|\bskat\b|\bskatter\b|\bgebyr|\bafgift|\btilskud|\bbevilling|\bl[åa]n\b|\bkredit\b|\bvederlag|\bhonorering|\btakst|\bkontingent|\bm[åa]ltal|regnskab\b|\bkirkeligning|\boverenskomstforhandl"),
        ("inwestycje", r"\banl[æae]g\b|\banl[æae]gsbevilling|\binvestering|\bbyggeri\b|\bnybyggeri|\bombygning|\brenovering|\bmodernisering|\budbygning|\bop[fø]relse|\budskiftning|\betablering\b|\btilbygning"),
        ("skarga", r"\bklage\b|\bklager\b|\bklageudvalg|\bindsigelse\b|\bborgerhenvendelse"),
        ("procedura", r"\bdagsorden|\bforretningsorden|\bvedt[æae]gter|\bkommissorium|\budvalg\b|\bvalg af\b|\bvalgperiode|\bkandidat\b|\bindstilling om udpegning|\bmedlemsforslag\b|\bforesp[øo]rgsel|\bborgmester|\budn[æae]vnelse|\bhabilitet|\bprotokol|\bprotokolbem[æae]rkning|\budpegning\b|\bh[øo]ring\b|\bsamarbejdsaftale|\bfuldmagt|\bdelegation\b|\bber[æae]tning|\brokering|\bsuppleant"),
    ],
    # ── Nederlands (Amsterdam, Den Haag) ────────────────────────────────
    "nl": [
        ("budzet", r"begroting|financiën|financieel|belasting|heffing|leges|tarieven|subsidie|lening|krediet|precario|rioolheffing|toeristenbelasting|reclamebelasting|marktgelden|precariobelasting"),
        ("inwestycje", r"investering|bouw|nieuwbouw|verbouw|renovatie|reconstructie|herinrichting|kapitaal|oprichting|aanleg"),
        ("planowanie", r"bestemmingsplan|omgevingsplan|omgevingsvisie|stedenbouw|ruimtelijk|structuurvisie|gebiedsplan|locatieplan|exploitatieplan"),
        ("nieruchomosci", r"grond|vastgoed|pand|verhuur|huur|woning|woningen|sociale huur|middenhuur|erfpacht|perceel|verkoop.*onroerend|onroerend goed|atelierbeleid|broedplaats"),
        ("transport", r"verkeer|vervoer|openbaar vervoer|fiets|parkeer|stalling|metro|tram|bus\b|wegverkeer|mobiliteit|scheepvaart|haven|cruise|zeecruise|rivierboot"),
        ("oswiata", r"onderwijs|school|scholen|leerling|kinderopvang|peuterspeelzaal|gymnasium|middelbaar|basisschool|stipendium|stagefonds|sportakkoord"),
        ("zdrowie", r"gezondheid|zorg|welzijn|sociaal|armoede|bijstand|beschermd wonen|maatschappelijk|daklozen|verslaving|ouderen|mantelzorg|thuiszorg|gehandicapt"),
        ("srodowisko", r"groen|groenstructuur|park\b|milieu|klimaat|duurzaam|boom|bomen|ecolog|afval|water|recreatiegebied|lutkemeer|natuur"),
        ("kultura", r"cultuur|museum|bibliotheek|monument|erfgoed|theater|kunst|subsidie.*cultur|festival|evenement"),
        ("skarga", r"klacht|bezwaar|petitie|beroep|motie van afkeuring"),
        ("nazwy", r"vernoeming|straatnaam|naamswijziging|naamgeving"),
        ("procedura", r"verordening|reglement|statuut|commissie|raadscommissie|presidium|amendement|motie\b|agenda|notulen|vergadering.*vaststelling|besluit.*vaststelling|aangenomen worden|leden van de raad|volmacht|lidmaatschap|stemming.*procedur|regeling.*vaststell|legesverordening|algemene verordening|afdeling.*verordening|wijziging.*verordening"),
    ],
}


def generate_cat_rules_js(locale: str) -> str:
    """Zwróć JS array literal `[[cat, /regex/i], ...]` dla danego locale.

    Fallback do PL jeśli locale nieobsługiwany (zachowuje dotychczasowe
    zachowanie dla polskich miast i miast bez własnego słownika).
    """
    rules = CAT_RULES_BY_LOCALE.get((locale or "pl").lower(), CAT_RULES_BY_LOCALE["pl"])
    lines = []
    for cat, pattern in rules:
        # Escape backslashy nie trzeba - regexy są raw stringami, ale w JS
        # regex literal slash '/' musi być escaped. Nasze wzorce nie zawierają
        # '/', więc bezpieczne. Walidacja na wszelki wypadek.
        safe = pattern.replace("/", r"\/")
        lines.append(f"  ['{cat}', /{safe}/i],")
    return "[\n" + "\n".join(lines) + "\n]"


if __name__ == "__main__":
    import sys
    loc = sys.argv[1] if len(sys.argv) > 1 else "pl"
    print(generate_cat_rules_js(loc))
