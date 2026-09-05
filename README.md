# Tesla Charging – ověřené ceny

Veřejný datový zdroj rodinné iOS aplikace Tesla Charging.

Cenový robot každý den před ranní aktualizací aplikace načte aktuální oficiální ceníky
E.ON Drive, PRE POINT a ČEZ futurego. Nový `prices.json` zveřejní pouze tehdy,
když se podaří jednoznačně ověřit všechny tři zdroje a všech 11 používaných
tarifních pravidel. Při změně formátu nebo chybě zůstane poslední platný soubor
beze změny.

Ruční spuštění je dostupné v **Actions → Aktualizovat ceny nabíjení → Run
workflow**. iOS aplikace neobsahuje žádný GitHub token; tlačítko **Aktualizovat
ceny** pouze bezpečně stáhne poslední robotem ověřený výsledek.

Veřejný feed: https://tlobel.github.io/tesla-charging-data/prices.json
