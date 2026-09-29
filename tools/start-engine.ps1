# Desktop launcher: wait for the box on USB, start the engine in this window, show the viewer URL.
# Engine starts master=0, NOT armed. Ctrl-C or closing the window stops it.
# No auto-restart after a fault/USB drop -- PlaStim presses Enter to go again.
param([string]$Port = "COM13", [string]$Mode = "threephase")

$Root = Split-Path -Parent $PSScriptRoot
$Py = Join-Path $Root "venv\Scripts\python.exe"
$Url = "http://127.0.0.1:8321/"
$Host.UI.RawUI.WindowTitle = "stim engine"
Set-Location $Root

function Show-Url {
    Write-Host ""
    Write-Host "  Viewer:  $Url" -ForegroundColor Green
    Write-Host ""
}

# Already running? Don't start a second one.
$held = Get-NetTCPConnection -LocalPort 8321 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($held) {
    $cmd = (Get-CimInstance Win32_Process -Filter "ProcessId=$($held.OwningProcess)").CommandLine
    Write-Host "An engine is already running on :8321 (pid $($held.OwningProcess))" -ForegroundColor Yellow
    Write-Host "  $cmd"
    Show-Url
    Read-Host "Press Enter to close"
    exit
}

while ($true) {
    if (-not ([System.IO.Ports.SerialPort]::GetPortNames() -contains $Port)) {
        Write-Host -NoNewline "Waiting for USB ($Port)... plug in the box" -ForegroundColor Yellow
        while (-not ([System.IO.Ports.SerialPort]::GetPortNames() -contains $Port)) {
            Start-Sleep -Milliseconds 1000
            Write-Host -NoNewline "."
        }
        Write-Host ""
        Write-Host "$Port is up." -ForegroundColor Green
        Start-Sleep -Seconds 2   # let the port settle after enumeration
    }

    Show-Url
    & $Py -m stimengine.tools.serve --serial $Port --mode $Mode
    $rc = $LASTEXITCODE

    if ($rc -eq 2) {
        # Couldn't open the link (port busy -- restim open? -- or box still booting). Nothing was armed; retry.
        Write-Host "Link didn't open; retrying in 5s (close restim if it's holding $Port)..." -ForegroundColor Yellow
        Start-Sleep -Seconds 5
        continue
    }
    Write-Host ""
    Write-Host "Engine exited (code $rc)." -ForegroundColor Yellow
    Read-Host "Press Enter to wait for USB and start again, or close this window"
}
