# WorkPortal – De Vreugd Productietechniek

Versie 2 (1.1.0) · Python (Flask) · SQLite · Docker

Modules: Klanten & projecten · Service & onderhoud · Druktestregistratie ·
Calculatie · Na-calculatie · Kenniscentrum · Beheer (gebruikers, functierollen, rechten).

## Mappen

```
workportal-app-V2/          <- inhoud van de zip (bestanden staan direct in de root)
├── publiceren.cmd          <- naar GitHub publiceren
├── Dockerfile
├── docker-compose.yml      <- stack voor Portainer
├── docker-entrypoint.sh
├── requirements.txt
├── wsgi.py
└── workportal/             <- de applicatie
    ├── *.py                <- modules
    ├── templates/          <- schermen
    └── static/             <- css, js, logo's
```

Alle gegevens (database, foto's, PDF's, back-ups) staan in het Docker-volume
`workportal-data` (in de container: `/data`). Updaten via "Pull and redeploy"
laat die gegevens staan. Databasewijzigingen worden bij het opstarten
automatisch en zonder dataverlies doorgevoerd.

## Installeren (zelfde werkwijze als Cheese Stock Manager)

1. Pak de zip uit en zet de bestanden in de hostmap / Git-repository
   `WorkPortal` en push naar Git.
2. Portainer → Stacks → Add stack → **Repository** → kies de Source van deze repo.
   Stacknaam: `workportal`.
3. Environment variables invullen in de Portainer-UI:

| Variabele | Verplicht | Voorbeeld / uitleg |
|---|---|---|
| `APP_URL` | ja | `https://workportal.devreugd-pt.nl` |
| `ADMIN_EMAIL` | ja | E-mailadres van de eerste beheerder (wordt bij de eerste start aangemaakt) |
| `ADMIN_NAME` | nee | `Cees de Vreugd` |
| `SECRET_KEY` | ja | Lange willekeurige tekst (min. 40 tekens). Niet meer wijzigen: anders is iedereen uitgelogd |
| `GRAPH_TENANT_ID` | ja* | Directory (tenant) ID van de app-registratie |
| `GRAPH_CLIENT_ID` | ja* | Application (client) ID van de app-registratie |
| `GRAPH_CLIENT_SECRET` | ja* | Client secret (de *Value*, niet het Secret ID) |
| `MAIL_FROM` | ja* | Mailbox waaruit verstuurd wordt, bijv. `noreply@devreugd-pt.nl` |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASS` / `SMTP_SECURITY` | nee | Alleen als alternatief voor de app-registratie |
| `PA_SHAREPOINT_URL` | nee | HTTP-URL van de flow "Document naar SharePoint" |
| `PA_NACALC_URL` | nee | HTTP-URL van de flow "Na-calculatie ophalen" |
| `PA_SECRET` | nee | Gedeelde sleutel; de app stuurt hem mee als header `x-workportal-key` |

\* Zonder mailinstellingen werkt de app in **testmodus**: de inlogcode verschijnt dan in het
containerlog (Portainer → Containers → `workportal-app` → Logs).

4. Klik op Deploy the stack. Controle: `https://<adres>/health` geeft `{"status":"ok"}`.
5. Nginx Proxy Manager → Proxy Host toevoegen:
   - Domain: `workportal.devreugd-pt.nl` (DNS moet naar het publieke IP wijzen)
   - Forward Hostname: `workportal-app`, Forward Port: `3000`, scheme `http`
   - SSL: Let's Encrypt, **Force SSL**, HTTP/2 aan
   - Tabblad Advanced (voor grote foto-uploads):
     `client_max_body_size 60m;`
6. Inloggen met `ADMIN_EMAIL` → code uit de mail (of het log) → pincode instellen.
   Daarna in **Beheer** de gebruikers aanmaken met hun functierol.

## Publiceren / updaten

1. Pak de nieuwe zip uit in `WorkPortal-App` (bijv. `WorkPortal-App\workportal-app-V2`).
2. Dubbelklik op **`publiceren.cmd`** in die map. Het script:
   - haalt de eerste keer de repository `https://github.com/CeesdeVreugd/workportal-app.git`
     op in de hostmap `WorkPortal-App\WorkPortal`;
   - kopieert de nieuwe bestanden naar de hostmap (oude bestanden worden opgeruimd);
   - commit en pusht naar GitHub.
3. Portainer → stack `workportal` → **Pull and redeploy** (met "Re-pull image and redeploy").
   Gegevens blijven staan.

