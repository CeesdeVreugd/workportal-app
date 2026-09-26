<#
  WorkPortal - DVP: nieuwe ordermappen uit Komdex doorgeven aan WorkPortal.

  Draai dit script als geplande taak (elke 5 minuten) op een server die bij de Komdex-ordermap kan.
  Het stuurt de lijst met ordermappen via HTTPS naar WorkPortal, en de orderbon (PDF met "orderbon" in
  de naam) uit nieuwe ordermappen. Met het ordertype op de bon maakt WorkPortal het project of de order
  zelf aan; anders komt de map in het actievenster "Nieuwe orders". Er wordt niets gewijzigd in de
  Komdex-map (alleen lezen).

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
$Logbestand = "$PSScriptRoot\komdex-orders.log"
# -------------------------------------------------------------------------

$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

function Log($tekst) {
    $regel = "{0:yyyy-MM-dd HH:mm:ss}  {1}" -f (Get-Date), $tekst
    Add-Content -Path $Logbestand -Value $regel -Encoding UTF8
    # log klein houden
    if ((Get-Item $Logbestand).Length -gt 1MB) { Get-Content $Logbestand -Tail 2000 | Set-Content $Logbestand -Encoding UTF8 }
}

try {
    if (-not (Test-Path -LiteralPath $OrderMap)) { throw "Map niet gevonden: $OrderMap" }
    $root = (Get-Item -LiteralPath $OrderMap).FullName.TrimEnd('\')
    $vanafJaar = (Get-Date).Year - $Jaren + 1
    $mappen = New-Object System.Collections.Generic.List[object]
    foreach ($d in Get-ChildItem -LiteralPath $root -Directory -ErrorAction SilentlyContinue) {
        if ($d.Name -match '^\d{8}') {
            # ordermap direct in de hoofdmap
            $mappen.Add([pscustomobject]@{ path = $d.FullName; name = $d.Name; parent = "" })
        }
        elseif ($d.Name -match '^\d{4}$' -and [int]$d.Name -ge $vanafJaar) {
            # jaarmap: de ordermappen staan daarin
            foreach ($o in Get-ChildItem -LiteralPath $d.FullName -Directory -ErrorAction SilentlyContinue) {
                if ($o.Name -match '^\d{8}') {
                    $mappen.Add([pscustomobject]@{ path = $o.FullName; name = $o.Name; parent = "" })
                }
            }
        }
    }
    $json  = @{ folders = @($mappen.ToArray()) } | ConvertTo-Json -Depth 4 -Compress
    $bytes = [Text.Encoding]::UTF8.GetBytes($json)
    $res = Invoke-RestMethod -Uri "$WorkPortal/api/komdex/orders" -Method Post -Body $bytes `
        -ContentType "application/json; charset=utf-8" -Headers @{ "X-WorkPortal-Key" = $Sleutel } -TimeoutSec 60
    Log ("OK: {0} ordermappen, {1} nieuw{2}" -f $res.ordermappen, $res.nieuw, $(if ($res.eerste_keer) { " (eerste keer: bestaande mappen onthouden)" } else { "" }))

    # Orderbon (PDF) meesturen voor de mappen waar WorkPortal nog om vraagt
    foreach ($pad in @($res.want)) {
        if (-not $pad -or -not (Test-Path -LiteralPath $pad)) { continue }
        $bon = Get-ChildItem -LiteralPath $pad -File -Filter "*orderbon*.pdf" -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending | Select-Object -First 1
        if (-not $bon) { continue }
        $body = @{ path = $pad; filename = $bon.Name; data = [Convert]::ToBase64String([IO.File]::ReadAllBytes($bon.FullName)) } |
            ConvertTo-Json -Compress
        $r2 = Invoke-RestMethod -Uri "$WorkPortal/api/komdex/orderbon" -Method Post -Body ([Text.Encoding]::UTF8.GetBytes($body)) `
            -ContentType "application/json; charset=utf-8" -Headers @{ "X-WorkPortal-Key" = $Sleutel } -TimeoutSec 120
        Log ("Orderbon {0}: {1} {2}" -f $bon.Name, $r2.ordertype, $r2.resultaat)
    }
}
catch {
    Log ("FOUT: " + $_.Exception.Message)
    exit 1
}
