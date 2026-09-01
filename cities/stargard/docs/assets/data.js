window.__CFG={"siteUrl":"https://stargard.radoskop.pl","cityName":"Stargard","citySlug":"stargard","cityGenitive":"Stargardu","siteTitle":"Radoskop Stargard — Jak głosują radni?","siteDescription":"Radoskop — otwarte narzędzie monitoringu Rada Miejska w Stargardzie. Sprawdź skład rady, kluby i kalendarz sesji.","bipName":"BIP Urząd Miejski w Stargardzie","rootHost":"radoskop.pl","budgetNote":"","impressumHtml":"","hasVotingData":false,"hasSpeakerActivity":false,"hasCouncilors":true,"councilorRosterMode":false,"catRules":[
  ['budzet', /budże[tc]|finansow|dochodów|wydatków|podatk|opłat|skarbnik|absolutorium|WPF|wieloletni.*prognoz|dotacj|umarzani.*spłat|rozkładani.*rat|kredyt|pożyczk|zaciągnięci|stawek.*jednostkow|ekwiwalent.*pienięż|średni.*cen.*paliw|czynszów|odpłatności za pobyt/i],
  ['inwestycje', /inwestycj|budow[aąęy]|przebudow|remont|modernizacj|spółk.*kapitał|objęci.*udziałów/i],
  ['planowanie', /plan.*zagospodarowania|miejscowego planu|studium|zagospodarowania przestrzennego|rewitaliz|obszar.*zdegradowan/i],
  ['nieruchomosci', /nieruchom|gruntu|działk|dzierżaw|użytkowania wieczyst|sprzedaż.*lokalu|bonifikat|lokali użytkow|zasad.*gospodarowan.*zasob|lokali.*mieszkaln|wynajmowan.*lokali|mieszkaniow/i],
  ['transport', /transport|komunikacj|kategorii dróg|drogi gminnej|tramwaj|autobus|metro|parking|ścieżk.*rowerow|stref.*płatnego parkowania|elektromobil|zarząd.*dróg/i],
  ['oswiata', /szkoł|przedszkol|żłob|oświat|edukacj|stypend|nadania imienia|sieci.*szkół|liceum|branżow.*szkół|godzin zajęć|nagrod.*edukacyjn/i],
  ['zdrowie', /zdrow|szpital|społeczn|pomoc.*społeczn|bezdomn|niepełnospraw|senioraln|opiek|alkohol|profilaktyk|piecz.*zastępcz|mieszkani.*wspomagany|mieszkani.*chronionych|organizacj.*pozarządow/i],
  ['srodowisko', /środowisk|zieleń|park[uói]|ekolog|odpady|klimat|wycink|kąpielisk|sezon.*kąpielow/i],
  ['kultura', /kultur|bibliotek|zabytk|pomnik|muzeum|teatr|nagrod.*miasta|nagrody.*miasta|konkurs.*literack|konserwatorsk|nagrod.*historyczn|nagrod.*literack/i],
  ['skarga', /skarg|petycj|rozpatrzenia skargi|rozpatrzenia wniosku/i],
  ['nazwy', /nadania nazwy|nazwy? obiektowi|nazewnictw|zniesieni.*nazwy?|zmiany? nazw|nadania ulic/i],
  ['procedura', /protokoł|porządk.*obrad|ślubowani|włącz?enie druku|komisj.*rewizyjn|powołani|regulamin|statut.*dzielnicy|statut.*zakładu|przyjęci.*regulamin|odesłanie do komisji|zamknięci.*obrad|otwarci.*sesji|okręg.*wyborczy|podział.*dzielnicy|ławnik|rezolucj|oświadczeni|apel|upoważnien.*dyrektor|tekst.*jednolity|stanowisko nr|przewodniczą|wiceprzewodniczą|porozumieni.*gmin|współdziałan.*gmin|kasyn.*gry|wyrażeni.*opinii|stwierdzeni.*nieważności|wyznaczeni.*termin|przedstawiciel.*rady|powierzeni|referendum|zmian.*siedziby|członk.*jury|kandydat.*na członk/i],
],"kindCats":{},"voteCatsExtra":{}};
function clubColor(club) {
  return club === 'ZP' ? '#0ea5e9' : club === 'KO' ? '#f59e0b' : club === 'PiS' ? '#1f4ea0' : 'var(--muted)';
}
function clubBg(club) {
  return club === 'ZP' ? '#0369a1' : club === 'KO' ? '#b45309' : club === 'PiS' ? '#173a78' : '#374151';
}
var _clubCls={'ZP':'zp','KO':'ko','PiS':'pis'};
function clubClass(club) {
  return _clubCls[club] ? 'club-'+_clubCls[club] : 'club-unknown';
}