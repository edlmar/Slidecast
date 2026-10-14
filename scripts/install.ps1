# slidecast installer (run through Install.cmd). Installs pixi if needed, the Python environment
# (~6 GB, PyTorch with CUDA), ffmpeg and LibreOffice (through winget, if they are missing), the
# voice model (3.8 GB), builds Slidecast.exe and creates Desktop / Start menu shortcuts.
# Safe to run again: it only does what is still missing, so it also repairs and updates.
param([switch]$NoShortcuts)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Step($text) { Write-Host ""; Write-Host "==> $text" -ForegroundColor Cyan }
function Info($text) { Write-Host "    $text" }
function Warn($text) { Write-Host "    ! $text" -ForegroundColor Yellow }

Write-Host "slidecast - PowerPoint deck to narrated video" -ForegroundColor Cyan
Write-Host "Folder: $root"

if ($root -match '[^\x20-\x7E]' -or $root -match ' ') {
    Warn "The folder path has spaces or non-Latin letters. Some GPU libraries break on such"
    Warn "paths; if anything fails, move the folder to e.g. C:\slidecast and run Install.cmd again."
}

# -- graphics card -------------------------------------------------------------------------------
Step "Checking the graphics card"
$smi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
if ($smi) {
    $gpu = (& nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader) |
        Select-Object -First 1
    Info $gpu
    $driver = [double](($gpu -split ',')[1].Trim() -replace '^(\d+\.\d+).*', '$1')
    if ($driver -lt 570) {
        Warn "NVIDIA driver $driver is too old; update it (570 or newer) from nvidia.com."
    }
} else {
    Warn "No NVIDIA graphics card found. slidecast will run on the processor, which is much slower."
}

# -- pixi ----------------------------------------------------------------------------------------
Step "pixi (installs and runs slidecast's Python environment)"
function Find-Pixi {
    $found = (Get-Command pixi -ErrorAction SilentlyContinue).Source
    foreach ($c in @("$env:LOCALAPPDATA\pixi\bin\pixi.exe", "$env:USERPROFILE\.pixi\bin\pixi.exe")) {
        if (-not $found -and (Test-Path $c)) { $found = $c }
    }
    return $found
}
$pixi = Find-Pixi
if (-not $pixi) {
    Info "installing pixi from https://pixi.sh ..."
    Invoke-RestMethod -UseBasicParsing https://pixi.sh/install.ps1 | Invoke-Expression
    $pixi = Find-Pixi
    if (-not $pixi) { throw "pixi installation failed" }
}
Info $pixi

Step "Python environment (PyTorch with CUDA, ~6 GB download the first time)"
& $pixi install
if ($LASTEXITCODE) { throw "pixi install failed ($LASTEXITCODE)" }
& $pixi run setup-tts
if ($LASTEXITCODE) { throw "installing the voice engine failed ($LASTEXITCODE)" }

# -- ffmpeg and LibreOffice ------------------------------------------------------------------------
$winget = (Get-Command winget -ErrorAction SilentlyContinue).Source

function Install-WithWinget($id, $name, $url) {
    if (-not $winget) {
        Warn "$name is missing and winget is not available. Install it from $url, then run"
        Warn "Install.cmd again."
        return
    }
    Info "installing $name (winget) ..."
    & $winget install --exact --id $id --silent --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE) { Warn "winget could not install $name. Install it from $url." }
}

Step "ffmpeg (encodes the video)"
$ffmpeg = (Get-Command ffmpeg -ErrorAction SilentlyContinue).Source
if (-not $ffmpeg) {
    $ffmpeg = Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages\Gyan.FFmpeg*\*\bin\ffmpeg.exe" `
        -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty FullName
}
if ($ffmpeg) { Info $ffmpeg } else { Install-WithWinget 'Gyan.FFmpeg' 'ffmpeg' 'https://ffmpeg.org' }

Step "LibreOffice (turns slides into images)"
$soffice = @("$env:ProgramFiles\LibreOffice\program\soffice.exe",
             "${env:ProgramFiles(x86)}\LibreOffice\program\soffice.exe") |
    Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
if ($soffice) {
    Info $soffice
} else {
    Install-WithWinget 'TheDocumentFoundation.LibreOffice' 'LibreOffice' 'https://www.libreoffice.org'
}

# -- voice model and self-check ------------------------------------------------------------------
Step "Voice model and self-check (downloads 3.8 GB the first time)"
& $pixi run doctor
$doctorFailed = [bool]$LASTEXITCODE
if ($doctorFailed) { Warn "Some checks failed (see above). Fix them and run Install.cmd again." }

# -- launcher and shortcuts ----------------------------------------------------------------------
Step "Slidecast.exe"
$exe = Join-Path $root 'Slidecast.exe'
$ico = Join-Path $root 'slidecast\gui\slidecast.ico'
$csc = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path $csc)) { $csc = Join-Path $env:WINDIR 'Microsoft.NET\Framework\v4.0.30319\csc.exe' }
& $csc /nologo /target:winexe /optimize "/out:$exe" "/win32icon:$ico" `
    /reference:System.Windows.Forms.dll (Join-Path $root 'scripts\Slidecast.cs')
if ($LASTEXITCODE) { throw "building Slidecast.exe failed ($LASTEXITCODE)" }
Info $exe

if ($NoShortcuts) { Step "Done (shortcuts skipped)"; exit [int]$doctorFailed }
Step "Shortcuts"
$shell = New-Object -ComObject WScript.Shell
$places = @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Programs'))
foreach ($dir in $places) {
    $lnk = $shell.CreateShortcut((Join-Path $dir 'slidecast.lnk'))
    $lnk.TargetPath = $exe
    $lnk.WorkingDirectory = $root
    $lnk.IconLocation = "$ico,0"
    $lnk.Description = 'slidecast - PowerPoint deck to narrated video'
    $lnk.Save()
    Info "$dir\slidecast.lnk"
}

Step "Done"
Info "Open slidecast from the Desktop or the Start menu."
Info "First: pick a 10-20 second recording of your voice in the Voice step."
exit [int]$doctorFailed
