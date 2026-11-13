# Werdens Media LLM

Eigen LLM-stack voor Werdens Media: één engine met dezelfde API als Ollama én de
OpenAI-API, werkruimtes met je eigen documenten en bronvermelding, en spraak naar
tekst en spraak uit. Alles op je eigen hardware, zonder abonnement en zonder
tokenkosten — met een Nederlandse interface in de huisstijl van Werdens Media.

![Werdens Media LLM — een antwoord uit je eigen documenten, met de bronnen eronder](docs/img/laag-2-antwoord.png)

[![Platform](https://img.shields.io/badge/platform-Proxmox%20%7C%20Debian%20%7C%20Linux-2ea3f2)](#installeren-op-proxmox)
[![Python](https://img.shields.io/badge/python-3.11%2B-1e73be?logo=python&logoColor=white)](https://www.python.org/)
[![Engine](https://img.shields.io/badge/engine-llama.cpp-5ff0e8)](https://github.com/ggml-org/llama.cpp)
[![API](https://img.shields.io/badge/API-Ollama%20%2B%20OpenAI-2ea3f2)](#api)
[![Licentie](https://img.shields.io/badge/licentie-alle%20rechten%20voorbehouden-55657f)](README.md#licentie)

---

## Wat het is

Eén stack, drie lagen, alles op je eigen hardware:

| Laag | Wat het doet | Status |
|---|---|---|
| **1. Engine** | Eigen LLM-server met dezelfde API als Ollama plus de OpenAI-API. Beheert modellen, start en stopt de rekenkracht-engine, houdt modellen warm. | **werkt en is getest** |
| **2. Werkruimte** | Werkruimtes per project, documenten erin met bronvermelding, vectordatabase op SQLite, gesprekken, API-sleutels, embed-widget voor je eigen sites. | **werkt en is getest** |
| **3. Media** | Spraak naar tekst (Whisper), spraak uit (Piper, Nederlandse stemmen). Beeld (ComfyUI/SDXL) en video staan nog open. | **spraak werkt** |

Waarom eigen code in plaats van een bestaande app forken: één techniek in plaats
van twee, geen licentie die meereist, en alles wat erin zit is van jou.

## Zo ziet het eruit

Alle drie de lagen hebben een eigen webpagina. Hieronder echte opnamen van de
draaiende stack — op een CPU zonder GPU, met 4 vCPU en 7,6 GB geheugen.
De pagina's staan in de huisstijl van **Werdens Monitor**: licht, kaarten op wit,
blauw `#2ea3f2` als accent en de merk-gradient (cyaan → blauw) op de knoppen, met
het logo linksboven naast de naam.

**De engine** (poort 11434) — status, modellen in huis en de endpoints:

![De engine-statuspagina](docs/img/laag-1-engine.png)

**De werkruimte** (poort 8090) — een vraag stellen over je eigen documenten:

![De werkruimtelaag](docs/img/laag-2-werkruimte.png)

En het antwoord, met de bronnen eronder en de score per bron:

![Antwoord met bronvermelding](docs/img/laag-2-antwoord.png)

Op een telefoon klapt hetzelfde scherm naar één kolom:

![De werkruimte op een telefoon](docs/img/mobiel-werkruimte.png)

**De medialaag** (poort 8091) — spraak naar tekst en spraak uit met een
Nederlandse stem:

![De medialaag](docs/img/laag-3-media.png)

En hier is echt iets voorgelezen: 7,5 seconden spraak in 0,92 seconden — acht keer
sneller dan realtime, op de CPU.

![Spraak uit met Piper](docs/img/laag-3-spraak.png)

**De embed-widget** — één regel script op je eigen site, met antwoorden uit jouw
documenten en de bronnen erbij. Een werkende voorbeeldpagina staat in
`docs/embed-voorbeeld.html`.

![De embed-widget op een gewone website](docs/img/embed-widget.png)

**Een rondleiding in beeld** (echte opname, inclusief de wachttijd op een
antwoord — op een CPU duurt dat ongeveer anderhalve minuut):

[![Bekijk de rondleiding](docs/img/laag-2-werkruimte.png)](docs/demo.mp4)

De opnamen zijn gemaakt met `scripts/screenshots.js` en `scripts/record-demo.js`,
zodat je ze na een wijziging zelf opnieuw kunt maken.

## Chatten met de AI

Open `http://<server>:11434/chat` in je browser — of klik op **Chatten** op de
statuspagina. Typ je vraag, druk op Enter, en het antwoord komt woord voor woord
binnen. Alles blijft op je eigen machine.

![De chatpagina van de Werdens Media LLM](docs/img/chat.png)

- Je kiest zelf het model in de kop, als er meer dan één in huis staat.
- **Nieuw gesprek** begint opnieuw; het gesprek blijft staan als je de pagina
  herlaadt of de volgende dag terugkomt.
- **Stop** breekt een lang antwoord af — de engine stopt dan ook echt met rekenen
  in plaats van door te werken voor een leeg scherm.
- Onderaan staat wat het kostte: tekens, seconden en tekens per seconde.
- Op een telefoon werkt dezelfde pagina:

![De chatpagina op een telefoon](docs/img/chat-mobiel.png)

## Gereedschap: wat het model niet zelf kan weten

Een taalmodel weet niet wat het vandaag in Hilversum doet, wat er in het nieuws
is of hoe laat het is. De engine haalt dat zelf op en zet het boven de vraag, dus
het model hoeft niets te beslissen — dat is precies waarom het ook met een klein
model werkt.

Er zijn driehonderdvijfenzeventig gereedschappen, in tweeëndertig groepen. Daarnaast staat er een **catalogus van drieduizend** gereedschappen naast, die je op naam aanroept.

**Zonder internet** — de engine rekent zelf, dus altijd goed en direct:

| Gereedschap | Voorbeeldvraag |
|---|---|
| rekenen | *hoeveel is 17% van 2480?* |
| eenheden | *10 km in mijl*, *hoeveel pond is 2 kilo?* |
| datum | *hoeveel dagen tot 5 december?* |
| feestdagen | *welke feestdagen zijn er in 2027?* |
| tijdzone | *hoe laat is het in New York?* |

**Bronnen zonder sleutel:**

| Gereedschap | Bron | Voorbeeldvraag |
|---|---|---|
| weer | [Open-Meteo](https://open-meteo.com) | *wat is het weer in Utrecht?* |
| buien | [Buienradar](https://www.buienradar.nl) | *gaat het zo regenen in Hilversum?* |
| lucht | Open-Meteo (CAMS) | *hoe is de luchtkwaliteit?* |
| zon | Open-Meteo | *hoe laat gaat de zon onder?* |
| nieuws | NOS, Tweakers, NU.nl, RTL | *wat is er in het nieuws?* |
| naslag | Nederlandse Wikipedia | *wie is Mark Rutte?* |
| woordenboek | [WikiWoordenboek](https://nl.wiktionary.org) | *wat betekent fiets?* |
| zoeken | DuckDuckGo | *zoek de openingstijden van de Gamma* |
| postcode | [PDOK](https://www.pdok.nl) (BAG) | *welke plaats hoort bij 1217 AB?* |
| valuta | Europese Centrale Bank | *wat is 250 dollar in euro?* |
| crypto | [CoinGecko](https://www.coingecko.com) | *wat is de koers van bitcoin?* |
| vertalen | MyMemory | *vertaal goedemorgen* |
| domein | [RDAP](https://rdap.org) | *van wie is nu.nl?* |
| verkeer | [NDW](https://opendata.ndw.nu) / Rijkswaterstaat | *zijn er files op de A2?* |
| tijd | deze machine | *hoe laat is het?* |

**Water, energie, afstand en meer:**

| Gereedschap | Bron | Voorbeeldvraag |
|---|---|---|
| water | [Rijkswaterstaat](https://rijkswaterstaatdata.nl) | *hoe hoog staat het water bij Vlissingen?* |
| stroomprijs | EnergyZero | *wat kost stroom nu?* |
| afstand | OpenStreetMap (OSRM) | *hoe ver is het van Utrecht naar Maastricht?* |
| wetten | [wetten.overheid.nl](https://wetten.overheid.nl) | *wat zegt de Waterwet?* |
| cbs | [CBS Open Data](https://opendata.cbs.nl) | *hoeveel inwoners heeft Sittard?* |
| boek | [Open Library](https://openlibrary.org) | *wie schreef De Avonden?* |
| vliegtuigen | [OpenSky](https://opensky-network.org) | *hoeveel vliegtuigen hangen er boven Nederland?* |

**Buurt, gezondheid en hulpjes:**

| Gereedschap | Bron | Voorbeeldvraag |
|---|---|---|
| aardbevingen | [EMSC](https://www.seismicportal.eu) | *waren er bevingen in Groningen?* |
| hooikoorts | Open-Meteo (pollen + UV) | *is het hooikoortsweer?* |
| bruggen | NDW / Rijkswaterstaat | *gaat de brug bij Alphen open?* |
| verwachting | Open-Meteo | *wat is de verwachting voor veertien dagen?* |
| gasprijs | EnergyZero | *wat kost gas nu?* |
| horeca | [OpenStreetMap](https://www.openstreetmap.org) | *welke pizzeria zit er in Utrecht?* |
| weeralarm | [MeteoAlarm](https://www.meteoalarm.org) | *is er code geel vandaag?* |
| buurtcijfers | CBS Open Data | *hoeveel 65-plussers wonen in Sittard?* |
| devhulp | de engine zelf | *sha256 van hallo*, *contrast tussen #fff en #1e73be* |

**Dagelijks gebruik en prijzen:**

| Gereedschap | Bron | Voorbeeldvraag |
|---|---|---|
| uurverloop | Open-Meteo | *wanneer is het droog vandaag?* |
| vorigjaar | Open-Meteo archief | *hoe warm was het vorig jaar op deze dag?* |
| btw | de engine zelf | *btw over 250 euro*, *250 inclusief btw* |
| inflatie | CBS | *wat is de inflatie?* |
| wikipedia | nl.wikipedia.org | *wie is Rijkswaterstaat?* |
| prijswacht | Marktplaats | *wat kost een rtx 3060 12gb tweedehands?* |
| afsluitingen | NDW / Rijkswaterstaat | *welke weg is afgesloten?* |
| laadpalen | OpenStreetMap | *waar kan ik mijn auto laden?* |
| parkeren | OpenStreetMap | *waar kan ik parkeren in Utrecht?* |
| feed | elke rss-feed | *laatste berichten van https://feeds.nos.nl/nosnieuwsalgemeen* |

**Dagritme, taal en onderweg:**

| Gereedschap | Bron | Voorbeeldvraag |
|---|---|---|
| brandstof | CBS | *wat kost benzine nu?* |
| zon | Open-Meteo | *hoe laat gaat de zon onder?* |
| synoniemen | WikiWoordenboek | *een ander woord voor huis* |
| koken | de engine zelf | *2 cup in ml*, *3 ons in gram* |
| prijsgeschiedenis | Marktplaats + eigen logboek | *wordt een rtx 3060 goedkoper?* |
| artikel | nl.wikipedia.org | *wat is het artikel van de dag?* |
| persberichten | Rijksoverheid | *wat heeft de overheid besloten?* |
| reistijd | OSRM + NDW | *hoe lang doe ik over Utrecht naar Maastricht?* |
| spelling | OpenTaal-woordenlijst | *controleer mijn spelling* |

**Weer en water, in detail:**

| Gereedschap | Bron | Voorbeeldvraag |
|---|---|---|
| regenradar | Buienradar | *waar regent het nu?* |
| neerslag | Buienradar (5 minuten) | *blijft het droog de komende twee uur?* |
| windstoten | Open-Meteo | *hoe hard waait het?* |
| uv | Open-Meteo | *hoe hoog is de zonkracht vandaag?* |
| verbrandingstijd | eigen berekening | *hoe lang kan ik in de zon blijven?* |
| klimaat | Open-Meteo archief | *hoe warm was het per maand?* |
| warmste dag | Open-Meteo archief | *wat was de warmste dag dit jaar?* |
| getijden | Rijkswaterstaat | *hoe laat is het hoogwater in Vlissingen?* |
| watertemperatuur | Rijkswaterstaat | *hoe warm is het zeewater?* |
| maanfase | eigen berekening | *hoe vol staat de maan?* |
| zwemwater | zwemwater.nl | *kan ik daar zwemmen?* (geen open API) |

**Reizen en vervoer:**

| Gereedschap | Bron | Voorbeeldvraag |
|---|---|---|
| werkzaamheden | NDW | *waar wordt aan de weg gewerkt?* |
| snelheidslimiet | OpenStreetMap | *hoe hard mag ik hier?* |
| flitspalen | OpenStreetMap | *waar staan flitspalen?* |
| ov | OVapi | *welke lijnen rijden er in Utrecht?* |
| spoorwerk | nieuwsfeed | *is er werk aan het spoor?* |
| fietsroute, wandelroute | OSM routing | *hoe lang fiets ik naar Amersfoort?* |
| tolwegen, veerponten | OpenStreetMap | *moet ik tol betalen?* |
| tankstations, p+r | OpenStreetMap | *waar kan ik tanken?* |
| vertrektijden, treinvertragingen | — | werken niet meer zonder sleutel |

**Geld, overheid en kennis:**

| Gereedschap | Bron | Voorbeeldvraag |
|---|---|---|
| valuta-geschiedenis, crypto-geschiedenis | Frankfurter, CoinGecko | *hoe deed de dollar het dit jaar?* |
| aandelenkoers | Yahoo Finance | *wat doet ASML?* |
| hypotheekrente, spaarrente | vergelijkingssites | *wat is de hypotheekrente?* |
| teruglevering, zonnepanelen | EnergyZero, Open-Meteo | *wat leveren mijn panelen op?* |
| kosten apparaat, reiskosten, rente op rente | eigen berekening | *wat kost een vriezer per jaar?* |
| bruto netto, belasting | eigen berekening (tarieven 2025) | *wat houd ik netto over?* |
| kamerstukken, bekendmakingen, subsidies, provincie, wetten | nieuwsfeeds | *wat speelt er in de Kamer?* |
| gemeente open data | CBS | *geef de cijfers van mijn gemeente* |
| wikipedia volledig, woord van de dag, definitie | Wikipedia, WikiWoordenboek | *geef het volledige artikel* |
| rijmwoorden | OpenTaal-woordenlijst | *wat rijmt op huis?* |
| werkwoorden, getallen in woorden | eigen berekening | *vervoeg lopen* |
| citaten | Wikiquote | *een citaat over moed* |
| films, muziekalbums, landeninfo, nobelprijzen | Wikidata, MusicBrainz, RestCountries, nobelprize.org | *wie speelt erin?* |
| vandaag in het jaar | Wikipedia | *wat gebeurde er op deze dag?* |

**Nieuws, gezondheid en de buurt:**

| Gereedschap | Bron | Voorbeeldvraag |
|---|---|---|
| nieuws onderwerp, nieuws gemeente, evenementen, nieuwsarchief | Google Nieuws-feed | *nieuws over ASML* |
| sportuitslagen | NOS | *wat zijn de uitslagen?* |
| podcasts | iTunes | *zoek een podcast over geschiedenis* |
| radiostations | NPO icecast | *welke zenders zijn er?* |
| calorieen, voedingswaarde, allergenen, barcode | Open Food Facts | *hoeveel calorieën zit erin?* |
| verbranding | eigen berekening | *hoeveel kcal verbrand ik met fietsen?* |
| ehbo | eigen naslag | *ehbo bij een brandwond* |
| griepcijfers, corona-cijfers, bijwerkingen, wachttijden | RIVM, CBG | eerlijke melding als er geen open API is |
| supermarkten, bereikbaarheid | OpenStreetMap | *waar is de supermarkt?* |
| huizenprijzen | CBS | *wat kost een huis in Sittard?* |
| hoogte | Open-Meteo | *hoe hoog ligt Utrecht?* |
| afval ophalen, zon op je dak | — | eerlijke melding: per gemeente geregeld |

**Er nog eens 133 bij, in negen nieuwe groepen:**

| Groep | Aantal | Wat erin zit |
|---|---|---|
| Weer en natuur | 14 | gevoelstemperatuur, dauwpunt, zicht, sneeuw, windrichting, fijnstof, poollicht, ruimteweer, ISS, maanopkomst, rivierafvoer, bodemvocht, graaddagen, zonuren |
| Verkeer en vervoer | 13 | kenteken opzoeken, APK, brandstofverbruik, terugroepacties, files nu, NS-verstoringen, dichtstbijzijnde halte, verkeersboetes, vliegtuig opzoeken, schip opzoeken, occasions, laadpunten, veerboottijden |
| Geld en zaken | 16 | maandlasten hypotheek, dividend, beursindices, grondstofprijzen, gasprijs TTF, crypto omrekenen, box 3, vakantiegeld, lease-bijtelling, goedkoopste pomp, huurwoningen, inflatie per productgroep, koopkracht, toeslagen, AOW-leeftijd, gemeentelijke lasten |
| Overheid en recht | 14 | raadsbesluiten, moties en amendementen, vergaderagenda, stemmingen, Staatscourant, open data register, EUR-Lex, reisadviezen, ambassades, oorlogsbronnen, archief, paspoort en ID, uittreksel BRP, aankomende verkiezingen |
| Kennis en taal | 20 | etymologie, de of het, meervoud, anagrammen, lettergrepen, spreekwoorden, citaten van een persoon, liedteksten, artiesteninfo, series, musea, monumenten, bergtoppen, vlaggen, wiskunde, statistiek, Romeinse cijfers, weeknummers, bieb zoeken, tijdlijn |
| Nieuws, sport en media | 12 | Formule 1, voetbalstanden, Eredivisie-programma, wielrennen, olympische medailles, podcast-afleveringen, YouTube zoeken, krantenvoorpagina's, mediawijsheid, streaming, concerten, evenementenkalender |
| Gezondheid en voeding | 11 | BMI en gezond gewicht, hartslagzones, stappen naar kilometers, E-nummers, AED in de buurt, apotheken, huisartsenpost, tekenbeet en lyme, reisvaccinaties, bloeddonatie, psychische hulp |
| Buurt en wonen | 13 | openingstijden winkel, scholen, kinderopvang, speeltuinen, honden uitlaatplekken, geldautomaat, brievenbus, glasbak, bibliotheek en zwembad, bestemmingsplan, leefbaarheid, energielabel, bouwprojecten |
| Techniek en internet | 20 | IP opzoeken, DNS, SSL-certificaat, GitHub, npm, PyPI, Docker, CVE zoeken, wachtwoordlek, wayback machine, QR-code, link inkorten, Linux-commando, cron, schermafdruk, UUID en wachtwoord, JSON formatteren, unixtijd, bestandsgrootte, serverstatus |

**Er nog eens 98 bij, in elf nieuwe groepen:**

| Groep | Aantal | Wat erin zit |
|---|---|---|
| Huishouden en klussen | 10 | verf, behang, tegels, beton en zand, schutting, wasgoed, vlekken, schoonmaakmiddelen, apparaatverbruik, huisonderhoud |
| Tuin en planten | 8 | zaaikalender, bodemtemperatuur, vorst op komst, gietadvies, zon in de tuin, plant opzoeken, boom herkennen, onkruid herkennen |
| Eten en koken | 10 | recept zoeken, recept met ingrediënt, willekeurig recept, cocktail, bier, wijn bij gerecht, ovenstanden, houdbaarheid, boodschappenlijst, menugrootte |
| Reizen en vakantie | 12 | adres opzoeken, weer op de bestemming, zonsondergang op reis, tijdverschil, munten omrekenen, rijtijd, tolwegen, tankstations langs de route, overnachting, kampeerplekken, bagageregels, reisverzekering |
| Wetenschap en ruimte | 10 | wetenschappelijke artikelen, artikel met DOI, arXiv, ruimtevaartnieuws, raketlanceringen, aardbevingen wereldwijd, planeten vannacht, sterrenhemel, aardmagnetisch veld, elementen |
| Kinderen en school | 8 | tafels, breuken, dictee, werkwoordspelling, kinderuitleg, kinderliedjes, spreekbeurt, geschiedenis voor kinderen |
| Auto en onderhoud | 8 | onderhoudsschema, bandenspanning, laadkosten, autokosten, wegenbelasting, schadeformulier, kentekencheck RDW, oldtimer-vrijstelling |
| Verzekering en pensioen | 8 | zorgverzekering, zorgkosten, inboedel, opstal, aansprakelijkheid, pensioenopbouw, pensioengat, lijfrente |
| Belasting en toeslagen | 8 | aangifte, aftrekposten, heffingskortingen, bijzondere tarieven, btw-aangifte, zzp-kosten, schenkbelasting, erfbelasting |
| Vrije tijd en hobby | 10 | hondenrassen, kattenrassen, spreuk van de dag, vogelherkenning, visstekken, fietstocht, wandelroutes, speeltuin, naambetekenis, woordspel |
| Diversen | 6 | was buiten, barbecueweer, feestdagen wereldwijd, naam van de dag, weer vorig jaar, eenheden voor druk en energie |

**De catalogus van drieduizend:**

Naast de gereedschappen hierboven zit er een catalogus in de engine: drieduizend
gereedschappen die elk op hun eigen naam te gebruiken zijn. Ze vallen in zeven families,
en elke familie heeft één functie in de engine — daarom zijn het er drieduizend zonder dat
de code drieduizend functies heeft.

| Familie | Aantal | Wat je ermee doet |
|---|---|---|
| CBS-cijfers | 1950 | *cbs 83625ned* — de cijfers van elke tabel van het CBS, met titel, periode en de eerste regels |
| Feestdagen per land | 204 | *feestdagen duitsland* — de feestdagen van dat land in het komende jaar |
| Lange weekenden per land | 204 | *lange weekenden duitsland* — welke feestdagen gunstig vallen voor een lang weekend |
| Kaart en omgeving | 199 | *dichtstbijzijnde brievenbus in Utrecht* — waar iets in de buurt is |
| Eigen reken- en taalgereedschap | 193 | *oppervlakte uitrekenen*, *kiloprijs vergelijken* — rekent de engine zelf |
| Wikipedia-onderwerpen | 177 | *treinstation: spoorwegstations in nederland* — naslag per onderwerp |
| Nieuwsbronnen | 73 | *nos.nl algemeen nieuws* — het laatste nieuws van die bron |

Elk van die drieduizend is ook een eigen route: `/api/cbs%2083625ned`,
`/api/feestdagen%20duitsland`. Zoeken in de catalogus gaat met
`/api/catalogus?zoek=koopwoningen` (met `&familie=cbs` en `&limiet=50` als je wilt
beperken), en het aantal staat in `/api/gereedschap`. Vraag je het in gewone taal, dan
pakt de engine de catalogus pas als geen enkel ander gereedschap de vraag herkent — de
gewone gereedschappen blijven dus voorgaan.

**Je eigen apps:**

| Gereedschap | Wat het leest | Voorbeeldvraag |
|---|---|---|
| recepten | VoorraadThuis (ruim 10.000 recepten) | *wat kan ik maken met prei?* |
| akkoorden | AkkoordKast | *hoe speel ik Sinterklaas kapoentje?* |
| zittesj | de woordenlijst van de Zittesj-app | *wat betekent neit?* |
| notities | je eigen notitiemap | *wat heb ik over de LLM opgeschreven?* |
| status | deze machine | *hoe staat de server ervoor?* |
| boodschappen | VoorraadThuis-voorraad + recepten | *wat moet ik kopen voor preisoep?* |
| document | je eigen documentenmap | *wat staat er in mijn notitie over de LLM?* |

De notities en de documenten lezen alleen wat je zelf aanwijst: zet
`WLLM_NOTITIES` en `WLLM_DOCUMENTEN` op het pad van je mappen (meerdere mappen
mogen, gescheiden door een dubbele punt). Zonder die instelling zegt het
gereedschap eerlijk dat het niets kan lezen in plaats van te gokken. Voor de
boodschappenlijst wijst `WLLM_VOORRAAD_DB` naar de database van VoorraadThuis.

De **prijsgeschiedenis** bewaart elke dag de mediaanprijs in een eigen SQLite-bestand
(`WLLM_PRIJS_DB`, standaard `~/.werdens-media-llm/prijzen.db`). Hoe vaker je hem vraagt,
hoe meer je ziet. De **spelling**-controle haalt de OpenTaal-woordenlijst (173.607 woorden)
één keer op en bewaart die naast de prijsdatabase.

De gereedschappen die OpenStreetMap gebruiken (horeca, laadpalen, parkeren) kunnen
traag zijn of tijdelijk niets teruggeven: de gratis Overpass-servers zitten soms vol.
De engine zegt dat dan eerlijk en probeert drie spiegels na elkaar.

Documenten worden gelezen als txt, md, docx en pdf. Let op: een pdf die zijn
tekst met een eigen lettercode opslaat — zoals alles wat Chromium maakt — kan de
engine niet ontcijferen; dan zegt hij dat eerlijk en kun je er een txt- of
docx-versie naast zetten.

Je kunt een gereedschap ook direct afdwingen met een slash-commando: `/weer
Utrecht`, `/reken 250 * 1,21`, `/verkeer A2`, `/recepten prei`, `/zittesj neit`,
`/status` en zo verder. Alles is sleutelvrij en dus gratis; uitzetten kan met
`WLLM_GEREEDSCHAP=0`.

Er zijn ook losse routes, handig voor een eigen script of een dashboard:

```bash
curl 'http://127.0.0.1:11434/api/weer?plaats=Hilversum'
curl 'http://127.0.0.1:11434/api/buien?plaats=Hilversum'
curl 'http://127.0.0.1:11434/api/verkeer?weg=A2'
curl 'http://127.0.0.1:11434/api/recepten?q=prei'
curl 'http://127.0.0.1:11434/api/status'
curl 'http://127.0.0.1:11434/api/water?plaats=Vlissingen'
curl 'http://127.0.0.1:11434/api/stroomprijs'
curl 'http://127.0.0.1:11434/api/afstand?q=van%20Utrecht%20naar%20Maastricht'
curl 'http://127.0.0.1:11434/api/cbs?gemeente=Sittard'
curl 'http://127.0.0.1:11434/api/weeralarm'
curl 'http://127.0.0.1:11434/api/bruggen'
curl 'http://127.0.0.1:11434/api/buurtcijfers?gemeente=Sittard'
curl 'http://127.0.0.1:11434/api/prijswacht?q=rtx%203060'
curl 'http://127.0.0.1:11434/api/inflatie'
curl 'http://127.0.0.1:11434/api/btw?q=btw%20over%20250%20euro'
curl 'http://127.0.0.1:11434/api/reistijd?q=van%20Utrecht%20naar%20Maastricht'
curl 'http://127.0.0.1:11434/api/brandstof'
curl 'http://127.0.0.1:11434/api/gereedschap'      # wat er beschikbaar is
```

Bij weer, neerslag en verkeer hoort bronvermelding — Buienradar en NDW vragen
daar uitdrukkelijk om, en de engine zet de bron altijd in het antwoord.

## Snel starten (op een Debian-machine)

```bash
# 1) engine ophalen (llama.cpp, voorgebouwde binary — geen compiler nodig)
mkdir -p engine && cd engine
curl -sLO https://github.com/ggml-org/llama.cpp/releases/download/b12262/llama-b12262-bin-ubuntu-x64.tar.gz
tar xzf llama-b12262-bin-ubuntu-x64.tar.gz

# 2) server starten
cd .. && python3 engine/wllm.py serve      # luistert op :11434
```

## Installeren op Windows

Download de map van deze repo (groene **Code**-knop → *Download ZIP*), pak hem uit
en dubbelklik op **`deploy\windows\start-werdens-media-llm.bat`**.

De eerste keer wordt alles wat nodig is opgehaald (llama.cpp voor Windows plus de
modellen, circa 2,5 GB); daarna starten de drie lagen meteen en opent de
chatpagina. Nodig is alleen **Python 3** van [python.org](https://www.python.org/downloads/windows/)
(met het vinkje *Add python.exe to PATH*); verder niets te installeren.

```powershell
# liever zelf de variant kiezen (cpu, vulkan, cuda12, cuda13)
python deploy\windows\setup.py --variant vulkan
```

Stoppen: `deploy\windows\stop-werdens-media-llm.bat`. Zie
[`deploy/windows/README.md`](deploy/windows/README.md) voor de details, de
poorten en spraak.

## Installeren op Proxmox

Eén regel, in de shell van je **Proxmox-host** (als root):

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/Theberas04/werdens-media-llm/main/deploy/proxmox/install.sh)"
```

Dat haalt de kit op naar `/opt/werdens-media-llm-kit` en zet de hele stack in één
unprivileged LXC (Debian 13, 4 cores, 12 GB, 60 GB schijf). Reken op een kwartier
tot een half uur: Debian ophalen, llama.cpp bouwen en de modellen downloaden.

Liever eerst kijken wat er gebeurt, of meer rekenkracht meegeven:

```bash
# niets aanmaken, alleen de kit ophalen en vertellen wat er zou gebeuren
DROOG=1 bash -c "$(curl -fsSL https://raw.githubusercontent.com/Theberas04/werdens-media-llm/main/deploy/proxmox/install.sh)"

# 6 cores, 16 GB en een vast adres
CORES=6 MEMORY=16384 DISK=80 STATIC_IP=192.168.1.100/24 GATEWAY=192.168.1.1 \
  bash -c "$(curl -fsSL https://raw.githubusercontent.com/Theberas04/werdens-media-llm/main/deploy/proxmox/install.sh)"
```

Zelf de bestanden in handen houden kan ook: `git clone` de repo en draai
`bash deploy/proxmox/create-werdens-media-llm-lxc.sh`. Of stap voor stap, in een
bestaande container:

```bash
bash deploy/proxmox/provision-werdens-media-llm.sh <CTID>
bash deploy/proxmox/check-werdens-media-llm.sh <CTID>
```

**Start de container niet** en zie je alleen `Failed to spawn container`? Die regel
zegt niet waaróm. Draai dan dit op de host; het haalt de echte oorzaak op en zegt
wat je moet doen:

```bash
CTID=105 bash -c "$(curl -fsSL https://raw.githubusercontent.com/Theberas04/werdens-media-llm/main/deploy/proxmox/diagnose-werdens-media-llm.sh)"
```

Instellingen gaan via env-variabelen vóór het commando: `CORES`, `MEMORY`, `DISK`,
`STATIC_IP`, `GATEWAY`, `CT_HOSTNAME`. Het inrichtscript kiest het chatmodel zelf
met de gemeten cijfers; wil je een eigen keuze, zet dan `WMLM_MODEL_REPO` en
`WMLM_MODEL_FILE`, of `SKIP_MODELS=1` om het downloaden over te slaan.

## API

De server praat twee talen op dezelfde poort, zodat bestaande software zonder
aanpassing werkt.

**Ollama-vorm** — `/api/version`, `/api/tags`, `/api/ps`, `/api/show`,
`/api/pull`, `/api/chat`, `/api/generate`, `/api/embed`

**OpenAI-vorm** — `/v1/models`, `/v1/chat/completions`, `/v1/completions`,
`/v1/embeddings`

Daarbovenop twee eigen lagen, elk op hun eigen poort:

**Werkruimtelaag (8090)** — documenten in een werkruimte met bronvermelding:

- `GET|POST /api/workspaces`, `GET /api/workspaces/<slug>/documents`
- `POST /api/workspaces/<slug>/documents` (plak tekst) en `.../documents/bestand` (haal een webpagina op)
- `POST /api/workspaces/<slug>/query` — antwoord met bronnen
- `POST /v1/chat/completions` met `model="werkruimte:<slug>"` — dezelfde vraag in OpenAI-vorm
- `POST /api/workspaces/<slug>/sleutels` — API-sleutel voor je eigen apps

**Medialaag (8091)** — spraak, en beeld zodra ComfyUI is aangesloten:

- `POST /v1/audio/transcriptions` — spraakbericht naar tekst (multipart of rauw bestand)
- `POST /v1/audio/speech` — tekst naar spraak, Nederlandse stem
- `GET /v1/voices`, `GET /health`
- `POST /v1/images/generations` — beeld (vraagt `WMLM_COMFY_URL`)
- `GET /` — webpagina met microfoonopname en voorleesknop

```bash
# chat, Ollama-vorm met streaming
curl http://192.168.1.100:11434/api/chat -d '{
  "model": "Llama-3.2-3B-Instruct-Q4_K_M",
  "messages": [{"role": "user", "content": "Noem drie Nederlandse provincies."}],
  "stream": true
}'

# chat, OpenAI-vorm
curl http://192.168.1.100:11434/v1/chat/completions -d '{
  "model": "Llama-3.2-3B-Instruct-Q4_K_M",
  "messages": [{"role": "user", "content": "Hallo"}]
}'

# spraak uit, en datzelfde spraakbericht weer terug naar tekst
curl -s http://192.168.1.100:8091/v1/audio/speech \
  -d '{"input":"Dit is de Werdens Media LLM."}' -o hallo.wav
curl -s http://192.168.1.100:8091/v1/audio/transcriptions \
  -F file=@hallo.wav | jq -r .text
```

## CLI

```bash
python3 engine/wllm.py serve        # server starten
python3 engine/wllm.py list         # modellen met grootte, quantisatie en digest
python3 engine/wllm.py ps           # wat er nu geladen is
python3 engine/wllm.py aanbevelen            # welk model past op deze machine?
python3 engine/wllm.py aanbevelen --meten    # dezelfde vraag, met echte cijfers
python3 engine/wllm.py model-info <model>    # architectuur en geheugengebruik
python3 engine/wllm.py pull hf:unsloth/Llama-3.2-3B-Instruct-GGUF/Llama-3.2-3B-Instruct-Q4_K_M.gguf
python3 engine/wllm.py run Llama-3.2-3B-Instruct-Q4_K_M "Wat is een LLM?"
```

## Koppelen

**Hermes Agent** (per sessie, zonder je configuratie te veranderen):

```bash
hermes chat -q "vraag" -m Llama-3.2-3B-Instruct-Q4_K_M \
  --provider custom --base-url http://192.168.1.100:11434/v1 --api-key wmlm-local
```

Twee dingen om te weten voordat je dit doet:
- Hermes eist een contextvenster van **minimaal 64.000 tokens**. Zit je daaronder,
  dan weigert hij met een duidelijke melding — dat is geen storing.
- Op een CPU zonder GPU is dit **traag**: een prompt van 14.000 tokens duurt al
  snel tien minuten. Bruikbaar voor losse vragen en documentvragen, niet als
  agent-brein. Met een GPU (12 GB of meer) verandert dat volledig.

**Je eigen apps**: alles wat OpenAI of Ollama praat, kan tegen deze server. Geen
API-sleutel nodig en er verlaat niets je eigen netwerk.

## Welk model past op jouw machine?

Dit is de knop voor kwaliteit: hoe groter het model, hoe beter het antwoord. Maar
grootte is niet gratis — bij 64k context (wat Hermes eist) kost de KV-cache
alleen al ettelijke gigabytes.

De server meet het zelf, uit de architectuur in de GGUF-kop:

```bash
python3 engine/wllm.py aanbevelen --meten
```

Dat haalt per model ongeveer 1 MB op (de kop, niet het hele bestand) en rekent
uit wat er past. Gemeten voorbeeld op een CPU zonder GPU met 8 GB en 64k context:

| Model | Gewichten | KV-cache | Nodig | Past in 5.825 MB? |
|---|---|---|---|---|
| Qwen3-32B Q4_K_M | 18.846 MB | 8.192 MB | 27.038 MB | nee |
| Qwen3-30B-A3B Q4_K_M | 17.697 MB | 3.072 MB | 20.769 MB | nee |
| Qwen3-14B Q4_K_M | 8.584 MB | 5.120 MB | 13.704 MB | nee |
| Qwen3-8B Q4_K_M | 4.794 MB | 4.608 MB | 9.402 MB | nee |
| Qwen3-4B Q4_K_M | 2.381 MB | 4.608 MB | 6.989 MB | nee |
| **Llama-3.2-3B Q4_K_M** | 1.925 MB | 3.584 MB | 5.509 MB | **ja** |
| Llama-3.2-1B Q4_K_M | 770 MB | 1.024 MB | 1.794 MB | ja |

De uitkomst is niet wat je zou verwachten: de **Llama-3.2-familie** heeft 28
lagen met kop-lengte 128, Qwen3 heeft 36 lagen met dezelfde kop-lengte. Daardoor
is de KV-cache van Llama-3.2 veel kleiner en past Llama-3.2-3B bij 64k waar
Qwen3-4B en 8B niet passen. Op een gewone CPU met 8 GB is Llama-3.2-3B dus het
beste dat je kunt draaien.

Wil je echt kwaliteit (Qwen3-8B of groter), dan is er geen instelling die dat
oplost: er moet geheugen bij, of een GPU. Op een GPU telt alleen het
videogeheugen en is de KV-cache f16, waardoor dezelfde modellen wél passen.

## Configuratie

| Variabele | Default | Betekenis |
|---|---|---|
| `WLLM_HOME` | projectmap | basis voor modellen, logs en manifests |
| `WLLM_MODELS` | `$WLLM_HOME/models` | waar de `.gguf`-bestanden staan |
| `WLLM_ENGINE` | `$WLLM_HOME/engine/llama-b12262/llama-server` | pad naar de engine |
| `WLLM_HOST` / `WLLM_PORT` | `0.0.0.0` / `11434` | luisteradres |
| `WLLM_CTX` | `65536` | contextvenster (Hermes eist minimaal 64.000) |
| `WLLM_KV_TYPE` | `q8_0` | KV-cache-type; 8-bits halveert het geheugen |
| `WLLM_KEEP_ALIVE` | `300` | seconden voordat een ongebruikt model wordt ontladen |
| `WLLM_THREADS` | aantal CPU's | rekenkracht-threads |
| `WLLM_GEREEDSCHAP` | `1` | weer, nieuws, naslag, zoeken en tijd aan of uit (`0`) |
| `WLLM_GEREEDSCHAP_PLAATS` | leeg | plaats als er geen in de vraag staat |
| `WLLM_GEREEDSCHAP_TIMEOUT` | `15` | seconden voordat een ophaalpoging wordt opgegeven |

De werkruimtelaag (`workspace/config.env`) en de medialaag (`media/config.env`)
hebben hun eigen bestand:

| Variabele | Default | Betekenis |
|---|---|---|
| `WMLM_ENGINE_URL` | `http://127.0.0.1:11434` | waar de werkruimtelaag de engine vindt |
| `WMLM_DATA` | `$WMLM_HOME/data` | de SQLite-database met documenten en vectoren |
| `WMLM_CHAT_MODEL` / `WMLM_EMBED_MODEL` | gekozen bij installatie | welk model antwoordt en welk model vectoren maakt |
| `WMLM_PORT` | `8090` | poort van de werkruimtelaag |
| `WMLM_MEDIA_PORT` | `8091` | poort van de medialaag |
| `WMLM_WHISPER_MODEL` | `ggml-small.bin` | groter is nauwkeuriger maar langzamer op CPU |
| `WMLM_PIPER_STEM` | `nl_NL-mls-medium.onnx` | welke Nederlandse stem Piper gebruikt |
| `WMLM_COMFY_URL` | leeg | ComfyUI-adres voor beeldgeneratie; zonder dit geeft die route een uitleg |

## Architectuur

```
Hermes Agent · je eigen apps · spraakberichten · embed-widget
        │                        │                      │
        │ :11434                 │ :8090                │ :8091
        ▼                        ▼                      ▼
  engine/wllm.py        workspace/wmlm_workspace.py   media/wmlm_media.py
  Ollama + OpenAI       werkruimtes, documenten        spraak naar tekst
  modellen, pull,       met bronvermelding,            (whisper.cpp) en
  warm houden           vectordatabase op SQLite       spraak uit (Piper)
        │                        │                      │
        ▼                        └──────────┬───────────┘
  llama-server (llama.cpp)                  │
        ▲───────────────────────────────────┘
        (de werkruimte vraagt embeddings en antwoorden aan de engine)
```

De lagen kunnen niet zonder de engine: die levert het rekenwerk. Alles blijft in
je eigen netwerk en er gaat geen verzoek naar buiten.

## Status en roadmap

Zie [docs/ROADMAP.md](docs/ROADMAP.md). Kort: de engine, de werkruimtelaag en de
medialaag werken en zijn getest op een CPU-only machine (4 vCPU, 7,6 GB, geen
GPU). De drie lagen draaien als aparte diensten. Beeldgeneratie en video staan
nog open.

## Licentie

Alle rechten voorbehouden. Dit is eigen code; gebruik, kopiëren of verspreiden
alleen met toestemming van de maker. Gebruikte onderdelen van derden houden hun
eigen licentie — zie [docs/ROADMAP.md](docs/ROADMAP.md) voor de details.
