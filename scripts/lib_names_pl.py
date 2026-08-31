#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Wspólne narzędzia nazw radnych dla scraperów DSSS Vote / APWINC II (PL 'Nazwisko Imię' -> 'Imię Nazwisko').

DSSS Vote (Brzozów, Polczyn-Zdrój) i APWINC II (Olsztynek) wydają każdą nazwę
jako NAZWISKO IMIĘ. Radoskop standard = "Imię Nazwisko" — verify_city check
`councilor_names` flaguje reversed jako FAIL.

Słownik PL_FIRST_NAMES 1:1 z radoskop-premium/scripts/verify_city.py (battle-tested,
pokrywa ~99% radnych; rzadkie imię spoza listy = false negative, nie false alarm).
"""
import re

PL_FIRST_NAMES = frozenset("""
adam adrian adrianna agata agnieszka albert aldona aleksander aleksandra
alicja alina amelia andrzej aneta angelika anita anna antoni antonina
arkadiusz artur aurelia barbara bartlomiej bartłomiej bartosz beata benedykt
berenika bernadeta bernard blazej błażej bogdan bogumiła bogusław bogusława
bolesław borys bożena bronisław cecylia celina cezary czesław dagmara damian
daniel danuta daria dariusz dawid dominik dominika dorota edmund edward edyta
eleonora elżbieta emil emilia ernest eryk eugeniusz ewa ewelina fabian
felicja filip franciszek fryderyk gabriel gabriela genowefa grażyna grzegorz
gustaw halina hanna helena henryk honorata hubert ignacy igor ilona inga
irena ireneusz iwona izabela izabella jacek jadwiga jakub jan janina janusz
jarosław jerzy joachim joanna jolanta jonasz józef józefa judyta julia
julian julita juliusz justyna kacper kajetan kalina kamil kamila karina
karol karolina katarzyna kazimierz kinga klaudia konrad konstanty kornel
kornelia krystian krystyna krzysztof ksawery laura lech leon leonard leszek
lidia liliana lucjan lucyna ludwik magdalena maksymilian malwina małgorzata
marcel marcin marek maria marian marianna mariola mariusz marlena marta
martyna marzena mateusz matylda maurycy michalina michał mieczysław mikołaj
milena miłosz mirosław mirosława monika natalia natasza nikodem nikola nina
norbert olaf olga olgierd oliwia oskar patrycja patryk paulina paweł piotr
przemysław radosław rafał renata robert roksana roman romuald róża ryszard
sabina sandra sebastian sergiusz seweryn sławomir stanisław stanisława
stefan stefania sylwia szczepan szymon tadeusz teodor teresa tobiasz tomasz
tymoteusz urszula violetta wacław waldemar walenty wanda weronika wiesław
wiesława wiktor wincenty wioleta wioletta witold władysław włodzimierz
wojciech zbigniew zdzisław zenon zofia zuzanna zygmunt żaneta łucja łukasz
sylwester ferdynand oliwier wawrzyniec alan arnold eliasz natan leokadia
apolonia bohdan nadja
adriana alfred alfreda ali alojzy anatol andżelika anne-sophie arleta arletta
arwid bożenna brygida christian claudia cyprian diana domicela eliza elwira
emilian emin eskan georgios greta henryka hieronim iga jeannette jowita justyn
jędrzej klaudiusz kosma larysa laurence lechosław lena lilia lilianna
litosława lubomir ludmiła luiza maciej maja manuela marcelina marcjanna marcus
marzanna marzenna maximilian melania michael nelly nikolaos ola radomir
rajmund remigiusz roch samanta sara sonia sławomira tamara tatiana tytus
wiktoria zdzisława
""".split())


def _is_first(tok: str) -> bool:
    return tok.lower() in PL_FIRST_NAMES


def _titlecase_upper(tok: str) -> str:
    if tok.isupper() and tok.isalpha() and len(tok) >= 2:
        return tok.capitalize()
    return tok


def fix_name_order(raw: str) -> str:
    """'Bednarczyk Krzysztof' -> 'Krzysztof Bednarczyk';
    'BRYŚ Marek' -> 'Marek Bryś'; 'Chmielewska Magdalena Anna' -> 'Magdalena Anna Chmielewska'.
    Nie rusza poprawnego 'Imię Nazwisko'; trzyma nazwiska z łącznikiem (Kacperczyk-Baran).
    """
    raw = re.sub(r"\s+", " ", raw).strip()
    if not raw:
        return raw
    tokens = [_titlecase_upper(t) for t in raw.split()]
    if len(tokens) < 2 or len(tokens) > 4:
        return " ".join(tokens)
    # Wszystkie znanе imiona (np. dwuimienne bez nazwiska)? — zostaw
    if all(_is_first(t) for t in tokens):
        return " ".join(tokens)
    # 'Nazwisko Imię' (może + drugie imię): ostatni znany token=imię, pierwszy=nie-imię
    if _is_first(tokens[-1]) and not _is_first(tokens[0]):
        rest = tokens[:-1]
        if len(rest) <= 3:
            return " ".join(tokens[1:] + [tokens[0]])
    # 'Nazwisko Imię Drugie-Imię' wariant B: dr. token imię, pierwszy nie
    if len(tokens) >= 3 and _is_first(tokens[1]) and not _is_first(tokens[0]) \
            and not _is_first(tokens[-1]) and not _is_first(tokens[2]):
        return " ".join([tokens[1], tokens[2], tokens[0]])
    return " ".join(tokens)


def fix_all(names) -> list:
    """Fix + dedupe, zachowując kolejność."""
    out, seen = [], set()
    for n in names:
        f = fix_name_order(n)
        if f and f not in seen:
            out.append(f)
            seen.add(f)
    return out
