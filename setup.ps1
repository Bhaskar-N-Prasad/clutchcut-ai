# One-command setup for Windows + NVIDIA GPU:   powershell -ExecutionPolicy Bypass -File setup.ps1
#
# Creates .venv\, installs Python packages, checks ffmpeg / yt-dlp, downloads a portable Ollama into
# tools\ollama\ and pulls the vision model into models\ollama\. Nothing is installed as a service.
# Re-running is safe: finished steps are skipped.
#
#   -SkipVision      don't download Ollama or the Qwen model (you can still run prepare + HUD scans)
#   -Model <name>    Ollama vision model to pull (default qwen3-vl:8b, about 6 GB)
#   -CudaIndex <url> PyTorch wheel index (default CUDA 12.8; use https://download.pytorch.org/whl/cpu for CPU only)
param(
    [switch]$SkipVision,
    [string]$Model = "qwen3-vl:8b",
    [string]$CudaIndex = "https://download.pytorch.org/whl/cu128"
)
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
Set-Location $root

function Step($msg) { Write-Host "`n== $msg" -ForegroundColor Cyan }

Step "Python"
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) { throw "Python 3.10-3.12 is needed: winget install Python.Python.3.12" }
& python -c "import sys; assert sys.version_info >= (3, 10), sys.version"
if ($LASTEXITCODE -ne 0) { throw "Python 3.10 or newer is needed." }
if (-not (Test-Path "$root\.venv\Scripts\python.exe")) { & python -m venv "$root\.venv" }
$vpy = "$root\.venv\Scripts\python.exe"
& $vpy -m pip install --upgrade pip --quiet

Step "PyTorch (from $CudaIndex)"
& $vpy -c "import torch" 2>$null
if ($LASTEXITCODE -ne 0) { & $vpy -m pip install torch torchaudio --index-url $CudaIndex }

Step "Python packages"
& $vpy -m pip install -r "$root\requirements.txt"
& $vpy -c "import torch; print('CUDA available:', torch.cuda.is_available(), torch.cuda.get_device_name(0) if torch.cuda.is_available() else '')"

Step "ffmpeg and yt-dlp"
foreach ($pair in @(@("ffmpeg", "Gyan.FFmpeg"), @("yt-dlp", "yt-dlp.yt-dlp"))) {
    if (-not (Get-Command $pair[0] -ErrorAction SilentlyContinue)) {
        Write-Host "Installing $($pair[0]) with winget..."
        winget install --id $pair[1] -e --accept-source-agreements
    } else { Write-Host "$($pair[0]) found." }
}

if (-not $SkipVision) {
    Step "Portable Ollama (tools\ollama, runs only during a job)"
    $ollama = "$root\tools\ollama\ollama.exe"
    if (-not (Test-Path $ollama) -and -not (Get-Command ollama -ErrorAction SilentlyContinue)) {
        New-Item -ItemType Directory -Force "$root\tools\ollama" | Out-Null
        $zip = "$root\tools\ollama-windows-amd64.zip"
        Invoke-WebRequest "https://github.com/ollama/ollama/releases/latest/download/ollama-windows-amd64.zip" -OutFile $zip
        Expand-Archive $zip "$root\tools\ollama" -Force
        Remove-Item $zip
    }
    Step "Vision model $Model (into models\ollama)"
    & $vpy "$root\scripts\local_watch.py" --pull --model $Model
}

Step "Done"
Write-Host "Copy preferences.example.md to preferences.md and edit it."
Write-Host "Then, in Claude Code from this folder:  /clutchcut <YouTube link or video file>"
Write-Host "Whisper, Demucs and PANNs weights download on first use (about 4 GB in models\)."
