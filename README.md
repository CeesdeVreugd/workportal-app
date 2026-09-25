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
Aan te passen in Beheer → Rechten per functierol. Per gebruiker kunnen extra rechten
worden gegeven. Een gebruiker met "Beheerder" aangevinkt heeft overal alle rechten.

## Inloggen

E-mailadres → code van 6 cijfers per mail (10 minuten geldig) → pincode per apparaat.
Elke 14 dagen opnieuw een e-mailcode (instelbaar). Na 5 foute pincodes is weer een
e-mailcode nodig. Na 12 uur vergrendelt de app automatisch (instelbaar).
