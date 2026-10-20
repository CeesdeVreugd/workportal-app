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

## Bestanden slepen naar de project-/ordermap
- In de map van een project of order (knop Projectmap/Ordermap) sleep je bestanden in het vak; laat los op een submap
  om ze daarin te zetten. Of klik **Bestanden toevoegen**. Bestaat de naam al, dan maakt SharePoint er "naam 1.pdf" van
  (er wordt niets overschreven). Max. 65 MB per bestand (MAX_UPLOAD_MB). Kan iedereen met Bewerken op Projecten/Orders,
  Service of Druktesten. Mappen zelf slepen kan niet, alleen bestanden.

## Certificaten mailen
- In de map **7 Certificaten** (elke map met "certificaten" in de naam) staat **Certificaten mailen**; in de projectmap
  ook een knop **Mailen** bij die map. Je vinkt de certificaten aan (ook uit submappen), past de tekst aan en verstuurt.
- Het adres komt van de contactpersoon van de order/het project (T.a.v. op de orderbon, gezocht bij de contactpersonen
  van de klant); anders het algemene e-mailadres van de klant. Kopie aan jezelf staat standaard aan.
- Afzender: Beheer › Instellingen › "Afzender bij certificaten mailen" (leeg = MAIL_FROM), met de handtekening van
  Werkvoorbereiding. De mail komt in de gespreksnotities (soort E-mail) en de historie.
- Bijlagen samen tot 3 MB gaan direct. Groter (tot ±30 MB) vraagt in Entra het extra toepassingsrecht **Mail.ReadWrite**
  voor de app-registratie (WorkPortal maakt dan een concept met de bijlagen en verstuurt dat).
- In de adresbalk van een ticket staat naast Route een knop **Delen**: op telefoon/tablet opent het deelmenu (WhatsApp,
  mail, berichten …) met klant, locatie, adres en een kaartlink; op de pc wordt het adres met kaartlink gekopieerd.

## Tickets (V67)
- **Type ticket:** Storing, Service & onderhoud, Garantie, Inspectie, Overig (bovenaan het ticketformulier; ook filter in de lijst).
- **Afsluiten zonder handtekening:** bij een bezoek "Afronden zonder handtekening" (met optionele reden, ticket meteen
  afsluiten aangevinkt), of in het bezoekformulier de knop "Afronden zonder handtekening". De werkbon vermeldt dan
  "Niet ondertekend – afgerond op …". Een ticket zonder bezoek sluit je met **Ticket afsluiten** bovenaan.
- **Foto's:** naast "Foto maken" (camera) staat overal **Uit galerij** (bezoek en druktesten).
- De status **Gefactureerd** is vervallen; bestaande tickets met die status staan nu op Afgerond. Het lijstfilter heet "Afgerond".

## PDF-viewer (V68)
- De PDF-viewer in WorkPortal toont alle pagina's onder elkaar (met PDF.js, meegeleverd in static/pdfjs, Apache-2.0).
  Een iPhone/iPad liet in het oude venster alleen de eerste pagina zien, waardoor bijv. de handtekening op pagina 2
  van een werkbon ontbrak. Met + en − zoom je in en uit; downloaden en openen in een nieuw venster blijven bovenin.

## Kenniscentrum: Leidingen, pompen & CIP (V69)
Kenniscentrum › **Leidingen, pompen & CIP** (rekent direct in de browser, ook op de telefoon):
- **Leidinginhoud:** regels met buis (DIN 11850 reeks 2: 13 t/m 204, inch-OD of eigen binnendiameter) en lengte,
  plus overige inhoud (tank, wisselaar) → liters; met debiet ook de vul-/rondgangtijd.
- **Snelheid & debiet:** debiet → snelheid of gewenste snelheid → debiet, met Reynoldsgetal en een kleur per toepassing
  (CIP ≥ 1,5 m/s; water/sap/melk 1–2,5; concentraat 0,5–1,5; beslag/viskeus 0,2–1; zuigleiding ≤ 1). Tabel met de
  snelheid in alle DIN-maten bij hetzelfde debiet. Media met richtwaarden (water, CIP-loog/-zuur, sap, melk,
  concentraat, beslag, eigen) – dichtheid en viscositeit aan te passen.
- **Pomp & drukverlies:** Darcy-Weisbach (RVS 0,002 mm), bochten/T-stukken/kleppen, hoogteverschil en extra drukverlies
  (wisselaar, filter, sproeibol) → opvoerhoogte, hydraulisch en asvermogen en een motorvoorstel.
- **CIP / reinigen:** minimaal pompdebiet voor de gewenste snelheid in de grootste leiding, sproeibol (l/min per m
  tankomtrek), rondgangtijd, liters voorraadloog/-zuur voor de gewenste concentratie en spoelwater.
- **Verpompen:** tijd bij een debiet of benodigd debiet voor een tijd, met snelheidscontrole.
- **Opslaan & PDF:** geef een naam en koppel aan een project/order of ticket; daar staat de berekening onder
  "Berekeningen" (openen en verder rekenen, of PDF).

## Aantekeningen (productie-opmerkingen) bij projecten en orders (V70)

Vervangt het losse Word-bestand met screenshots. Op het project/de order staat de kaart **Aantekeningen**.

- **Afbeelding**: *Screenshot maken* (Chrome/Edge op de computer: kies het venster van bijvoorbeeld SolidWorks of een heel scherm, daarna sleep je een kader zoals bij het Knipprogramma), *Foto maken*, *Uit galerij / bestand*, plakken met Ctrl+V (bijv. na Windows+Shift+S), slepen, of *Leeg vel*. In de 3D-viewer van een project zit een knop **Aantekening** die het huidige beeld meeneemt.
  - Kies bij Screenshot liefst een **venster**; WorkPortal blijft dan in beeld en het venster wordt op de achtergrond vastgelegd (een geminimaliseerd venster kan niet). Bij een heel scherm krijg je 3 seconden om naar het programma te gaan.
  - Op telefoon/tablet bestaat schermopname in de browser niet; daar foto of galerij gebruiken.
