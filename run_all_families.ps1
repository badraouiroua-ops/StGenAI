param(
    [string]$LogFile = "run_all_families.log"
)

$ErrorActionPreference = "Continue"

function Log {
    param([string]$Msg)
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') | $Msg"
    Write-Host $line
    Add-Content -Path $LogFile -Value $line
}

function Get-WorkerCount {
    param([int]$PdfCount)
    if ($PdfCount -le 3) { return 4 }
    if ($PdfCount -le 8) { return 8 }
    if ($PdfCount -le 14) { return 12 }
    return 16
}

# Déterminer l'ordre des familles et leur nombre de PDFs
$families = Get-ChildItem -LiteralPath DataSHEET -Directory | Sort-Object Name

foreach ($fam in $families) {
    $name = $fam.Name
    $pdfCount = @(Get-ChildItem -LiteralPath $fam.FullName -Filter *.pdf).Count
    $workers = Get-WorkerCount -PdfCount $pdfCount

    Log "========================================"
    Log "DEBUT famille $name ($pdfCount PDFs, $workers workers)"
    Log "========================================"

    # Étape 1 : extraction des tables
    Log "  -> main.py --family $name --workers $workers"
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    python table_extractor_raw\main.py --family $name --workers $workers
    if ($LASTEXITCODE -eq 0) {
        Log "  <- main.py OK ($($sw.Elapsed.TotalMinutes.ToString('0.0')) min)"
    } else {
        Log "  <- main.py ECHEC (exit code $LASTEXITCODE)"
    }

    Start-Sleep -Seconds 5
    [System.GC]::Collect()
    Start-Sleep -Seconds 5

    # Étape 2 : construction RAG selective
    Log "  -> build_rag_selective.py --family $name"
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    python table_extractor_raw\build_rag_selective.py --family $name
    if ($LASTEXITCODE -eq 0) {
        Log "  <- build_rag_selective.py OK ($($sw.Elapsed.TotalMinutes.ToString('0.0')) min)"
    } else {
        Log "  <- build_rag_selective.py ECHEC (exit code $LASTEXITCODE)"
    }

    Start-Sleep -Seconds 5
    [System.GC]::Collect()
    Start-Sleep -Seconds 5

    Log "FIN famille $name"
    Log ""
}

Log "========================================"
Log "TOUTES LES FAMILLES TERMINEES"
Log "========================================"