## E-mail via app-registratie (Microsoft 365)

WorkPortal verstuurt mail (inlogcodes, werkbonnen, herinneringen) via Microsoft Graph.

1. [Microsoft Entra-beheercentrum](https://entra.microsoft.com) → Identiteit → Toepassingen →
   **App-registraties** → Nieuwe registratie. Naam: `WorkPortal`, alleen deze organisatie,
   geen redirect-URI.
2. **API-machtigingen** → Machtiging toevoegen → Microsoft Graph → **Toepassingsmachtigingen** →
   `Mail.Send` → toevoegen → **Beheerderstoestemming verlenen**.
3. **Certificaten en geheimen** → Nieuw clientgeheim (bijv. 24 maanden). Kopieer direct de
   *Waarde*. Zet een herinnering in de agenda voor de vervaldatum.
4. Noteer van het Overzicht: *Toepassings-id (client)* en *Map-id (tenant)*.
5. Maak (of kies) een mailbox om uit te versturen, bijv. `noreply@devreugd-pt.nl`
   (een gedeelde mailbox zonder licentie volstaat).
6. Vul in Portainer in: `GRAPH_TENANT_ID`, `GRAPH_CLIENT_ID`, `GRAPH_CLIENT_SECRET`, `MAIL_FROM`
   en doe **Update the stack**.
7. WorkPortal → Beheer → Instellingen → **Testmail naar mij**.

Aanbevolen (IT): beperk de app zodat hij alleen uit die ene mailbox mag versturen
(Exchange Online: *RBAC for Applications* of `New-ApplicationAccessPolicy`). Zonder beperking
mag een app met `Mail.Send` uit elke mailbox in de organisatie mailen.

Lukt versturen niet, dan staat de reden in het containerlog
(`E-mail via Microsoft 365 mislukt ...`). Veelvoorkomend: geen beheerderstoestemming,
verkeerde secret (Secret ID i.p.v. Waarde) of `MAIL_FROM` bestaat niet.

## SharePoint: projectmappen koppelen

WorkPortal koppelt projecten aan de mappen in `Projecten/<Klantmap>/<ordernummer>-<omschrijving>`:

- Een nieuwe ordermap (8 cijfers, streepje, omschrijving) wordt binnen ~3 minuten een project,
  bij de relatie die aan de klantmap is gekoppeld. Bestaat het nummer al, dan wordt het gekoppeld.
- Een project dat in WorkPortal wordt aangemaakt krijgt zo'n map in de klantmap. De bestaande
  Power Automate-flow herkent die en kopieert het sjabloon en plaatst de Teams-post.
- Monteurs zien de projectmap alleen-lezen in WorkPortal (project, ticket en druktest) en hebben
  geen SharePoint-toegang nodig. Werkbonnen en druktestrapporten gaan direct in de projectmap.

**Eenmalig door IT (zelfde app-registratie `WorkPortal`):**

1. App-registratie → **API-machtigingen** → Microsoft Graph → **Toepassingsmachtigingen** →
   `Sites.Selected` → toevoegen → **Beheerderstoestemming verlenen**.
   (`Sites.Selected` geeft zelf nog nergens toegang; alleen tot sites uit stap 2.)
2. Geef de app **schrijfrecht op de site** waar de Projecten-map staat. Met PnP PowerShell:
   ```powershell
   Connect-PnPOnline -Url https://<tenant>.sharepoint.com/sites/<site> -Interactive -ClientId <PnP-app-id>
   Grant-PnPAzureADAppSitePermission -AppId <client-id van WorkPortal> -DisplayName "WorkPortal" `
     -Site https://<tenant>.sharepoint.com/sites/<site> -Permissions Write
   ```
   Of in Graph Explorer (als beheerder, met toestemming `Sites.FullControl.All`):
   `GET https://graph.microsoft.com/v1.0/sites/<tenant>.sharepoint.com:/sites/<site>` → neem `id`, dan
   `POST https://graph.microsoft.com/v1.0/sites/<id>/permissions` met body
   `{"roles":["write"],"grantedToIdentities":[{"application":{"id":"<client-id>","displayName":"WorkPortal"}}]}`.

**Daarna:**

3. Portainer: `GRAPH_TENANT_ID`, `GRAPH_CLIENT_ID`, `GRAPH_CLIENT_SECRET` invullen → Update the stack.
   Laat `MAIL_FROM` leeg zolang `Mail.Send` nog niet is goedgekeurd: dan blijven de inlogcodes
   in het containerlog staan en werkt SharePoint al wel.
4. WorkPortal → Beheer → **SharePoint** → plak de link van de Projecten-map (adresbalk van de
   browser) → **Verbinden en synchroniseren**. Kies of bestaande ordermappen als *actief* of
   *afgerond* project binnenkomen.
5. Controleer onder *Klantmappen zonder relatie* welke mappen niet automatisch op naam zijn
   herkend en koppel ze aan de juiste relatie.

Foutmelding 403 bij verbinden = stap 2 ontbreekt of is voor een andere site gedaan.

## Orders uit Komdex (actievenster "Nieuwe orders")

Komdex maakt bij een nieuwe order een map aan op de server. De Docker-VM zit in een ander VLAN, dus
WorkPortal leest die map niet zelf uit. Het script `scripts/komdex-orders.ps1` draait als geplande taak
op een server die wél bij de Komdex-map kan en stuurt elke 5 minuten de lijst met ordermappen via HTTPS
naar WorkPortal (alleen lezen, er wordt niets gewijzigd).

1. Bedenk een lange willekeurige sleutel (bijv. 40 tekens) en zet die in Portainer als `KOMDEX_KEY`
   → Update the stack.
2. Kopieer `scripts/komdex-orders.ps1` naar de server (bijv. `C:\Scripts\`) en vul bovenin in:
   `$OrderMap` (de Komdex-ordermap), `$Sleutel` (= `KOMDEX_KEY`) en eventueel `$Jaren`
   (aantal recente jaarmappen dat wordt bekeken, standaard 2).
3. Test: `powershell -ExecutionPolicy Bypass -File C:\Scripts\komdex-orders.ps1`. In
   `komdex-orders.log` staat `OK: … ordermappen`. De eerste keer worden alle bestaande mappen alleen
   onthouden; pas mappen die daarna verschijnen komen in het actievenster.
4. Taakplanner: elke 5 minuten, "uitvoeren ongeacht of gebruiker is aangemeld", account met leesrecht.
5. De server moet `https://workportal.devreugd-pt.nl` kunnen bereiken. Staat er in Nginx Proxy Manager een
   IP-toegangslijst op de host, voeg dan het IP van deze server toe.

Nieuwe mappen verschijnen onder **Orders → Nieuwe orders** (en op het dashboard). Gebruikers met de rol
Verkoop-Inkoop-WVB of Directie krijgen een push-/mailmelding. Daar kies je **order** of **project**, de klant
en de omschrijving; WorkPortal maakt dan de map in de klantmap in SharePoint:

- Project: direct een map `<ordernummer>-<omschrijving>` → Power Automate zet het sjabloon erin en plaatst de Teams-post.
- Order: géén map, tot iemand op de orderpagina op **Ordermap aanmaken** klikt; dan
  ook `<ordernummer>-<omschrijving>` (sinds V64 altijd met streepje, bijv. `20260148-Ontwerp, levering en aanpassen spuitjes`).
  Of iets een project of order is, onthoudt WorkPortal zelf; de mapnaam bepaalt dat niet meer. Oude ordermappen met
  een spatie blijven gewoon werken.

Ordertypes (Leeg, Offerte, Particulier, Handel, Service / onderhoud, Engineering, Speciaal Machinebouw,
Constructie/Plaatwerk, Leidingwerk, WBSO, Garantie, Intern, Standaard Machine - PalletRotator) staan
onderaan **Orders → Nieuwe orders** met per type: Project, Order of Vragen. Controleer die instelling eenmalig.

Bij projecten en orders kun je **gespreksnotities** vastleggen (datum, soort, met wie, notitie en eventueel
een actiedatum; open acties staan op je dashboard). In de lijsten kun je met vinkjes meerdere projecten
in één keer omzetten naar order, of andersom.

Controleer eenmalig dat de Power Automate-flow een map als `20260138 Omschrijving` níet oppakt.

**Orderbon: automatisch verwerken.** Staat er in de ordermap een PDF met "orderbon" in de naam (de
orderbon uit Komdex), dan stuurt het script die mee. WorkPortal leest ordernummer, ordertype, omschrijving,
klant, uitvoerder, order- en leverdatum en de werkomschrijving uit. Is het ordertype bekend als project of
order (in te stellen onderaan **Orders → Nieuwe orders**, of met het vinkje "voortaan automatisch" bij het
goedzetten) en is de klant bekend met een klantmap, dan maakt WorkPortal het project of de order zelf aan,
met map. Anders komt hij in het actievenster, met de gegevens van de bon al ingevuld. Een melding gaat pas
de deur uit als er na 10 minuten nog steeds actie nodig is. De werkomschrijving en de orderbon zijn te zien
bij het project/de order en bij gekoppelde servicetickets. Ook bestaande ordermappen (van vóór de koppeling) waar een orderbon
in staat, komen in WorkPortal: als order of project volgens het ordertype (onbekend = order), zonder map in
SharePoint en zonder melding. Het overzicht van alle mappen staat onder **Beheer → DC01**.

### Vinkjes op de orderbon
Bij "Ordereigenschappen" leest WorkPortal de vinkjes Vaste prijs, Geleverd, Afgesloten, Afgefactureerd en Vervallen
(in de PDF kleine plaatjes; een vinkje = donkere pixels). Ze staan bij de order/het project (Ordergegevens), in het
actievenster, in Beheer > DC01 en bij de na-calculatie ("Geen vaste prijs (regie)" als dat vinkje ontbreekt).
Wordt de orderbon in de map opnieuw opgeslagen (andere datum/grootte), dan haalt het script hem opnieuw op en worden
de vinkjes bijgewerkt; er wordt dan niet opnieuw geprint.

### Na-calculatie uit de ordermap
Staat er in een ordermap op DC01 een Excel waarvan de naam begint met **Nacalculatie** (ook de schrijfwijze
*Nacacalculatie*; .xlsx/.xlsm), dan stuurt het script de nieuwste daarvan mee en leest WorkPortal hem in als
na-calculatie van die order (zelfde import als handmatig uploaden). Een gewijzigd of nieuwer bestand wordt opnieuw
ingelezen; een onleesbaar bestand wordt onthouden en niet elke run opnieuw geprobeerd. Maximaal 20 per run, zodat
de eerste keer niet alles tegelijk komt. Overzicht en "opnieuw ophalen": Beheer > DC01, filter *Met na-calculatie*.
Werk hiervoor `komdex-orders.ps1` op DC01 bij (de instellingen bovenaan blijven gelijk).

## 3D-modellen

Module **3D-modellen** (rechten: Lezen = bekijken en meten, Bewerken = uploaden, Beheer = verwijderen).
Menu **Uitvoering → 3D-viewer**, en het blok *3D-modellen* bij elk project, elke order en elk serviceticket.

**Waar staan de modellen?**
- In SharePoint: alle 3D-bestanden in de map **1 Tekeningen** van de project-/ordermap (ook in submappen).
  De naam van die submap is aan te passen onder Beheer → SharePoint. Bestaat de map niet, dan wordt de hele
  projectmap doorzocht.
- Uploaden in WorkPortal: bij een project/order met SharePoint-map gaat het bestand naar **1 Tekeningen** in
  SharePoint; bij een project/order zonder map en bij een serviceticket wordt het in `/data/uploads` bewaard.
  Een ticket toont ook de modellen van het gekoppelde project.
- Via **3D-viewer → Bestand van deze computer openen** bekijk je een bestand zonder het te uploaden.

**Formaten:** STEP (.step/.stp, AP203/AP214 met kleuren), IGES (.iges/.igs), STL, OBJ en 3MF.
Maximaal 60 MB per upload (Nginx `client_max_body_size 60m`); bij een groter bestand geeft WorkPortal een
nette melding. Bestanden die al in SharePoint staan mogen groter zijn (ze worden gestreamd).

**De viewer:** draaien/zoomen/pannen (muis of vingers), standaardaanzichten (passend, voor, boven, rechts),
doorsnede langs X/Y/Z met schuif en omdraaien, onderdelen tonen/verbergen of één onderdeel isoleren,
randen, orthografisch, volledig scherm, en meten:
- **Punt–punt:** afstand plus ΔX/ΔY/ΔZ in mm.
- **Vlak–vlak:** klik twee vlakken; het hele vlak wordt herkend en gemarkeerd. Evenwijdig → loodrechte afstand,
  anders de hoek plus de kleinste afstand tussen de twee vlakken.
- **Lijn–lijn:** klik vlak bij twee rechte randen; de rand wordt over de volle lengte herkend. Evenwijdig → afstand
  tussen de lijnen, anders de hoek plus de kleinste afstand tussen de randen.
- **Diameter:** één klik op een ronde rand (bijv. het kopvlak van een buis of de rand van een gat) of op het ronde
  vlak zelf geeft Ø en R. Bij een deel van een cirkel (afronding, boog) wordt de radius getoond.
Alleen echte randen tellen bij het meten: de hulplijnen tussen de facetten van een rond vlak zijn niet aan te klikken
(een ronde rand of rond vlak meet je met Diameter). Alleen de gekozen meting staat in beeld; de laatste 10 staan in
de lijst en zijn met een klik weer te tonen.
Op een groot scherm staat de onderdelenboom altijd rechts (met doorsnede of metingen eronder).

**Grote bestanden op het apparaat:** modellen vanaf een instelbare grootte (Beheer > SharePoint, standaard 10 MB)
worden één keer gedownload (met voortgang in %) en in de opslag van de browser op de pc, telefoon of iPad bewaard
(Cache Storage). Daarna opent de viewer ze vanaf het apparaat. De sleutel bevat de versie uit SharePoint (cTag/eTag),
dus een gewijzigde STEP wordt automatisch opnieuw opgehaald en de oude versie opgeruimd. Op de pagina 3D-viewer
staat wat er op dit apparaat bewaard is, met een knop om alles te verwijderen. Een browser mag niet zomaar in een
eigen map op C:\ schrijven; de opslag staat in het browserprofiel op het apparaat. In een privévenster of bij een
volle schijf wordt het model gewoon rechtstreeks geladen.
Werkt op pc, tablet en telefoon (op de telefoon opent het paneel onderin).

**Techniek (geen licentiekosten):** [Online3DViewer](https://github.com/kovacsv/Online3DViewer) 0.18 (MIT,
three.js ingebouwd) staat in `workportal/static/3d/o3dv.min.js`. STEP/IGES worden gelezen met
occt-import-js 0.0.24 (OpenCascade, WASM; LGPL-2.1 met uitzondering, licenties in dezelfde map). Die bestanden
staan in de repository in `workportal/static/3d/occt/` en worden door WorkPortal zelf geserveerd (geen CDN in de
browser). Ontbreken ze, dan probeert `scripts/fetch_3d_libs.py` ze tijdens `docker build` op te halen. Zonder die
bestanden werken STL/OBJ/3MF wel, STEP/IGES niet (de 3D-viewer-pagina meldt dat).

Opent een model niet, dan toont de viewer de reden plus de technische melding van de lezer (ophalen mislukt,
geen vlakken in het bestand, of de STEP-lezer kan het bestand niet verwerken).

**Niet mogelijk (buiten scope):**
- SolidWorks (.sldprt/.sldasm) en eDrawings (.easm/.eprt/.edrw) kunnen niet worden geopend. Exporteer vanuit
  SolidWorks als **STEP AP214** met **Export face/edge properties** aan, dan blijven de kleuren behouden.
- Exact meten van gatdiameters en radiussen: er wordt gemeten op het beeldmodel (driehoeken). De diameter wordt
  door de hoekpunten gepast en is daardoor nauwkeurig, maar blijft een benadering van de CAD-maat. De doorsnede is open (geen dichte snijvlakken).

**Grote STEP-bestanden op de server omzetten:** STEP/IGES vanaf de ingestelde grootte (Beheer > SharePoint) worden
één keer op de server omgezet naar GLB met Node.js + dezelfde OpenCascade-lezer (`scripts/step2glb.js`, zit in de
container). Dat gebeurt zodra iemand het model opent of na een upload, op de
achtergrond, één model tegelijk (max. 30 min per model, `WP3D_CONVERT_TIMEOUT`; geheugen `WP3D_NODE_HEAP_MB`).
Het resultaat staat in `/data/3d-cache` (na 120 dagen niet gebruikt opgeruimd) en opent daarna voor iedereen in
seconden, ook op telefoon en iPad. Een gewijzigde STEP in SharePoint krijgt een nieuwe versie en wordt opnieuw
omgezet. Lukt omzetten niet (bijv. te weinig geheugen), dan leest de browser de STEP zoals voorheen.
Veiligheid: de container heeft een geheugengrens (`WP_MEM_LIMIT`, standaard `2g`, zonder swap). Vraagt een model
meer, dan stopt alleen het omzetten; WorkPortal, Nginx en Portainer blijven draaien. Omzetten draait met lage
prioriteit, alleen als iemand het model opent (of na upload), en alleen voor bestanden tot `WP3D_CONVERT_MAX_MB`
(standaard 150). Is een poging halverwege afgebroken, dan wordt dat model 6 uur niet opnieuw geprobeerd.
Helemaal uitzetten: `WP3D_CONVERT=0` in de stack.

**Hele grote samenstellingen: 3MF naast de STEP.** Een STEP groter dan `WP3D_STEP_MAX_MB` (standaard 80 MB) wordt
niet meer ingelezen; de viewer vraagt om vanuit SolidWorks ook een 3MF op te slaan (Opslaan als → 3D Manufacturing
Format) met dezelfde naam in dezelfde map. Staat die er, dan opent "Bekijk 3D" bij de STEP automatisch de 3MF
(`?step=1` forceert de STEP). Let op: SolidWorks zet in de 3MF alleen kleuren als de uiterlijken meegaan.

**Oriëntatie:** knop "Boven: Y/Z" in de werkbalk wisselt welke as omhoog wijst (Y, Z, -Y, -Z). Standaard Z voor
3MF/STL/OBJ en Y voor STEP/IGES; de keuze wordt per bestand en per bestandstype onthouden op het apparaat.
**Soepel draaien:** bij grote modellen tekent de viewer tijdens slepen/zoomen in lagere resolutie en bij stilstand
weer scherp.

## Back-ups

- Elke nacht om 02:00 maakt de app een kopie van de database in `/data/backups`
  (laatste 14 bewaard).
- Beheer → Instellingen → "Database-back-up downloaden" voor een directe kopie.
- Voor een volledige back-up ook de map `/data/uploads` (foto's en PDF's) meenemen,
  bijvoorbeeld het volume `workportal-data` met de bestaande back-upoplossing van IT.

## Power Automate-flows

**Document naar SharePoint** (trigger: *When an HTTP request is received*, methode POST)
- Body-velden: `bestandsnaam`, `map`, `projectnummer`, `type`, `inhoudBase64`
- Controleer eerst of header `x-workportal-key` gelijk is aan `PA_SECRET`.
- Actie *SharePoint – Create file*: map = `map`, naam = `bestandsnaam`,
  inhoud = `base64ToBinary(triggerBody()?['inhoudBase64'])`.
- *Response* met status 200.

**Na-calculatie ophalen** (trigger: *When an HTTP request is received*, POST)
- Body-veld: `ordernummer`; controleer header `x-workportal-key`.
- Actie *File System – Get file content* (via de on-premises data gateway) op het
  regie-exportbestand van die order in de ERP-map.
- *Response* met status 200 en body
  `{"bestandsnaam": "...xlsx", "inhoudBase64": "@{base64(body('Get_file_content'))}"}`.

Zonder deze flows werkt alles ook: PDF's zijn te downloaden in de app en de
regie-Excel kan handmatig worden geüpload bij Na-calculatie.

## Rechten

Per functierol en module: **Geen · Lezen · Bewerken · Beheer** (Beheer = ook verwijderen).
Relaties, Projecten en Orders zijn aparte modules met eigen rechten.
Aan te passen in Beheer → Rechten per functierol. Een gebruiker kan meerdere functierollen
hebben; per module geldt dan het hoogste recht van die rollen. Per gebruiker kunnen extra rechten
worden gegeven. Een gebruiker met "Beheerder" aangevinkt heeft overal alle rechten.

## Inloggen

E-mailadres → code van 6 cijfers per mail (10 minuten geldig) → pincode per apparaat.
Elke 14 dagen opnieuw een e-mailcode (instelbaar). Na 5 foute pincodes is weer een
e-mailcode nodig. Na 12 uur vergrendelt de app automatisch (instelbaar).


## Printen via Printix
Orderbonnen kunnen automatisch (nieuwe orders uit DC01) of met de hand (Beheer > DC01 of de knop *Printen* bij een
order/project) naar een A4-printer in Printix. WorkPortal gebruikt de Printix Cloud Print API:
1. Printix Administrator: maak bij de Cloud Print API een set API-gegevens (client ID en secret). Vereist Printix
   Premium; Secure Print mag niet op "Alle gebruikers moeten veilig printen" staan (gebruik groepen).
2. Portainer (stack): `PRINTIX_TENANT_ID`, `PRINTIX_CLIENT_ID`, `PRINTIX_CLIENT_SECRET`. Het secret alleen in Portainer.
3. WorkPortal > Beheer > Printen: printer kiezen, aantal, dubbelzijdig/kleur, automatisch printen aan, testpagina.
Bestaande ordermappen worden nooit automatisch geprint; elke orderbon hoogstens één keer automatisch.

## PDF's
PDF's (rapporten, werkbonnen, orderbonnen, PDF's in de projectmap) openen in een eigen scherm met terug-knop,
downloaden en "in nieuw venster" (om te printen). Op Android opent de PDF-app van de telefoon.


## Na-calculatie: Excel-calculatie en Standaard machine
- **Calculatie uit Excel:** bij een na-calculatie kan het eigen calculatieblad (Excel met 'Tabblad 01', 'Tabblad 02',
  kolommen Aantal / Prijs per stuk / marge / Totaal / Inclusief marge) worden geüpload. De onderdelen (Engineering,
  Inkopen incl. koopdelen, Werkplaats, Besturing, Transport, Montage) worden automatisch aan de items gekoppeld; de
  koppeling is aan te passen. Geldt voor alle imports van dezelfde order. Een gekozen WorkPortal-calculatie gaat voor.
- **Standaard machine / productieorder (bijv. PalletRotator):** interne productieorder, alleen om de kostprijs per
  machine te bepalen. Bovenaan kies je de soort (automatisch bij ordertype met "Standaard Machine" én klant De Vreugd Productietechniek; een verkooporder Standaard Machine aan een andere klant wordt gewoon vaste prijs of regie) en het aantal
  gebouwde machines; er wordt dan niet met een calculatie vergeleken. Per item: **Basis** (in elke machine, gedeeld
  door het aantal machines), **Versie** (onderdelen voor een type, bijv. K13/EPT; eigen aantal; zit in de
  machineprijs) of **Optie** (bijv. safety / cold storage; eigen aantal; apart boeken). Machines/uitvoeringen = basis
  + gekozen versies. Kostprijs per stuk → orderregel productieorder en kostprijs materiaalregel verkooporder;
  verkoopprijs per stuk → verkooporder. Bedragen met een klik te kopiëren. Geen winstbeoordeling.
- **Regie (geen vaste prijs):** staat op de orderbon geen vinkje bij *Vaste prijs*, dan is de order regie: de winst is
  de marge uit de regels (verkoopwaarde − kostprijs), er is geen 'extra bovenop de marge'. Automatisch via de vinkjes
  van de orderbon, of te kiezen bij Soort na-calculatie.
- Overzicht gesorteerd op ordernummer, nieuwste boven.

## Telefoonweergave
- Op een telefoon (iPhone of Android-telefoon; een tablet telt niet als telefoon) is alles compacter: kleinere letters, knoppen, invoervelden en kaarten.
- Onder **Beheer > Instellingen > Modules op de telefoon** kies je welke modules op de telefoon beschikbaar zijn. Standaard staat **Calculatie** uit; Na-calculatie staat aan.
- Een module die uitstaat verdwijnt op de telefoon uit het menu, de onderbalk en het dashboard. Wie de link toch opent, ziet de melding "niet beschikbaar op de telefoon". Staat Calculatie uit, dan toont de onderbalk **Na-calc.** in plaats van Calculatie.
- Op de pc en de tablet blijft alles gewoon beschikbaar (volgens de rechten).

## Inkoopfacturen (crediteuren)
- Eén factuurkaart per inkoopfactuur: de PDF, de uitgelezen gegevens (leverancier, factuurnummer, datums, bedragen,
  inkooporder ERP, omschrijving), de status, het gesprek en een logboek van wie wat wanneer deed.
- **Binnenkomst:** WorkPortal haalt elke 5 minuten de PDF-bijlagen op uit **factuur@devreugd-pt.nl** (Microsoft Graph,
  alleen lezen; in de mailbox verandert niets). Aanzetten onder **Beheer > Instellingen > Inkoopfacturen uit de mailbox**.
  Daarnaast kun je PDF's uploaden. Dezelfde PDF of hetzelfde factuurnummer van dezelfde leverancier wordt niet dubbel aangemaakt.
  De leverancier wordt onthouden per afzenderadres.
- **Statussen:** Nieuw → Verwerkt in ERP (administratie) → Verwerkt in SnelStart (boekhouding). Afwijzen kan iedereen met
  het recht Bewerken (directie, administratie, werkvoorbereiding), altijd met een reden. Status terugzetten kan ook.
- **Vraag stellen aan…** een collega: die krijgt een melding (push en mail) en antwoordt in WorkPortal; de vraagsteller krijgt het antwoord terug.
- **Voor de boekhouding:** een opmerking met dat vinkje komt in het filter "Voor de boekhouding" en kan worden afgehandeld.
- Filters: Alles open, Nieuw, Naar SnelStart, Vraag open, Wacht op mij, Voor de boekhouding, Afgewezen, Afgerond, Alle.
- Rechten (module "Inkoopfacturen"): standaard administratie Bewerken, directie Beheer, Verkoop-Inkoop-WVB Bewerken;
  engineering en werkplaats geen toegang. Aan te passen onder Beheer > Rechten. Wie in SnelStart boekt (boekhouding)
  krijgt toegang via zijn bestaande rol of een extra recht per gebruiker; er komen geen rollen bij.
- **Rollen liggen vast** (IT-strategie): Administratie, Directie, Verkoop-Inkoop-WVB, Engineering, Werkplaats-Service.
  Nieuwe modules krijgen rechten binnen deze rollen; voeg geen rollen toe.
- **Mailbox-recht instellen (eenmalig):** de bestaande app-registratie (GRAPH_CLIENT_ID) krijgt in Entra de
  toepassingsmachtiging **Mail.Read** (met beheerderstoestemming). Beperk dat tot alleen de factuurmailbox met een
  ApplicationAccessPolicy in Exchange Online (zie hieronder), zodat WorkPortal geen andere mailboxen kan lezen.
  ```powershell
  Connect-ExchangeOnline
  New-DistributionGroup -Name "WorkPortal mailboxen" -Type Security -PrimarySmtpAddress workportal-mailboxen@devreugd-pt.nl
  Add-DistributionGroupMember -Identity "WorkPortal mailboxen" -Member factuur@devreugd-pt.nl
  New-ApplicationAccessPolicy -AppId <GRAPH_CLIENT_ID> -PolicyScopeGroupId workportal-mailboxen@devreugd-pt.nl -AccessRight RestrictAccess -Description "WorkPortal: alleen factuurmailbox"
  Test-ApplicationAccessPolicy -Identity factuur@devreugd-pt.nl -AppId <GRAPH_CLIENT_ID>
  ```
  Let op: de afzender van WorkPortal-mails (MAIL_FROM) moet dan ook in die groep, anders werkt mailen niet meer.

## Tickets: zoekvelden, snel toevoegen en notities
- Klant, locatie, machine, contactpersoon, project en "toegewezen aan" zijn **zoekvelden**: typ een deel van de naam
  (ook serienummer of plaats). Ook bij Projecten/Orders, de Komdex-inbox en een nieuwe calculatie is de klant een zoekveld.
- Locatie, machine en contactpersoon tonen **alleen wat bij de gekozen klant hoort**; zonder klant staat er "Kies eerst een klant".
  Bij opslaan controleert WorkPortal dit ook (een machine van een andere klant wordt niet gekoppeld).
- Staat het er niet tussen? Kies in de lijst **"+ … toevoegen"**: klant, locatie, machine of contactpersoon wordt meteen
  aangemaakt (gekoppeld aan de gekozen klant) en ingevuld. Kan iedereen met Bewerken op Service of Relaties.
- Bij een ticket kun je naast bezoeken ook **notities** plaatsen (eigen notities bewerken/verwijderen; Beheer kan alles).
- **Adres zoeken:** bij een nieuwe locatie (ook in het snel-toevoegen-venster) en bij een relatie staat "Adres zoeken":
  typ postcode + huisnummer of straat en plaats, klik **Zoek adres** en kies; adres, postcode en plaats worden ingevuld
  (Nederlandse adressen via de PDOK Locatieserver van de overheid, gratis). Bij locaties staat "Op de kaart" en bij een
  ticket "Route" (Google Maps) naar het adres van de locatie, of anders van de klant.
- Bovenaan elk ticket staat altijd een **adresbalk** met het adres van de locatie (of anders van de klant) en een knop
  **Route**. Is er geen adres, dan staat er "Geen adres bekend" met een knop om het bij de klant in te vullen.
  Het adres staat ook bij Gegevens en in de ticketlijst (op de telefoon onder de omschrijving).
- Een **bezoek verwijderen** kan met het prullenbakje bij het bezoek of onderaan het bewerkscherm van het bezoek
  (met foto's). Een ondertekend bezoek kan alleen iemand met Beheer op Service verwijderen; het komt in de historie.
