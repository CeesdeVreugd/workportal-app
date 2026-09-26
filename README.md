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
   `$OrderMap` (de Komdex-ordermap), `$Sleutel` (= `KOMDEX_KEY`) en eventueel `$Diepte`
   (1 als de ordermappen per klant in een submap staan).
3. Test: `powershell -ExecutionPolicy Bypass -File C:\Scripts\komdex-orders.ps1`. In
   `komdex-orders.log` staat `OK: … ordermappen`. De eerste keer worden alle bestaande mappen alleen
   onthouden; pas mappen die daarna verschijnen komen in het actievenster.
4. Taakplanner: elke 5 minuten, "uitvoeren ongeacht of gebruiker is aangemeld", account met leesrecht.
5. De server moet `https://workportal.devreugd-pt.nl` kunnen bereiken. Staat er in Nginx Proxy Manager een
   IP-toegangslijst op de host, voeg dan het IP van deze server toe.

Nieuwe mappen verschijnen onder **Orders → Nieuwe orders** (en op het dashboard). Gebruikers met de rol
Verkoop-Inkoop-WVB of Directie krijgen een push-/mailmelding. Daar kies je **order** of **project**, de klant
en de omschrijving; WorkPortal maakt dan de map in de klantmap in SharePoint:

- Project: `<ordernummer>-<omschrijving>` → Power Automate zet het sjabloon erin en plaatst de Teams-post.
- Order: `<ordernummer> <omschrijving>` (spatie, geen streepje) → Power Automate doet niets.

Controleer eenmalig dat de Power Automate-flow een map als `20260138 Omschrijving` níet oppakt.

**Orderbon: automatisch verwerken.** Staat er in de ordermap een PDF met "orderbon" in de naam (de
orderbon uit Komdex), dan stuurt het script die mee. WorkPortal leest ordernummer, ordertype, omschrijving,
klant, uitvoerder, order- en leverdatum en de werkomschrijving uit. Is het ordertype bekend als project of
order (in te stellen onderaan **Orders → Nieuwe orders**, of met het vinkje "voortaan automatisch" bij het
goedzetten) en is de klant bekend met een klantmap, dan maakt WorkPortal het project of de order zelf aan,
met map. Anders komt hij in het actievenster, met de gegevens van de bon al ingevuld. Een melding gaat pas
de deur uit als er na 10 minuten nog steeds actie nodig is. De werkomschrijving en de orderbon zijn te zien
bij het project/de order en bij gekoppelde servicetickets.

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
