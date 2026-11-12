# Werdens Media LLM - de drie lagen starten op Windows.
#
# Doe dit via start-werdens-media-llm.bat (dubbele klik), of vanaf de
# PowerShell-regel:
#     powershell -ExecutionPolicy Bypass -File .\start-werdens-media-llm.ps1
#
# De eerste keer worden llama.cpp en de modellen opgehaald (circa 2,5 GB, eenmalig).
# Daarna starten de drie lagen meteen.

$ErrorActionPreference = 'Stop'
$Wortel = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $Wortel

function Meld($tekst) { Write-Host $tekst }

Meld ''
Meld 'Werdens Media LLM - starten'
Meld "  map: $Wortel"
Meld ''

# ---- Python zoeken ---------------------------------------------------------
$Python = $null
$Voor = @()
foreach ($kandidaat in @('py', 'python')) {
    if (Get-Command $kandidaat -ErrorAction SilentlyContinue) {
        $Python = $kandidaat
        if ($kandidaat -eq 'py') { $Voor = @('-3') }
        break
    }
}
if (-not $Python) {
    Meld '  ! Python 3 is niet gevonden.'
    Meld '    Installeer het via https://www.python.org/downloads/windows/'
    Meld '    en zet een vinkje bij "Add python.exe to PATH".'
    exit 1
}
$versie = (& $Python @Voor --version 2>&1) -join ' '
Meld "  python : $versie"

# ---- rekenkracht en modellen ----------------------------------------------
$server = Get-ChildItem -Path (Join-Path $Wortel 'engine') -Recurse -Filter 'llama-server.exe' `
    -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $server) {
    Meld ''
    Meld '  Nog niet klaargezet: llama.cpp en de modellen worden nu opgehaald.'
    Meld '  Dit is eenmalig en duurt een paar minuten (circa 2,5 GB).'
    Meld ''
    & $Python @Voor (Join-Path $PSScriptRoot 'setup.py') --home $Wortel
    if ($LASTEXITCODE -ne 0) {
        Meld ''
        Meld '  ! Klaarzetten mislukt. Draai setup.py opnieuw om te zien waar het misgaat:'
        Meld "    $Python $(if ($Voor) { $Voor -join ' ' }) deploy\windows\setup.py --home `"$Wortel`""
        exit 1
    }
    Meld ''
}

# ---- de drie lagen starten -------------------------------------------------
$log = Join-Path $Wortel 'logs'
New-Item -ItemType Directory -Force -Path $log | Out-Null

# Bereikbaar voor andere apparaten in huis (telefoon, laptop). Windows vraagt
# eenmalig om toestemming in de firewall; sta het toe voor je eigen netwerk.
$env:WLLM_HOST = '0.0.0.0'
$env:WLLM_PORT = '11434'
$env:WMLM_HOST = '0.0.0.0'
$env:WMLM_PORT = '8090'
$env:WMLM_MEDIA_HOST = '0.0.0.0'
$env:WMLM_MEDIA_PORT = '8091'

$lagen = @(
    @{ naam = 'engine';     script = 'engine\wllm.py';             poort = 11434; wat = 'praten met de AI' },
    @{ naam = 'werkruimte'; script = 'workspace\wmlm_workspace.py'; poort = 8090;  wat = 'documenten en bronnen' },
    @{ naam = 'media';      script = 'media\wmlm_media.py';         poort = 8091;  wat = 'spraak in en uit' }
)

foreach ($laag in $lagen) {
    $naam = $laag.naam
    Start-Process -FilePath $Python `
        -ArgumentList (@Voor + @((Join-Path $Wortel $laag.script), 'serve')) `
        -WorkingDirectory $Wortel `
        -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $log "$naam.log") `
        -RedirectStandardError (Join-Path $log "$naam-fout.log")
    Meld "  gestart : $naam ($($laag.wat))"
}

# ---- wachten tot ze antwoorden --------------------------------------------
Meld ''
foreach ($laag in $lagen) {
    $poort = $laag.poort
    $klaar = $false
    for ($poging = 0; $poging -lt 40; $poging++) {
        try {
            $antwoord = Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 "http://127.0.0.1:$poort/"
            if ($antwoord.StatusCode -eq 200) { $klaar = $true; break }
        } catch {
            Start-Sleep -Milliseconds 700
        }
    }
    if ($klaar) {
        Meld ("  poort {0,-6} bereikbaar   ({1})" -f $poort, $laag.naam)
    } else {
        Meld ("  poort {0,-6} nog stil     ({1}) - kijk in logs\$($laag.naam)-fout.log" -f $poort, $laag.naam)
    }
}

Meld ''
Meld '  Chatten : http://127.0.0.1:11434/chat'
Meld '  Werkruimte: http://127.0.0.1:8090'
Meld '  Media   : http://127.0.0.1:8091'
Meld '  Stoppen : stop-werdens-media-llm.bat'
Meld ''

Start-Process 'http://127.0.0.1:11434/chat'
