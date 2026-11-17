<#
  WorkPortal - DVP: nieuwe ordermappen uit Komdex doorgeven aan WorkPortal.

  Draai dit script als geplande taak (elke 5 minuten) op een server die bij de Komdex-ordermap kan.
  Het stuurt de lijst met ordermappen via HTTPS naar WorkPortal, de orderbon (PDF met "orderbon" in
  de naam) uit nieuwe ordermappen, en de nieuwste Excel die begint met "Nacalculatie" (wordt in
  WorkPortal de na-calculatie van die order; alleen als hij nieuw of gewijzigd is). Met het ordertype op de bon maakt WorkPortal het project of de order
  zelf aan; anders komt de map in het actievenster "Nieuwe orders". Er wordt niets gewijzigd in de
  Komdex-map (alleen lezen).
  Daarnaast: de verkoopfacturen uit het DigiDossier ($FactuurMap, jaarmappen > factuurmap > PDF) voor de module
  Verkoopfacturen. Ook alleen lezen; per keer hooguit 50 PDF's.

  Instellen:
    1. Vul hieronder $OrderMap en $Sleutel in ($Sleutel = KOMDEX_KEY uit Portainer).
    2. Test handmatig:  powershell -ExecutionPolicy Bypass -File "C:\Scripts\komdex-orders.ps1"
    3. Taakplanner: nieuwe taak, "Uitvoeren ongeacht of gebruiker is aangemeld",
       trigger dagelijks + herhalen elke 5 minuten, actie:
         Programma:  powershell.exe
         Argumenten: -NoProfile -ExecutionPolicy Bypass -File "C:\Scripts\komdex-orders.ps1"
       Gebruik een account met leesrecht op de Komdex-ordermap.
#>

# ---- instellingen -------------------------------------------------------
$OrderMap   = "D:\Shares\Komdex\Administraties\DeVreugd\DigiDossier\orders"   # Komdex-ordermap (met jaarmappen 2025, 2026, ...)
$WorkPortal = "https://workportal.devreugd-pt.nl"               # adres van WorkPortal
$Sleutel    = "VUL-HIER-KOMDEX_KEY-IN"                         # zelfde waarde als KOMDEX_KEY in Portainer
$Jaren      = 2                                                 # aantal recente jaarmappen dat wordt bekeken (2 = dit jaar en vorig jaar)
$FactuurMap = "D:\Shares\Komdex\Administraties\DeVreugd\DigiDossier\facturen"   # DigiDossier verkoopfacturen (jaarmappen > factuurmap > PDF); leeg = overslaan
$FactuurVanaf = 2020                                            # facturen vanaf dit jaar (jaarmappen 2020, 2021, ...)
$Logbestand = "$PSScriptRoot\komdex-orders.log"
# -------------------------------------------------------------------------

$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

function Log($tekst) {
    # Logboek bijhouden mag het script NOOIT laten stoppen (anders worden er geen orderbonnen meer opgehaald).
    try {
        $regel = "{0:yyyy-MM-dd HH:mm:ss}  {1}" -f (Get-Date), $tekst
        Add-Content -LiteralPath $Logbestand -Value $regel -Encoding UTF8
        # log klein houden: eerst de laatste regels helemaal inlezen, daarna pas het bestand overschrijven
        if ((Get-Item -LiteralPath $Logbestand).Length -gt 1MB) {
            $laatste = @(Get-Content -LiteralPath $Logbestand -Tail 2000)
            Set-Content -LiteralPath $Logbestand -Value $laatste -Encoding UTF8
        }
    }
    catch { }
}