- **Markeren**: pijl, tekst, kader, cirkel, lijn, pen en genummerde punten; zes kleuren, drie diktes, terug (Ctrl+Z), bijsnijden, wissen.
- **Tekst eronder**, **actie bij** (krijgt alleen een pushmelding, geen e-mail), **status** Nieuw / In behandeling / Afgerond. Afronden kan alleen met een stukje tekst "wat is er gedaan".
- Rechten: aanmaken/bewerken met bewerkrecht op Projecten/Orders. Degene bij wie de actie ligt (ook de werkplaats) mag afronden. Open acties staan op het dashboard onder *Aantekeningen met een actie voor mij*.
- **SharePoint**: bij elke wijziging wordt `Productie-opmerkingen <nummer>.pdf` bijgewerkt in de projectmap onder `3 …` / `2 …` (de eerste map die met 3 begint, daarin de eerste die met 2 begint; bestaan ze niet, dan worden `3 Documenten` / `2 Productie opmerkingen` aangemaakt).

## Foto's in mappen 4 t/m 8 (V70)

In submappen van de projectmap waarvan de naam met 4, 5, 6, 7 of 8 begint (en alles daaronder):

- foto's staan als raster met miniaturen (SharePoint maakt die zelf); tik voor groot beeld, vegen of pijltjes voor de volgende, *Origineel* en downloaden;
- knoppen **Foto maken** (camera, naam wordt `Foto <datum tijd>.jpg`) en **Uit galerij** (meerdere tegelijk) zetten de foto's direct in die map.

### V71
- **Foto's verwijderen** in de fotomappen (4 t/m 8): open de foto in groot beeld en tik op het prullenbakje. De foto gaat naar de prullenbak van SharePoint (daar 93 dagen terug te halen). Wie foto's mag toevoegen, mag ze ook weghalen; mappen kunnen hier niet verwijderd worden.
- **Groot beeld** ook voor de afbeelding van een aantekening; tik nog eens om in te zoomen (ook in de fotomappen).
- **Terugknop** op elk scherm (links boven, ook op de telefoon): terug naar het vorige scherm; kwam je van een formulier, dan één niveau omhoog.

## Excel bekijken (V74)
Excel-bestanden (`.xlsx`, `.xlsm`, `.xltx`, `.csv`) in de project-/ordermap openen in een eigen viewer binnen WorkPortal, met terugknop, tabbladen onderin, zoomknoppen en vaste kolom-/rijkoppen. Alleen lezen; de waarden zijn zoals Excel ze het laatst heeft berekend (formules worden niet opnieuw gerekend). Opmaak die meekomt: kolombreedtes, samengevoegde cellen, vet/cursief, tekst- en vulkleuren, getal-, procent- en euronotatie. Afbeeldingen (logo's, productfoto's) staan op hun plek; grote foto's worden verkleind, EMF/WMF-plaatjes en grafieken kan de browser niet tonen. Verborgen tabbladen, rijen en kolommen worden niet getoond. Grote tabbladen: eerste 2000 rijen × 80 kolommen. Het oude `.xls`-formaat kan niet in de viewer (downloadknop). De downloadknop opent op de telefoon het deelmenu (bijv. openen in de Excel-app), zodat je de app niet verlaat.

## Afdrukken uit de projectmap (V77)
Achter elke PDF (en foto: jpg/png, wordt automatisch een PDF van één pagina) in de project-/ordermap staat een printerknop, en in de PDF-viewer rechtsboven. Je kiest printer, aantal, pagina's (alle, de pagina die in de PDF-viewer in beeld is, of bijv. `1-3, 5` / `8-`), dubbelzijdig en kleur (standaard = Beheer > Printen). De laatst gekozen printer wordt per apparaat onthouden. In Beheer > Printen vink je aan uit welke printers gekozen kan worden (niets aangevinkt = alle); de standaardprinter blijft voor orderbonnen. Iedereen die de map mag zien, mag afdrukken.

**Printlogboek** (Beheer > Printen, onderaan): wie, wanneer en wat er is afgedrukt (map/bestand, aantal, dubbelzijdig/kleur, printer, Printix-job), ook orderbonnen (knop bij de order, Beheer > DC01 en automatisch bij binnenkomst) en mislukte pogingen. Alles staat ook in het algemene logboek.

## Wisselen van gebruiker op een gedeeld apparaat (V81)
Op een tablet of pc die door meerdere mensen wordt gebruikt: knop **Andere gebruiker** (op het pincodescherm, in de bovenbalk op de pc en onder *Meer* op de telefoon). Je kiest je naam en voert je eigen pincode in. Een nieuwe gebruiker voeg je toe met *Andere gebruiker toevoegen*: eenmalig een code per e-mail en een eigen pincode; daarna staat hij in de lijst. Iedere gebruiker heeft op het apparaat zijn eigen pincode en verificatie (elke 14 dagen opnieuw een e-mailcode). *Mij van dit apparaat halen* verwijdert alleen jezelf; de anderen blijven staan. Er worden maximaal 8 gebruikers per apparaat onthouden.

### Vergrendelen (V83)
- **Andere gebruiker / slotje**: vergrendelt altijd; terug kan alleen met je pincode.
- **App of venster sluiten** (kruisje rechtsboven, of de app wegvegen op de telefoon): bij het opnieuw openen is de pincode nodig. Let op: een nieuw tabblad of venster dat je zelf opent geldt ook als "opnieuw openen".
- **Geen activiteit**: na het ingestelde aantal minuten (Beheer > Instellingen, standaard 30, 0 = uit) wordt WorkPortal vergrendeld, ook als de app op de achtergrond stond. Daarnaast blijft "Altijd opnieuw pincode na (uren)" gelden.

