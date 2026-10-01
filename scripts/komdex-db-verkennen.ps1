# komdex-db-verkennen.ps1  -  ALLEEN LEZEN
# Zoekt op DC01 in de SQL-database waar de order-eigenschappen (Vaste prijs, Geleverd, Afgesloten, ...) staan.
# Er wordt NIETS gewijzigd: alleen SELECT-opdrachten. Resultaat: komdex-db-verkenning.txt naast dit script.
# Starten: rechtsklik > Uitvoeren met PowerShell (als een gebruiker die in de Komdex-database mag lezen).

$Server   = ""        # leeg = automatisch zoeken; anders bv. "DC01\SQLEXPRESS"
$Zoekwoorden = @("Vaste prijs", "Afgefactureerd", "Afgesloten", "Order Vervallen", "Deellevering")
$MaxRijen = 20000     # tabellen groter dan dit worden niet op tekst doorzocht

$uit = Join-Path $PSScriptRoot "komdex-db-verkenning.txt"
"Komdex database-verkenning  $(Get-Date)" | Out-File $uit -Encoding UTF8
function Log($t){ $t; $t | Out-File $uit -Append -Encoding UTF8 }

# 1. SQL-instanties op deze server
$inst = @()
try { $inst = (Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Microsoft SQL Server' -ErrorAction Stop).InstalledInstances } catch {}
Log "SQL-instanties in register: $($inst -join ', ')"
Get-Service | Where-Object { $_.Name -like 'MSSQL*' } | ForEach-Object { Log "  Service: $($_.Name) - $($_.Status)" }
if (-not $Server) {
  if ($inst -contains 'MSSQLSERVER') { $Server = "localhost" } elseif ($inst.Count -gt 0) { $Server = "localhost\$($inst[0])" } else { $Server = "localhost" }
}
Log "Gebruikte server: $Server"

function Q($db, $sql) {
  $c = New-Object System.Data.SqlClient.SqlConnection "Server=$Server;Database=$db;Integrated Security=True;Connect Timeout=10;ApplicationIntent=ReadOnly"
  $c.Open(); $cmd = $c.CreateCommand(); $cmd.CommandText = $sql; $cmd.CommandTimeout = 60
  $t = New-Object System.Data.DataTable; $t.Load($cmd.ExecuteReader()); $c.Close(); return ,$t
}

try { $dbs = Q "master" "SELECT name FROM sys.databases WHERE database_id > 4 ORDER BY name" }
catch { Log "FOUT bij verbinden met $Server : $($_.Exception.Message)"; Log "Vul bovenin `$Server in (bv. DC01\SQLEXPRESS) en probeer opnieuw."; Read-Host "Enter om te sluiten"; exit }
Log "Databases: $(($dbs | ForEach-Object { $_.name }) -join ', ')"

foreach ($d in $dbs) {
  $db = $d.name
  Log ""; Log "===== Database: $db ====="
  try {
    # 2. Tabellen/kolommen met verdachte namen
    $kol = Q $db @"
SELECT t.name AS tabel, c.name AS kolom, ty.name AS type
FROM sys.columns c JOIN sys.tables t ON t.object_id=c.object_id JOIN sys.types ty ON ty.user_type_id=c.user_type_id
WHERE c.name LIKE '%eigensch%' OR c.name LIKE '%status%' OR c.name LIKE '%kenmerk%' OR c.name LIKE '%vast%'
   OR c.name LIKE '%geleverd%' OR c.name LIKE '%afgeslot%' OR c.name LIKE '%factu%' OR c.name LIKE '%vervall%'
   OR c.name LIKE '%flag%' OR c.name LIKE '%prop%' OR t.name LIKE '%eigensch%' OR t.name LIKE '%kenmerk%' OR t.name LIKE '%status%'
ORDER BY t.name, c.name
"@
    Log "-- Kolommen met verdachte naam:"
    $kol | ForEach-Object { Log ("   {0}.{1} ({2})" -f $_.tabel, $_.kolom, $_.type) }

    # 3. Tekstkolommen doorzoeken op de namen van de vinkjes (kleine tabellen)
    $tk = Q $db @"
SELECT s.name AS sch, t.name AS tabel, c.name AS kolom, SUM(p.rows) AS rijen
FROM sys.columns c JOIN sys.tables t ON t.object_id=c.object_id JOIN sys.schemas s ON s.schema_id=t.schema_id
JOIN sys.types ty ON ty.user_type_id=c.user_type_id
JOIN sys.partitions p ON p.object_id=t.object_id AND p.index_id IN (0,1)
WHERE ty.name IN ('varchar','nvarchar','char','nchar') AND (c.max_length = -1 OR c.max_length >= 10)
GROUP BY s.name, t.name, c.name HAVING SUM(p.rows) BETWEEN 1 AND $MaxRijen
"@
    Log "-- Tekst gevonden:"
    foreach ($r in $tk) {
      $w = ($Zoekwoorden | ForEach-Object { "[$($r.kolom)] LIKE N'%$_%'" }) -join " OR "
      try {
        $hit = Q $db "SELECT TOP 5 CAST([$($r.kolom)] AS nvarchar(200)) AS v FROM [$($r.sch)].[$($r.tabel)] WHERE $w"
        if ($hit.Rows.Count -gt 0) {
          Log ("   {0}.{1}.{2} ({3} rijen): {4}" -f $r.sch, $r.tabel, $r.kolom, $r.rijen, (($hit | ForEach-Object { $_.v }) -join ' | '))
          $cols = Q $db "SELECT c.name FROM sys.columns c WHERE c.object_id=OBJECT_ID('[$($r.sch)].[$($r.tabel)]') ORDER BY column_id"
          Log ("      kolommen: " + (($cols | ForEach-Object { $_.name }) -join ', '))
        }
      } catch {}
    }
  } catch { Log "   (geen toegang: $($_.Exception.Message))" }
}
Log ""; Log "Klaar. Stuur komdex-db-verkenning.txt door."
Read-Host "Enter om te sluiten"
