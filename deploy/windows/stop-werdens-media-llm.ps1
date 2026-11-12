# Werdens Media LLM - alles stoppen op Windows.
#
# Doe dit via stop-werdens-media-llm.bat (dubbele klik). Stopt de drie lagen en
# de rekenkern van llama.cpp, zodat er geen geheugen bezet blijft.

$ErrorActionPreference = 'Continue'
$gestopt = 0

# De drie lagen (ze draaien als python.exe met het script in de opdrachtregel).
$lagen = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -match 'wllm\.py|wmlm_workspace\.py|wmlm_media\.py' }
foreach ($proces in $lagen) {
    Stop-Process -Id $proces.ProcessId -Force -ErrorAction SilentlyContinue
    $gestopt++
    Write-Host "  gestopt: laag (pid $($proces.ProcessId))"
}

# De rekenkernen van llama.cpp.
foreach ($kern in (Get-Process -Name 'llama-server' -ErrorAction SilentlyContinue)) {
    Stop-Process -Id $kern.Id -Force -ErrorAction SilentlyContinue
    $gestopt++
    Write-Host "  gestopt: rekenkern (pid $($kern.Id))"
}

if ($gestopt -eq 0) {
    Write-Host '  er liep niets van Werdens Media LLM'
} else {
    Write-Host ''
    Write-Host "  klaar: $gestopt proces(sen) gestopt"
}
