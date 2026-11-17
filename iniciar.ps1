# AdvvxClips - instalador y lanzador (Windows PowerShell 5.1+)
$ErrorActionPreference = "Continue"
$ProgressPreference = "SilentlyContinue"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
$Root = $PSScriptRoot
Set-Location -LiteralPath $Root
function Paso($t) { Write-Host ""; Write-Host "==> $t" -ForegroundColor Cyan }
function Fallo($t) { Write-Host ""; Write-Host "ERROR: $t" -ForegroundColor Red; Read-Host "Pulsa Enter para salir"; exit 1 }

# 1) Python 3.10 - 3.13
$PyExe = $null; $PyArgs = @()
$cands = @( @("py", @("-3.12")), @("py", @("-3.11")), @("py", @("-3.13")), @("py", @("-3.10")), @("python", @()), @("python3", @()) )
foreach ($c in $cands) {
  $exe = $c[0]; $a1 = $c[1]
  try {
    $v = & $exe @a1 -c "import sys;print(sys.version_info[0]*100+sys.version_info[1])" 2>$null
    if ($LASTEXITCODE -eq 0 -and [int]$v -ge 310 -and [int]$v -le 313) { $PyExe = $exe; $PyArgs = $a1; break }
  } catch {}
}
if (-not $PyExe) { Fallo "No se encontro Python 3.10-3.13. Instala Python 3.12 desde https://www.python.org/downloads/ (marca la casilla 'Add python.exe to PATH') y vuelve a abrir este archivo." }

# 2) Entorno virtual
$VenvPy = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $VenvPy)) {
  Paso "Creando entorno de Python"
  & $PyExe @PyArgs -m venv .venv
  if (-not (Test-Path $VenvPy)) { Fallo "No se pudo crear el entorno virtual." }
}

# 3) Dependencias (solo si cambiaron)
$hash = (Get-FileHash -Algorithm MD5 -LiteralPath (Join-Path $Root "requirements.txt")).Hash
$mark = Join-Path $Root ".venv\.deps"
if ((-not (Test-Path $mark)) -or ((Get-Content $mark -Raw).Trim() -ne $hash)) {
  Paso "Instalando dependencias (solo la primera vez: 2 a 5 minutos)"
  & $VenvPy -m pip install --upgrade pip --quiet --disable-pip-version-check
  & $VenvPy -m pip install -r requirements.txt --disable-pip-version-check
  if ($LASTEXITCODE -ne 0) { Fallo "No se pudieron instalar las dependencias. Revisa tu conexion a internet." }
  Set-Content -Path $mark -Value $hash
}
# GPU NVIDIA: librerias CUDA para Whisper (opcional; si algo falla se usa CPU automaticamente)
$gpuMark = Join-Path $Root ".venv\.gpu"
if ((Get-Command nvidia-smi -ErrorAction SilentlyContinue) -and -not (Test-Path $gpuMark)) {
  Paso "GPU NVIDIA detectada: instalando aceleracion CUDA para la transcripcion"
  & $VenvPy -m pip install nvidia-cublas-cu12 "nvidia-cudnn-cu12==9.*" --disable-pip-version-check
  Set-Content -Path $gpuMark -Value "1"
}

# 4) ffmpeg y yt-dlp
New-Item -ItemType Directory -Force -Path (Join-Path $Root "bin") | Out-Null
$ff = Join-Path $Root "bin\ffmpeg.exe"; $fp = Join-Path $Root "bin\ffprobe.exe"
if (-not ((Test-Path $ff) -and (Test-Path $fp))) {
  if ((Get-Command ffmpeg -ErrorAction SilentlyContinue) -and (Get-Command ffprobe -ErrorAction SilentlyContinue)) {
    Write-Host "ffmpeg encontrado en el PATH."
  } else {
    Paso "Descargando ffmpeg (unos 150 MB, solo la primera vez)"
    $zip = Join-Path $env:TEMP "tbx-ffmpeg.zip"; $tmp = Join-Path $env:TEMP "tbx-ffmpeg"
    try {
      Invoke-WebRequest -UseBasicParsing -Uri "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip" -OutFile $zip
      if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp }
      Expand-Archive -LiteralPath $zip -DestinationPath $tmp -Force
      Get-ChildItem -Recurse -Path $tmp -Include ffmpeg.exe, ffprobe.exe | ForEach-Object { Copy-Item $_.FullName (Join-Path $Root "bin") -Force }
      Remove-Item -Force $zip; Remove-Item -Recurse -Force $tmp
    } catch { Write-Host $_.Exception.Message -ForegroundColor Yellow }
    if (-not (Test-Path $ff)) { Fallo "No se pudo descargar ffmpeg. Descargalo manualmente (ffmpeg.exe y ffprobe.exe) y copialos a la carpeta 'bin'." }
  }
}
$yt = Join-Path $Root "bin\yt-dlp.exe"
if (-not (Test-Path $yt)) {
  Paso "Descargando yt-dlp"
  try { Invoke-WebRequest -UseBasicParsing -Uri "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe" -OutFile $yt } catch { Write-Host $_.Exception.Message -ForegroundColor Yellow }
  if (-not (Test-Path $yt)) { Fallo "No se pudo descargar yt-dlp. Descargalo de github.com/yt-dlp/yt-dlp/releases y copialo a la carpeta 'bin'." }
} else {
  # YouTube y Kick cambian a menudo: actualiza yt-dlp como maximo una vez al dia
  $stamp = Join-Path $Root "bin\.ytdlp_checked"
  if ((-not (Test-Path $stamp)) -or ((Get-Item $stamp).LastWriteTime -lt (Get-Date).AddHours(-24))) {
    Paso "Buscando actualizacion de yt-dlp"
    & $yt -U 2>$null | Out-Host
    Set-Content -Path $stamp -Value (Get-Date)
  }
}

# 5) Iniciar
Paso "Iniciando AdvvxClips (se abrira tu navegador)"
Write-Host "Deja esta ventana abierta mientras uses la app. Cierrala para salir." -ForegroundColor DarkGray
& $VenvPy -m app
if ($LASTEXITCODE -ne 0) { Fallo "La aplicacion se cerro con un error (codigo $LASTEXITCODE)." }