# Nieuwste Excel "Nacalculatie..." in een ordermap (ook "Nacacalculatie"); tijdelijke ~$-bestanden van Excel overslaan
$NacalcPatroon = '^\s*na\s*-?\s*(ca)?calculatie.*\.xls[xm]?$'
function Map($dir) {
    $info = [ordered]@{ path = $dir.FullName; name = $dir.Name; parent = "" }
    $nc = Get-ChildItem -LiteralPath $dir.FullName -File -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match $NacalcPatroon } | Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($nc) { $info.nacalc = @{ name = $nc.Name; modified = $nc.LastWriteTimeUtc.ToString("o"); size = $nc.Length } }
    # orderbon: naam/datum/grootte, zodat WorkPortal een opnieuw opgeslagen orderbon (andere vinkjes) opnieuw ophaalt
    $ob = Get-ChildItem -LiteralPath $dir.FullName -File -Filter "*orderbon*.pdf" -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($ob) { $info.bon = @{ name = $ob.Name; modified = $ob.LastWriteTimeUtc.ToString("o"); size = $ob.Length } }
    return [pscustomobject]$info
}
# Herinneringen/aanmaningen in een factuurmap zijn niet de factuur zelf
$HerinneringPatroon = '(herinnering|aanmaning|sommatie|reminder|mahnung|zahlungserinnerung|rappel|relance)'
function FactuurPdf($bestanden) {
    # de factuur: geen herinnering; liefst met 'factuur'/'invoice' in de naam; daarvan de nieuwste
    $kand = @($bestanden | Where-Object { $_.Name -notmatch $HerinneringPatroon })
    $voorkeur = @($kand | Where-Object { $_.Name -match '(factuur|invoice|rechnung|facture|credit)' })
    if ($voorkeur.Count) { $kand = $voorkeur }
    return ($kand | Sort-Object LastWriteTime -Descending | Select-Object -First 1)
}
function LeesBestand($pad) {
    # ook lezen als het bestand in Excel openstaat (komma: als byte[] teruggeven, niet uitgerold)
    $fs = [IO.File]::Open($pad, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
    try { $ms = New-Object IO.MemoryStream; $fs.CopyTo($ms); return ,$ms.ToArray() } finally { $fs.Close() }
}

try {
    if (-not (Test-Path -LiteralPath $OrderMap)) { throw "Map niet gevonden: $OrderMap" }
    $root = (Get-Item -LiteralPath $OrderMap).FullName.TrimEnd('\')
    $vanafJaar = (Get-Date).Year - $Jaren + 1
    $mappen = New-Object System.Collections.Generic.List[object]
    foreach ($d in Get-ChildItem -LiteralPath $root -Directory -ErrorAction SilentlyContinue) {
        if ($d.Name -match '^\d{8}') {
            # ordermap direct in de hoofdmap
            $mappen.Add((Map $d))
        }
        elseif ($d.Name -match '^\d{4}$' -and [int]$d.Name -ge $vanafJaar) {
            # jaarmap: de ordermappen staan daarin
            foreach ($o in Get-ChildItem -LiteralPath $d.FullName -Directory -ErrorAction SilentlyContinue) {
                if ($o.Name -match '^\d{8}') {
                    $mappen.Add((Map $o))
                }
            }
        }
    }
    $json  = @{ folders = @($mappen.ToArray()) } | ConvertTo-Json -Depth 4 -Compress
    $bytes = [Text.Encoding]::UTF8.GetBytes($json)
    $res = Invoke-RestMethod -Uri "$WorkPortal/api/komdex/orders" -Method Post -Body $bytes `
        -ContentType "application/json; charset=utf-8" -Headers @{ "X-WorkPortal-Key" = $Sleutel } -TimeoutSec 60
    Log ("OK: {0} ordermappen, {1} nieuw, {2} met na-calculatie{3}" -f $res.ordermappen, $res.nieuw, $res.nacalc_gezien, $(if ($res.eerste_keer) { " (eerste keer: bestaande mappen onthouden)" } else { "" }))

    # Orderbon (PDF) meesturen voor de mappen waar WorkPortal nog om vraagt
    foreach ($pad in @($res.want)) {
        if (-not $pad -or -not (Test-Path -LiteralPath $pad)) { continue }
        $bon = Get-ChildItem -LiteralPath $pad -File -Filter "*orderbon*.pdf" -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending | Select-Object -First 1
        if (-not $bon) { continue }
        try {   # één orderbon die mislukt mag de rest niet tegenhouden
            $body = @{ path = $pad; filename = $bon.Name; modified = $bon.LastWriteTimeUtc.ToString("o"); size = $bon.Length
                       data = [Convert]::ToBase64String((LeesBestand $bon.FullName)) } | ConvertTo-Json -Compress
            $r2 = Invoke-RestMethod -Uri "$WorkPortal/api/komdex/orderbon" -Method Post -Body ([Text.Encoding]::UTF8.GetBytes($body)) `
                -ContentType "application/json; charset=utf-8" -Headers @{ "X-WorkPortal-Key" = $Sleutel } -TimeoutSec 120
            Log ("Orderbon {0}: {1} {2}" -f $bon.Name, $r2.ordertype, $r2.resultaat)
        }
        catch { Log ("Orderbon {0} ({1}): FOUT {2}" -f $bon.Name, $pad, $_.Exception.Message) }
    }

    # Na-calculatie (Excel) meesturen als WorkPortal erom vraagt (nieuw of gewijzigd)
    foreach ($pad in @($res.want_nacalc)) {
        if (-not $pad -or -not (Test-Path -LiteralPath $pad)) { continue }
        $nc = Get-ChildItem -LiteralPath $pad -File -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -match $NacalcPatroon } | Sort-Object LastWriteTime -Descending | Select-Object -First 1
        if (-not $nc) { continue }
        try {
            $body = @{ path = $pad; filename = $nc.Name; modified = $nc.LastWriteTimeUtc.ToString("o"); size = $nc.Length
                       data = [Convert]::ToBase64String((LeesBestand $nc.FullName)) } | ConvertTo-Json -Compress
            $r3 = Invoke-RestMethod -Uri "$WorkPortal/api/komdex/nacalculatie" -Method Post -Body ([Text.Encoding]::UTF8.GetBytes($body)) `
                -ContentType "application/json; charset=utf-8" -Headers @{ "X-WorkPortal-Key" = $Sleutel } -TimeoutSec 120
            Log ("Na-calculatie {0}: {1}" -f $nc.Name, $r3.resultaat)
        }
        catch { Log ("Na-calculatie {0}: FOUT {1}" -f $nc.Name, $_.Exception.Message) }
    }

    # Verkoopfacturen uit het DigiDossier: lijst met factuurmappen sturen, daarna de PDF's waar WorkPortal om vraagt
    if ($FactuurMap -and (Test-Path -LiteralPath $FactuurMap)) {
        try {
            $froot = (Get-Item -LiteralPath $FactuurMap).FullName.TrimEnd('\')
            $fVanaf = $FactuurVanaf
            $fmappen = New-Object System.Collections.Generic.List[object]
            $perJaar = [ordered]@{}
            foreach ($j in Get-ChildItem -LiteralPath $froot -Directory -ErrorAction SilentlyContinue | Sort-Object Name) {
                if ($j.Name -notmatch '^\d{4}$' -or [int]$j.Name -lt $fVanaf) { continue }
                $n = 0
                # factuurmap per factuur (de PDF mag ook een niveau dieper staan)
                foreach ($fm in Get-ChildItem -LiteralPath $j.FullName -Directory -ErrorAction SilentlyContinue) {
                    $alle = @(Get-ChildItem -LiteralPath $fm.FullName -File -Filter "*.pdf" -Recurse -ErrorAction SilentlyContinue)
                    $herinn = @($alle | Where-Object { $_.Name -match $HerinneringPatroon })
                    $pdf = FactuurPdf $alle
                    $info = [ordered]@{ path = $fm.FullName; name = $fm.Name; year = $j.Name }
                    if ($pdf) { $info.pdf = @{ name = $pdf.Name; modified = $pdf.LastWriteTimeUtc.ToString("o"); size = $pdf.Length } }
                    if ($herinn.Count) { $info.reminders = @($herinn | ForEach-Object { @{ name = $_.Name; modified = $_.LastWriteTimeUtc.ToString("o") } }) }
                    $fmappen.Add([pscustomobject]$info); $n++
                }
                # oudere jaren: PDF's direct in de jaarmap (zonder factuurmap)
                foreach ($pdf in Get-ChildItem -LiteralPath $j.FullName -File -Filter "*.pdf" -ErrorAction SilentlyContinue | Where-Object { $_.Name -notmatch $HerinneringPatroon }) {
                    $info = [ordered]@{ path = $pdf.FullName; name = $pdf.BaseName; year = $j.Name
                                        pdf = @{ name = $pdf.Name; modified = $pdf.LastWriteTimeUtc.ToString("o"); size = $pdf.Length } }
                    $fmappen.Add([pscustomobject]$info); $n++
                }
                $perJaar[$j.Name] = $n
            }
            $fjson = @{ folders = @($fmappen.ToArray()) } | ConvertTo-Json -Depth 4 -Compress
            $fres = Invoke-RestMethod -Uri "$WorkPortal/debiteuren/api/dossiers" -Method Post -Body ([Text.Encoding]::UTF8.GetBytes($fjson)) `
                -ContentType "application/json; charset=utf-8" -Headers @{ "X-WorkPortal-Key" = $Sleutel } -TimeoutSec 120
            if ($fres.fout) { Log ("Facturen: FOUT in WorkPortal: " + $fres.fout) }
            else { Log ("Facturen: {0} mappen ({3}), {1} nieuw, {2} PDF's gevraagd" -f $fres.mappen, $fres.nieuw, @($fres.want).Count, (($perJaar.GetEnumerator() | ForEach-Object { "$($_.Key): $($_.Value)" }) -join ", ")) }
            foreach ($pad in @($fres.want)) {
                if (-not $pad -or -not (Test-Path -LiteralPath $pad)) { continue }
                if (Test-Path -LiteralPath $pad -PathType Leaf) { $pdf = Get-Item -LiteralPath $pad }
                else { $pdf = FactuurPdf @(Get-ChildItem -LiteralPath $pad -File -Filter "*.pdf" -Recurse -ErrorAction SilentlyContinue) }
                if (-not $pdf) { continue }
                try {
                    $body = @{ path = $pad; filename = $pdf.Name; modified = $pdf.LastWriteTimeUtc.ToString("o"); size = $pdf.Length
                               data = [Convert]::ToBase64String((LeesBestand $pdf.FullName)) } | ConvertTo-Json -Compress
                    $r4 = Invoke-RestMethod -Uri "$WorkPortal/debiteuren/api/factuur" -Method Post -Body ([Text.Encoding]::UTF8.GetBytes($body)) `
                        -ContentType "application/json; charset=utf-8" -Headers @{ "X-WorkPortal-Key" = $Sleutel } -TimeoutSec 120
                    Log ("Factuur {0}: {1}" -f $pdf.Name, $r4.resultaat)
                }
                catch { Log ("Factuur {0}: FOUT {1}" -f $pdf.Name, $_.Exception.Message) }
            }
        }
        catch { Log ("Facturen: FOUT " + $_.Exception.Message) }
    }
}
catch {
    Log ("FOUT: " + $_.Exception.Message)
    exit 1
}