## Gespreksnotities (V85)
- **Aanwezig van de klant** (vrije tekst, meerdere namen met komma's; suggesties uit de contactpersonen) en **Aanwezig van De Vreugd** (aanvinken; standaard staat de maker aangevinkt).
- **Reageren** onder elke notitie: iedereen die het project/de order mag zien (ook de werkplaats) kan reageren. De maker van de notitie krijgt een pushmelding (geen e-mail). Eigen reacties kun je verwijderen.
- Datumvelden staan op de iPhone nu even breed en links uitgelijnd zoals de andere velden.
- **Afvinkpunten** (V87): tik op *☐ Afvinkpunt*; er verschijnt direct een vinkvakje op de regel. Na Enter krijgt de volgende regel ook een vakje; Enter op een leeg vakje (of Backspace aan het begin) haalt het weg. In de notitie kan iedereen die het project mag zien de punten afvinken. *Annuleren* sluit het formulier zonder op te slaan.

## Servicebezoek: niets meer kwijt (V88)
- Tekst en foto's van een bezoek worden tijdens het invullen **op het apparaat bewaard**. Valt de app weg, wordt hij vergrendeld of herstart de telefoon, dan staat bij terugkomst alles er weer ("Concept teruggezet"). Het concept verdwijnt pas als het opslaan gelukt is (of met *Concept weggooien*).
- **Foto's tellen op**: meerdere keren *Foto maken* of *Uit galerij* voegt toe in plaats van te vervangen; met × haal je een foto weg vóór het opslaan.
- Zolang je bezig bent (typen, tikken) blijft de sessie open; na het maken van een foto wordt niet meer om de pincode gevraagd.
- Op de telefoon verdwijnt de menubalk onderin zolang het toetsenbord open is. Datum staat over de volle breedte; uren, reistijd en kilometers staan onderaan.
- (V89) Foto's worden op de telefoon verkleind (max. 2000 px, ±200 kB) vóór het versturen; opslaan toont een voortgangsbalk en de pagina blijft staan tot alles binnen is. Lukt het niet (geen bereik, vergrendeld), dan blijft alles bewaard. Lege of onleesbare foto's worden geweigerd met een melding. Tik op een foto (ook vóór het opslaan) voor groot beeld.
- (V91) **Foto's bij notities**: bij ticketnotities en gespreksnotities kun je foto's maken of uit de galerij kiezen (verkleind, met groot beeld). Ook voor notities wordt een concept bewaard. Het notitieformulier bij een ticket staat standaard dichtgeklapt.
- (V92) **Werkbon versturen** ook zonder handtekening: na *Afronden zonder handtekening* opent direct het verstuurscherm (aan/cc/kopie aan mij, onderwerp, tekst, SharePoint), en bij elk afgerond bezoek staat een knop *Versturen*. Laatst verstuurd naar/op staat bij het bezoek.
- (V93) **Nooit meer iets kwijt**: elk invulformulier met tekst (tickets, projecten, notities, druktesten, aantekeningen …, niet in Beheer) bewaart tijdens het typen een concept op het apparaat. Bij een aantekening wordt ook de afbeelding met pijlen en tekst bewaard. Bij terugkomst staat alles er weer; na een geslaagd opslaan verdwijnt het concept.

## V94 – Knoppen bij een servicebezoek
- Per bezoek: kop (datum, monteur, uren), daaronder de statuslabels (getekend / afgerond zonder handtekening, verstuurd, SharePoint) en een aparte knoppenbalk.
- Open bezoek: "Handtekening & afronden" als hoofdknop over de volle breedte (mobiel), daaronder Bewerken, Werkbon en verwijderen; "Afronden zonder handtekening" eronder.
- Afgerond bezoek: Werkbon en Versturen naast elkaar, prullenbak rechts.

## V95 – Kop van Aantekeningen in een project
- Titel "Aantekeningen" met daaronder de teller (x open · y totaal).
- Knoppenbalk: "+ Aantekening" (hoofdknop), PDF en "Alles (n)"; op de telefoon even breed naast elkaar onder de titel, op de computer rechts.

## V96 – Dashboard, Word-viewer en bijlagen bij gespreksnotities
- Dashboard: "Aantekeningen met een actie voor mij" toont de eerste 3; knop "Toon alle n" klapt de rest uit.
- Word-viewer (`/word?src=…`, `docview.py`): .docx-bestanden lezen binnen de app (projectmappen in SharePoint én bijlagen overal), met terug-knop en "Opslaan/delen".
  Eigen omzetter zonder extra pakketten: koppen, opmaak, lijsten/nummering, tabellen (samengevoegde cellen, arcering), afbeeldingen, kop-/voettekst, voetnoten, links (alleen http/mailto/tel).
  Oude .doc-bestanden: melding + downloadknop. Gedeelde bronlader `_viewer_src()` voor Excel- en Word-viewer.
- Bijlagen tonen overal als nette regels met type-icoon; PDF, Word en Excel openen in de viewer, andere typen downloaden.
- Gespreksnotities: "Bestand bijvoegen" (Word, Excel, PDF, tekening, e-mail …). Bijlagen worden als concept op het apparaat bewaard tot opslaan gelukt is (IndexedDB, `KEY:att`, tot 40 MB).
  Bij bewerken: huidige foto's/bijlagen aanvinken om te verwijderen (`del_file`). Extra toegestane typen: .xlsm, .docm, .rtf, .odt, .ods, .pptx, .ppt, .msg, .eml, .igs, .iges.

## V97 – Genegeerde ordermappen
- Oorzaak: een ordermap die op "Negeren" is gezet, haalt nooit meer de orderbon op (ook niet als die er later in komt).
- Komt er na het negeren toch een orderbon in de map, dan wordt de map automatisch weer opgepakt (status "nieuw") en de orderbon opgehaald en verwerkt.
- Orders > Inbox > Laatst verwerkt: knop "Niet meer negeren" bij genegeerde mappen.
- Beheer > DC01: filter "Genegeerd"; "Orderbon opnieuw ophalen" zet geselecteerde genegeerde mappen ook terug.

## V98 – Genegeerde ordermap: orderbon toch ophalen
- V97 leunde op de orderbon-gegevens in de mappenlijst; een oudere versie van komdex-orders.ps1 op DC01 stuurt die niet mee, waardoor een genegeerde map nooit werd opgepakt.
- Nu vraagt WorkPortal het script ook om de orderbon van genegeerde mappen (laatste 60 dagen, nog geen orderbon). Staat er een orderbon in, dan stuurt het script die op; WorkPortal pakt de map dan weer op (status "nieuw"), verwerkt de bon en print hem (als automatisch printen aan staat).

## V99 – Orderbon-ontvangst robuuster en zichtbaar
- Een orderbon die bij het uitlezen of verwerken een fout geeft, laat het script niet meer vastlopen: de fout staat als melding bij de map in Beheer > DC01.
- Bij ontvangst wordt direct "Orderbon '…' ontvangen" vastgelegd; blijft die melding staan, dan liep het verwerken mis.
- Beheer > DC01 status: "Om orderbon gevraagd" (aantal mappen bij laatste run) en "Laatste orderbon binnen" (tijd + ordernummer).
- scripts/komdex-orders.ps1: elke orderbon apart afgevangen en fouten met map in het logboek (optioneel bijwerken op DC01; $Sleutel opnieuw invullen).

## V100 – Script komdex-orders.ps1: logboek
- Oorzaak dat er sinds 18-10 geen orderbonnen meer binnenkwamen: het logbestand werd groter dan 1 MB; het inkorten (Get-Content | Set-Content op hetzelfde bestand) gaf "bestand in gebruik", waardoor het script na de mappenlijst stopte en de orderbonnen nooit verstuurde.
- Log-functie: eerst inlezen, dan overschrijven, en fouten bij het loggen worden genegeerd. Moet op DC01 worden bijgewerkt.

## V101 – Meldingen over orders die nog goed gezet moeten worden
- Beheer > DC01 > "Meldingen": kies per persoon wie een melding krijgt als een nieuwe ordermap na 10 minuten nog in het actievenster staat, en of dat push + e-mail of alleen push is.
- Instellingen `komdex_notify_users` (JSON-lijst met gebruikers-id's) en `komdex_notify_email`; zonder instelling blijft de standaard (rol verkoop/directie).
- De melding noemt nu ook de reden (bijv. onbekend ordertype of klant niet herkend).

## V102 – Inkoopfacturen: mailbox geweigerd (403)
- Bij een geweigerde mailbox haalt WorkPortal eerst één keer een nieuw Graph-token (een net toegevoegd recht zit niet in een bewaard token, dat tot een uur geldig is).
- Blijft het 403, dan zegt de melding welke rechten de app-registratie nu heeft en of Mail.Read ontbreekt of Exchange (ApplicationAccessPolicy) de mailbox tegenhoudt.

## V103 – Inkoopfacturen: beter uitlezen, leverancier uit de relaties, statussen
- **Bedragen:** ook totalen die als tabel onder een kopje staan ("Totaal € excl. btw" met het bedrag eronder) worden gelezen (op positie in de PDF). Incl./excl. worden niet meer verwisseld.
- **Inkoopnummer** (veld "Inkoopnummer", was "Inkooporder ERP"): begint altijd met 4 cijfers (4267, 4267-0, 4268-20260101), gezocht achter o.a. "Uw referentie", "Uw ordernr.", "PO number", "Your reference", "Bestellnummer"; anders elk "4xxx-2026xxxx".
- **Factuurdatum** ook bij "Documentdatum". Leveranciersnaam uit de kop (zonder KvK/BTW-tekst).
- **Leverancier uit de relaties** (`match_supplier`): controleert naam op de factuur, e-maildomein afzender, website/e-mail op de factuur, postcode, telefoonnummer en eerdere facturen van hetzelfde adres. Alleen bij een overtuigende match (≥2 kenmerken, het e-maildomein, of een lange unieke naam) wordt de relatie gekoppeld; bij twijfel tussen twee relaties niet. Eigen adres/De Vreugd wordt overgeslagen. Migratie 23: `invoices.supplier_id`, `invoices.supplier_match`.
- Detailscherm: leverancier kiezen uit een lijst met leveranciers; groen "Uit de relaties · herkend op …" of oranje "Niet gevonden in de relaties". Knop **Opnieuw uitlezen** (per factuur, overschrijft) en in het overzicht (Beheer-recht: alleen lege velden en leverancier aanvullen bij openstaande facturen).
- **Statussen:** Nieuw, Verwerkt in Komdex, Verwerkt buiten Komdex om (nieuw, status `buiten`), Verwerkt in SnelStart; direct te kiezen (ook terug). De knoppen "Verwerkt in ERP" + "en volgende →" zijn vervangen door een statuskeuze.

## V104 – Inkoopfacturen: leverancier beter herkennen
- Oorzaak (Fabory): de naamvergelijking haalde "Nederland" en "BV" weg, waardoor alleen "fabory" overbleef; een korte naam alleen telde niet als zeker, en de relatie heeft geen e-mail/website/postcode.
- Nieuw: de **volledige naam** letterlijk op de factuur (bijv. "Fabory Nederland BV") telt als zeker; en een **e-maildomein dat bij de naam past** (verkoopnl@fabory.com ↔ "Fabory …") ook.
- Een leveranciersnaam als "Invoice ." of "Factuur" wordt vervangen door de naam uit de kop van de factuur.
- Bedrag excl.: "BTW basis" / "grondslag" gaat vóór "Subtotaal" (vracht/toeslagen staan vaak onder het subtotaal).

## V105 – Inkoopfacturen: zoekveld voor de leverancier
- Het veld Leverancier is een zoekveld in de relaties (zoekt in naam, plaats en e-mail; leveranciers bovenaan). Gekozen relatie = gekoppeld (`supplier_id`, "handmatig gekozen").
- Staat de leverancier er niet in: "+ … toevoegen als nieuwe leverancier" maakt direct een relatie aan (type leverancier, met e-mail en website voor het herkennen van volgende facturen). Mag ook met alleen het recht Inkoopfacturen bewerken.
- De oude keuzelijst van de browser (die ook namen toonde die niet begonnen met wat je typte) is weg.

## V106 – Inkoopfacturen: één kaart per mail en beter uitlezen
- **Eén kaart per mail.** De PDF die het meest op een factuur lijkt (factuurnummer, bedrag, bestandsnaam) wordt de factuur; algemene voorwaarden en andere bestanden komen als "Andere bijlagen uit de mail" op dezelfde kaart (openen in de viewer).
  Bestaande losse kaarten uit dezelfde mail worden automatisch samengevoegd (bij het ophalen en bij "Opnieuw uitlezen"), zolang ze nog Nieuw zijn en er niets bij is gezet.
- **Uitlezen:** "TOTAAL IN EUR", "excl. BTW"/"incl. BTW" als kolomkop, "BTW basis", factuurnummer/-datum in een tabel onderaan ("Fact.nr.", "Fact. datum"), btw-bedrag (ook als kolom) om een ontbrekend of verkeerd incl.-bedrag te berekenen, vervaldatum uit de betalingstermijn ("binnen 14 dagen", "30 dagen netto").
- **Leverancier:** e-maildomein of website die bij de naam past (bmn-bv.nl ↔ "BMN …", ook als afkorting), en bij dubbele relaties voorrang voor de relatie waarvan de naam bij het domein past of die al eerder facturen had.
- **Opnieuw uitlezen (overzicht):** werkt nu ook verkeerd uitgelezen waarden bij; wat iemand zelf heeft aangepast blijft staan.
- Veld "Omschrijving" weg.

## V107 – Inkoopfacturen: lange facturen en meer opmaak
- Facturen met meerdere pagina's: de eerste twee én de laatste twee pagina's worden gelezen (de totalen staan op de laatste); eerder alleen pagina 1–3.
- Factuurnummer moet een cijfer bevatten (niet meer "Klantnummer" uit de kolom ernaast); staat het nummer onder het kopje ("Refnr. / Invoice number"), dan wordt het daar gevonden.
- "EINDBEDRAG", "Netto EUR …" en "B.T.W. 21,00 % …" worden herkend; ontbreekt incl., dan excl. + btw.

## V108 – Inkoopfacturen: euroteken en meer opmaak
- Euroteken in de velden Bedrag incl./excl. btw.
- "FACTUUR N° FLL A6665966" (ook met spatie en letters), datum na de plaatsnaam ("'s-Hertogenbosch, 02 oktober 2026"), "Vervaldag".
- "Totaal BTW 21,00 %" wordt niet meer als totaalbedrag gezien; kolom "Totaal" naast "Excl. BTW"/"Bedrag BTW" wel (gecontroleerd met de btw).
- Een kopje over twee kolommen ("BTW | Bedrag") telt niet meer als één kopje; waarden moeten echt onder het kopje staan.

## V109 – Inkoopfacturen: IMS-opmaak
- Bedrag op een totaalregel = het laatste bedrag op die regel; gewichten (KG), percentages en aantallen tellen niet mee ("gewicht/factuurbedrag 341,308 KG 1.719,54" → 1.719,54).
- "btw 21,00 % over 1.421,11  298,43": grondslag (excl.) = bedrag na "over", btw = laatste bedrag.
- "onze factuur / datum  1260695561 / 08.10.2026": factuurnummer én factuurdatum samen (gaat vóór een orderdatum elders op de factuur).

## V110 – Inkoopfacturen: toeslagen in het excl.-bedrag
- Staan er na het nettobedrag nog toeslagen (milieu, tol, vracht), dan klopt "Nettobedrag" niet als excl. WorkPortal controleert nu: totaal − btw past bij het btw-tarief → dat is het excl.-bedrag (Westfalen: 224,58 + 15,00 + 5,00 = 244,58).
- Grondslag na "van" op de btw-regel ("BTW 21 % van 244,58 51,36").
- Vervaldatum uit "Betalingstermijn: tot 06.11.2026".

## V111 – Vervaldatum standaard 30 dagen
- Staat er geen vervaldatum of betalingstermijn op de factuur, dan wordt de vervaldatum factuurdatum + 30 dagen (in het logboek: "niet op de factuur: factuurdatum + 30 dagen"). Een gevonden vervaldatum of termijn gaat altijd voor.

## V112 – Inkoopfacturen: datums zonder kopje ervoor
- "Vervaldatum" als kopje met de datum eronder wordt gelezen (Gaasbeek: 13-11-2026).
- Factuurdatum zonder kopje (losse datum in de kop, bijv. rechts naast het debiteurnummer): de eerste datum die geen verval-, lever-, order- of periodedatum is.

## V113 – Inkoopfacturen: streepjes, ij/y en naam van de afzender
- Sommige PDF's gebruiken een ander streepje (‑ – —) of een harde spatie: "15‑10‑2026" en "4288‑20260000" werden daardoor niet herkend (Heleon: datum leeg, inkoopnummer alleen "4288"). Alle streepjes en spaties worden nu eerst gelijkgemaakt.
- Leverancier: website op de factuur die precies de naam is (waayenbergcoatings.nl ↔ "Waayenberg Coatings") telt als zeker; ook de naam van de afzender van de mail ("Digitale factuur van Waaijenberg Coatings BV") wordt gebruikt; ij en y gelden als gelijk.

## V114 – Inkoopfacturen: statusbalk en statuskeuze
- Voortgangsbalk met splitsing: Binnengekomen → óf Verwerkt in Komdex (boven) óf Verwerkt buiten Komdex om (onder) → Verwerkt in SnelStart. De gekozen weg kleurt groen, de andere wordt grijs.
- Statuskaart: Nieuw, dan "stap 2 – kies één van de twee" (Komdex of buiten Komdex; nooit allebei), dan Verwerkt in SnelStart. Na SnelStart blijft zichtbaar welke weg genomen is.
- Afgewezen: status "Afgewezen door <naam>" (met datum en reden), ook in het overzicht; "Afwijzing intrekken" zet hem terug naar Nieuw.
- Migratie 24: `invoices.route` ('komdex' of 'buiten').

## V115 – Inkoopfacturen: bredere pagina, vrije volgorde, Engelse totalen
- Factuurpagina over de volle breedte; PDF-kolom breder en hoger. Voortgangsbalk lager en over de hele breedte.
- Komdex en SnelStart zijn twee losse stappen in willekeurige volgorde: Komdex óf buiten Komdex om (nog eens tikken = uit) en SnelStart aan/uit. Allebei gedaan = Afgerond. Eerst SnelStart → status "In SnelStart, nog Komdex" (filter "Nog in Komdex"). "Nieuw" zet alles terug.
- Engelse facturen: "To pay in EUR", "Total due", "Grand total"; 0,00 telt niet ("To pay: 0,00" na vooruitbetaling); grondslag na "VAT on …" (Oceanz: excl. 168,80, incl. 204,25).

## V116 – Inkoopfactuur iets smaller
- Factuurpagina maximaal 1640 px breed (was de volle breedte); PDF : gegevens = 1,3 : 1.

## V117 – Voortgangsbalk in volgorde van afhandelen
- Drie stappen op één lijn: 1 Binnengekomen, 2 en 3 eerst leeg. De eerste status die gegeven wordt (Komdex, buiten Komdex om of SnelStart) komt bij 2; bij 3 staat wat nog moet ("Nog: verwerken in SnelStart" / "Nog: Komdex (of buiten Komdex om)"), en na afhandelen wat als tweede gedaan is. Volgorde op tijdstip (erp_at / snelstart_at).

## V118 – Inkoopfacturen: kopjes en waarden in een andere letter
- Bij facturen waar de kopjes kleiner zijn dan de waarden en dicht op elkaar staan (Heleon) haalde de standaard tekst-indeling de regels door elkaar ("F D O P a a e rd …"). Nu wordt ook een strakkere regel-indeling gelezen (reserve), en zoekt WorkPortal de waarde rechts van het kopje op ongeveer dezelfde hoogte.
- Tekens die de PDF niet kan vertalen ("(cid:16)") tussen cijfers worden als streepje gelezen.
- Inkoopnummer ook op positie (rechts van "Uw referentie"), zodat het volledige nummer (4288-20260000) wordt genomen.

## V119 – Heleon: zacht afbreekstreepje
- Getest op de echte PDF: Heleon gebruikt in datums en nummers het "zachte afbreekstreepje" (U+00AD). V113 haalde dat teken weg, waardoor 15‑10‑2026 → 15102026 en 4288‑20260000 → 428820260000 werd. Het wordt nu als gewoon streepje gelezen: factuurdatum 15-10-2026, vervaldatum 14-11-2026, inkoopnummer 4288-20260000.
- Reserve-leveranciersnaam slaat regels als "Debiteurnr. : …", straat + huisnummer en "Afleveradres" over.

## V120 – Inkoopfacturen: leverancier mailen vanaf de kaart
- Knop **Mail leverancier** in het gesprek: Aan (voorgevuld met het e-mailadres van de relatie, de vorige ontvanger of de afzender van de factuur; geen no-reply), Cc, onderwerp, bericht, "Factuur-PDF meesturen", "Kopie naar mij".
- Verstuurd vanuit de factuurmailbox (Beheer › Instellingen, standaard factuur@devreugd-pt.nl) met de naam van de verzender + "Crediteurenadministratie" in de handtekening. Vereist het recht Mail.Send op die mailbox (zelfde app-registratie; staat er een ApplicationAccessPolicy, dan moet factuur@ in de groep).
- In het onderwerp staat `[WP-F<nummer>]`. Bij het ophalen van de mailbox worden mails met die code (dus elke "RE:", van wie dan ook) als **reactie leverancier** in het gesprek van die kaart gezet, alleen het nieuwe deel van de tekst; bijlagen komen bij "Andere bijlagen". Wie de mail verstuurde krijgt een pushmelding.
- Het ophalen leest nu alle nieuwe mails in de inbox (niet alleen met bijlage); mails zonder bijlage en zonder code worden overgeslagen.
- Leverancier herkennen: alleen "De Vreugd Productietechniek" (onszelf) wordt overgeslagen, andere "De Vreugd"-bedrijven (Transport) niet; ons eigen adresblok (t.a.v., Edisonring) telt niet mee; relaties van het type klant tellen minder zwaar.
- Migratie 25: `invoice_notes.mail_meta`.

## V121 – Mail aan leverancier in dezelfde mailwisseling
- Kwam de factuur per mail binnen, dan is de mail vanaf de kaart een **antwoord op die mail** (Graph `reply`, recht Mail.Send): onderwerp exact "RE: <onderwerp van de factuurmail>", en in Outlook hangt hij onder de binnengekomen mail. Geen code meer in het onderwerp.
- Reacties van de leverancier in diezelfde mailwisseling (conversationId) komen in het gesprek van de kaart, ook zonder code. Bij geüploade facturen (geen mail) blijft de code `[WP-F…]` in het onderwerp nodig.
- Migratie 26: `invoices.mail_conv` (wordt voor nieuwe mails bij binnenkomst opgeslagen, voor oudere bij het eerste antwoord opgehaald).

## V122 – Mails uit het gesprek verwijderen
- Prullenbak bij elke mail (verstuurd of reactie) in het gesprek, en bij je eigen opmerkingen (Beheer: alle opmerkingen; een afwijzing niet). Na bevestigen weg uit het gesprek, met een regel in het logboek.
- Alleen in WorkPortal: de mail blijft in Outlook (factuur@) staan; bijlagen van een reactie blijven bij de kaart.

## V123 – Reactie van de leverancier beantwoorden
- Onder elke reactie van een leverancier in het gesprek: **Beantwoorden**. Het mailformulier opent met "Antwoord aan <naam>", Aan = de afzender van die reactie, onderwerp "RE: …" van die mail (vast), PDF standaard niet mee.
- Verstuurd als antwoord op dát bericht (Graph `reply`), dus in dezelfde mailwisseling in Outlook; ook de volgende reactie komt weer op de kaart. In het gesprek staat bij de verstuurde mail waarop hij een antwoord was.
- Reacties bewaren nu het bericht-id (`msg_id` in `mail_meta`); bij oudere reacties zonder id wordt geantwoord op de oorspronkelijke factuurmail.

## V124 – Inkoopfacturen: vraag intrekken, slepen, intern/extern
- Verwijder je een vraag aan een collega uit het gesprek, dan verdwijnt ook "Vraag open bij …" (en de melding bij die collega). Nieuwe knop **Vraag intrekken** in de gele balk.
- **Slepen naar Komdex:** boven de PDF een blok met de bestandsnaam ("Leverancier Factuurnummer.pdf"); sleep dat naar Komdex, de Verkenner, het bureaublad of een mail. Werkt in Chrome en Edge (DownloadURL). Ook "Andere bijlagen" zijn te slepen. Pakt Komdex het niet direct, sleep dan eerst naar het bureaublad.
- Mail aan leverancier: Aan is standaard het adres waar de factuurmail vandaan kwam; knopjes om te wisselen naar het adres van de relatie of de vorige ontvanger.
- Gesprek gesplitst in **Intern – vragen en opmerkingen** (collega's, vraag stellen, voor de boekhouding) en **Extern – leverancier** (mails, beantwoorden, mail leverancier).

## V125 – Venster na statuswijziging
- Na het wijzigen van de status verschijnt een venster: wat er gewijzigd is, de status van de factuur, en de knoppen **Door naar volgende factuur →** (met leverancier, nummer en status van die factuur), **Op deze kaart blijven** en **Naar het overzicht**.
- "Volgende" = de oudste openstaande factuur zonder open vraag die nog dezelfde stap nodig heeft (na Komdex: nog geen Komdex; na SnelStart: nog niet in SnelStart); anders de oudste openstaande. Zijn er geen meer, dan staat dat erin.
- Bij vernieuwen van de pagina komt het venster niet opnieuw.

## V126 – Annuleren
- Knop **Annuleren** bij een interne opmerking, een mail aan de leverancier (ook bij beantwoorden), een vraag aan een collega en bij afwijzen. Annuleren leegt het formulier (ook het bewaarde concept) en klapt het blok dicht; bij beantwoorden ga je terug naar de kaart.
- Algemeen bruikbaar: `<button type="button" data-cancel [data-cancel-href]>`.

## V127 (1.74.0)
- Inkoopfacturen: interne opmerking zit nu achter de knop "Opmerking plaatsen…" (naast "Vraag stellen aan collega…"); bij een openstaande vraag aan jou staat "Antwoord geven…" direct open. Annuleren klapt hem weer dicht.
- Terug-link (kruimelpad) boven aan de pagina's: pijltje netjes in lijn met de tekst.

## V128 (1.75.0)
- Inkoopfacturen: is er al mail met de leverancier, dan wordt "Mail leverancier" de knop "Antwoord allen": antwoord op de allerlaatste mail, in dezelfde mailwisseling, aan de afzender met iedereen die erop stond in Cc (ons eigen factuuradres eruit). "Nieuwe mail in plaats daarvan" blijft mogelijk.
- Reacties van leveranciers bewaren nu ook de Aan/Cc-lijst (voor Antwoord allen).
- Gegevens-formulier van een factuur maakt geen (onterecht) concept meer; de concept-melding zegt nu waarvan het concept is.

## V129 (1.76.0)
- Inkoopfacturen: komt een factuur (uit de mailbox) op Afgerond (Komdex + SnelStart), dan vraagt WorkPortal de boekhouding of de mail – met alle mails uit dezelfde mailwisseling, ook reacties en onze verzonden mails – naar de map "01-Verwerkt" mag.
  - Wie afrondt en bij de boekhouding hoort, krijgt de vraag direct in het venster "Door naar volgende factuur / Op deze kaart blijven" (vinkje staat standaard aan).
  - De andere boekhouders krijgen een pushmelding; op de factuurkaart staat een gele balk "Ja, verplaatsen / Nee, laten staan". Bij "Nee" kan later nog "Alsnog naar 01-Verwerkt".
  - Beheer > Instellingen: mapnaam en wie de vraag krijgt (standaard: rol administratie). Bestaat de map niet, dan komt hij onder Postvak IN.
  - Nodig in Entra (app-registratie): toepassingsrecht Mail.ReadWrite met beheerderstoestemming. Zonder dat recht: melding in het logboek en de balk "opnieuw proberen".
  - Mails in Verwijderde items en Concepten blijven staan. Na verplaatsen worden de bewaarde bericht-id's bijgewerkt, zodat Antwoord allen blijft werken.
- Database-migratie 27: invoices.mail_archive.

## V130 (1.77.0)
- Inkoopfacturen: de vraag "mail naar 01-Verwerkt?" gaat naar één verantwoordelijke boekhouder, te kiezen in Beheer > Instellingen (losse keuze, los van de rollen; de rollen blijven ongewijzigd). Alleen die persoon krijgt de melding, de vraag in het venster en de gele balk, en mag hem beantwoorden.
- Niemand gekozen: iedereen met beheerrechten op inkoopfacturen kan het doen, zonder melding.

## V131 (1.78.0)
- Inkoopfacturen: de Outlook-categorieën van de factuurmail worden uitgelezen en getoond (met de kleur uit Outlook) in het overzicht onder de status en op de factuurkaart bovenaan ("Outlook: …").
  - Bijgewerkt bij elk ophalen van de mailbox (elke 5 minuten / knop Postvak ophalen) voor openstaande facturen en facturen van de laatste 30 dagen. Alleen lezen: er wordt in Outlook niets veranderd.
  - Categorieën: recht Mail.Read (is er al). Kleuren: recht MailboxSettings.Read (toepassingsrecht, beheerderstoestemming); zonder dat recht worden de categorieën grijs getoond en staat de reden bij Beheer > Instellingen.
- Database-migratie 28: invoices.mail_categories.

## V132 (1.79.0)
- Inkoopfacturen: facturen die via een facturatieplatform binnenkomen (bijv. mail@klantmail.snelstart.nl, Exact, Moneybird, e-Boekhouden, Visma, AFAS …) werden gekoppeld aan de leverancier van een eerdere factuur van hetzelfde adres. Zo'n gedeeld adres telt niet meer mee; de leverancier komt dan uit de factuur zelf (naam, website, postcode, telefoon) en de naam van de afzender in de mail.
- "Opnieuw uitlezen" maakt een koppeling die alleen op dat gedeelde adres berustte los.

## V133 (1.80.0)
- Inkoopfacturen: de knop "Door naar volgende factuur" in het venster na een statuswijziging gaf "Niet gevonden" (404) door een fout in de link (sinds V129). Opgelost.

## V134 (1.81.0)
- Inkoopfacturen: de tellers bovenaan tellen nu per boeking: "Nog in Komdex" (nieuw + al in SnelStart) en "Nog in SnelStart" (nieuw + al in/buiten Komdex), met eronder hoeveel daarvan de andere stap al hebben. Eerder telde "Nieuw" een factuur niet meer mee zodra hij alleen in SnelStart was geboekt.
- Filters "Nog in Komdex" en "Nog in SnelStart" tonen dezelfde facturen als de tellers.

## V135 (1.82.0)
- Microsoft-koppeling: wordt een recht geweigerd (403), dan haalt WorkPortal één keer een vers token op – een net toegekend recht (Mail.ReadWrite, MailboxSettings.Read) werkt dan meteen, zonder herstart.
- Categoriekleuren: na een mislukte poging wordt het bij elke ophaalronde opnieuw geprobeerd (niet pas na een uur).
- Beheer > Instellingen: overzicht welke rechten de koppeling heeft (Mail.Read, Mail.Send, Mail.ReadWrite, MailboxSettings.Read) met "opnieuw controleren".

## V136 (1.83.0)
- Inkoopfacturen: krijgt een factuur in WorkPortal een status, dan krijgt de factuurmail in Outlook de bijbehorende categorie (Verwerkt in Komdex, Verwerkt buiten Komdex om, Verwerkt in SnelStart, Afgewezen). Terugzetten haalt de categorie weer weg. Categorieën die iemand zelf in Outlook heeft gegeven (en die niet in het lijstje staan) blijven staan.
- Beheer > Instellingen: per status de Outlook-categorie kiezen uit de categorielijst van de mailbox (met kleur). Niet ingesteld: WorkPortal kiest op naam (bevat "Komdex", "buiten", "SnelStart", "afgewezen").
- Gebeurt op de achtergrond, vóór het verplaatsen naar 01-Verwerkt. Recht: Mail.ReadWrite. Lukt het niet, dan staat de reden in het logboek van de factuur.

## V137 (1.84.0)
- Inkoopfacturen: de tellers "Nog in Komdex" en "Nog in SnelStart" hebben de kleur van de Outlook-categorie die bij die stap hoort (Beheer > Instellingen); zonder categorie Komdex groen en SnelStart oranje.

## V138 (1.85.0)
- Inkoopfacturen: extra Outlook-categorie "Vraag uitstaand": de factuurmail krijgt hem zolang er een vraag bij een collega openstaat (intern) of onze laatste mail aan de leverancier nog niet beantwoord is (extern). Bij het antwoord, intrekken of afwijzen gaat hij eraf.
- De categorie wordt nu na élke wijziging op een factuurkaart bijgewerkt (status, vraag, antwoord, mail, afwijzen) en als er een reactie van de leverancier binnenkomt.
- Raden op naam herkent nu ook "vraag" (bijv. "VRAAG in Workportal").

## V139 (1.86.0)
- Inkoopfacturen: knop "Categorieën ophalen" (naast "Mailbox nu ophalen", voor beheer) haalt de categorielijst met kleuren en de categorieën van de factuurmails direct opnieuw uit Outlook.
- Beheer > Instellingen, bij Outlook-categorie per status: "Categorieën opnieuw ophalen" en "Categorieën toepassen op openstaande facturen" (zet bij alle openstaande en recente factuurmails de categorie volgens de huidige status, op de achtergrond).

## V140 (1.87.0)
- Inkoopfacturen: WorkPortal en de factuurmail lopen gelijk. Zet iemand in Outlook een statuscategorie (bijv. "Geboekt Snelstart" of "Verwerkt in KOMDEX"), dan neemt WorkPortal die status bij de volgende ophaalronde over (logboek: "Status overgenomen uit Outlook"). Afgewezen via Outlook kan ook (reden: "Afgewezen via de Outlook-categorie"). Wordt het zo afgerond, dan komt de vraag "mail naar 01-Verwerkt?" zoals gewoonlijk.
- Een categorie weghalen in Outlook zet de status niet terug; terugzetten gaat in WorkPortal (dan gaat de categorie in Outlook ook weg).
- De statuscategorieën worden niet meer naast de status getoond (dat waren dubbele statussen); alleen overige categorieën uit Outlook blijven zichtbaar.

## V141 (1.88.0)
- Inkoopfacturen: teller "Vraag open" in geel.

## V142 (1.89.0)
- Inkoopfacturen: teller "Vervalt binnen 7 dagen" in (helder) rood.

## V143 (1.90.0)
- Inkoopfacturen: tussenstap **Af te ronden door boekhouder**. Zijn Komdex (of buiten Komdex om) en SnelStart klaar – in WorkPortal of via de Outlook-categorie – dan staat de factuur op "Af te ronden door boekhouder". Pas als de boekhouder afrondt, wordt het "Afgerond".
  - Boekhouder: gele balk op de kaart ("Afronden, mail laten staan" / "Afronden en mail verplaatsen"; bij een geüploade factuur alleen "Afronden"), of in het venster na de statuswijziging (vinkjes "Factuur afronden" en "Mail naar 01-Verwerkt").
  - Anderen zien "Af te ronden door <naam boekhouder>". De boekhouder krijgt een pushmelding.
  - Voortgangsbalk met een 4e stap; filter "Af te ronden"; "Alles open" telt ze mee, "Afgerond" niet.
  - Wat al afgerond was blijft afgerond (migratie 29).
- Status in het overzicht en op de kaart als losse stappen onder elkaar, elk in de kleur van zijn Outlook-categorie (Verwerkt in Komdex, Verwerkt in SnelStart, Vraag open, Vraag bij leverancier, Af te ronden …).
- Categoriekleuren zoals de nieuwe Outlook ze toont: Perzik (oranje), Pruimenpaars (buiten Komdex om), Lichtgroen, Hemelsblauw (vraag), Zilver.

## V144 (1.91.0)
- Inkoopfacturen: een mail aan de leverancier zet "Vraag bij leverancier" open. Die blijft open (ook als er een reactie komt) tot iemand op **Vraag bij leverancier afronden** klikt (balk bovenaan, in de statuskaart en bij Extern). Dat komt als regel in het gesprek met de leverancier.
- Zolang die vraag openstaat: geen status geven, niet afwijzen en niet afronden (knoppen grijs; ook op de server tegengehouden). Statussen uit Outlook worden dan niet overgenomen.
- Met een open interne vraag kan de boekhouder niet afronden.
- Teller en filter "Vraag open" tellen ook vragen bij de leverancier mee. De Outlook-categorie "VRAAG in Workportal" volgt dit.
- Database-migratie 30: invoices.ext_question (bestaande facturen waarvan onze laatste mail nog onbeantwoord is, staan op open).

## V145 (1.92.0)
- Inkoopfacturen: staat er een vraag bij de leverancier open, dan kan de boekhouder (Beheer > Instellingen) wel "Verwerkt in SnelStart" zetten; de andere statussen en afronden blijven geblokkeerd tot de vraag is afgerond.

## V146 (1.93.0)
- Inkoopfacturen: de Outlook-categorie komt nu op álle mails die bij de factuur horen: de factuurmail, de reacties van de leverancier en onze verzonden mails (de hele mailwisseling, ook Verzonden items; niet in Verwijderde items/Concepten).
- "Verwerkt in Komdex" en "Verwerkt buiten Komdex om" (zetten, uitzetten of terug naar Nieuw) mag alleen de boekhouder (Beheer > Instellingen) of wie beheerrechten op inkoopfacturen heeft (Beheer > Rechten). Voor anderen zijn die knoppen grijs; ook op de server tegengehouden.

## V147 (1.94.0)
- Correctie op V146: niet Komdex maar **Verwerkt in SnelStart** (zetten, uitzetten, of terug naar Nieuw als SnelStart al gezet was) mag alleen de boekhouder of wie beheerrechten op inkoopfacturen heeft. Komdex / buiten Komdex om kan weer iedereen met bewerkrechten.

## V148 (1.95.0)
- Menu: het getal bij Inkoopfacturen is nu wat de ingelogde gebruiker zelf nog moet doen (met uitleg bij aanwijzen):
  - iedereen: vragen die aan jou gesteld zijn;
  - de boekhouder (Beheer > Instellingen): + nog boeken in SnelStart + af te ronden;
  - anderen met bewerkrecht: + nog verwerken in Komdex (zonder open vraag).
