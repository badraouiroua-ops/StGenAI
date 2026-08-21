<#
.SYNOPSIS
    Lance le pipeline complet (extraction + RAG + LLM + corrections) sur les
    Application Notes du dossier Input\41\.

.USAGE
    .\run_all_an.ps1               # traite le dossier 41 avec les defauts
    .\run_all_an.ps1 -AnFolder 41  # equivalent explicite
    .\run_all_an.ps1 -Workers 4    # surcharge le nombre de workers
#>

param(
    [string]$AnFolder  = "41",
    [int]   $Workers   = 8,
    [string]$LogFile   = "run_all_an.log"
)

$ErrorActionPreference = "Continue"

function Log {
    param([string]$Msg)
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') | $Msg"
    Write-Host $line
    Add-Content -Path $LogFile -Value $line
}

$pdfCount = @(Get-ChildItem -LiteralPath "Input\$AnFolder" -Filter *.pdf -ErrorAction SilentlyContinue).Count
Log "========================================"
Log "DEBUT pipeline Application Notes"
Log "Dossier : Input\$AnFolder  |  $pdfCount PDFs  |  $Workers workers"
Log "========================================"

# ── Etape 1 : Extraction des tables (pdfplumber) ─────────────────────────────
Log ""
Log "ETAPE 1 — Extraction des tables (main.py --an $AnFolder)"
$sw = [System.Diagnostics.Stopwatch]::StartNew()
python table_extractor_raw\main.py --an $AnFolder --workers $Workers
if ($LASTEXITCODE -eq 0) {
    Log "  <- Etape 1 OK ($($sw.Elapsed.TotalMinutes.ToString('0.0')) min)"
} else {
    Log "  <- Etape 1 ECHEC (exit code $LASTEXITCODE)"
}

Start-Sleep -Seconds 5
[System.GC]::Collect()
Start-Sleep -Seconds 5

# ── Etape 2 : Construction RAG selective ────────────────────────────────────
Log ""
Log "ETAPE 2 — Construction RAG selective (build_rag_selective.py --family $AnFolder)"
$sw = [System.Diagnostics.Stopwatch]::StartNew()
python table_extractor_raw\build_rag_selective.py --family $AnFolder
if ($LASTEXITCODE -eq 0) {
    Log "  <- Etape 2 OK ($($sw.Elapsed.TotalMinutes.ToString('0.0')) min)"
} else {
    Log "  <- Etape 2 ECHEC (exit code $LASTEXITCODE)"
}

Start-Sleep -Seconds 5
[System.GC]::Collect()
Start-Sleep -Seconds 5

# ── Etape 3 : Validation + correction LLM ───────────────────────────────────
Log ""
Log "ETAPE 3 — Validation LLM (BatchLLMValidation.py --an $AnFolder)"
$sw = [System.Diagnostics.Stopwatch]::StartNew()
python PipelineViaLLM\BatchLLMValidation.py --an $AnFolder --workers $Workers
if ($LASTEXITCODE -eq 0) {
    Log "  <- Etape 3 OK ($($sw.Elapsed.TotalMinutes.ToString('0.0')) min)"
} else {
    Log "  <- Etape 3 ECHEC (exit code $LASTEXITCODE)"
}

Start-Sleep -Seconds 5
[System.GC]::Collect()
Start-Sleep -Seconds 5

# ── Etape 4 : Application des corrections finales ───────────────────────────
Log ""
Log "ETAPE 4 — Application corrections (ApplyCorrections.py --an $AnFolder)"
$sw = [System.Diagnostics.Stopwatch]::StartNew()
python PipelineViaLLM\ApplyCorrections.py --an $AnFolder
if ($LASTEXITCODE -eq 0) {
    Log "  <- Etape 4 OK ($($sw.Elapsed.TotalMinutes.ToString('0.0')) min)"
} else {
    Log "  <- Etape 4 ECHEC (exit code $LASTEXITCODE)"
}

Log ""
Log "========================================"
Log "PIPELINE AN TERMINE — dossier : $AnFolder"
Log "Outputs dans : Output\Json\Final_Tables\$AnFolder\"
Log "========================================"
