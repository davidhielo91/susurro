# Instalador de Susurro para Windows.
# Uso: doble clic en instalar.bat (o ejecutar este archivo con PowerShell).
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Paso($texto) { Write-Host "`n==> $texto" -ForegroundColor Cyan }
function Falla($texto) {
    Write-Host "`nERROR: $texto" -ForegroundColor Red
    exit 1
}

Write-Host "Instalando Susurro - dictado por voz local con Whisper" -ForegroundColor Magenta

# 1. Buscar Python 3.10 a 3.13 (se recomienda 3.12)
Paso "Buscando Python..."
$python = $null
$candidatos = @(
    @{ exe = "py"; args = @("-3.12") },
    @{ exe = "py"; args = @("-3.11") },
    @{ exe = "py"; args = @("-3.13") },
    @{ exe = "py"; args = @("-3.10") },
    @{ exe = "python"; args = @() }
)
foreach ($c in $candidatos) {
    if (-not (Get-Command $c.exe -ErrorAction SilentlyContinue)) { continue }
    try {
        $v = & $c.exe @($c.args + @("-c", "import sys; print('%d.%d' % sys.version_info[:2])")) 2>$null
        if ($LASTEXITCODE -eq 0 -and $v -match '^3\.1[0-3]$') { $python = $c; break }
    } catch { }
}
if (-not $python) {
    Falla ("No encontre Python 3.10-3.13. Instala Python 3.12 desde https://www.python.org/downloads/ " +
           "(marca 'Add python.exe to PATH') y vuelve a ejecutar este instalador.")
}
Write-Host "Usando Python $v"

# 2. Entorno virtual (todo queda dentro de esta carpeta)
Paso "Creando el entorno de Python (.venv)..."
if (-not (Test-Path ".venv\Scripts\python.exe")) {
    & $python.exe @($python.args + @("-m", "venv", ".venv"))
    if ($LASTEXITCODE -ne 0) { Falla "No se pudo crear el entorno virtual." }
}
$venvPy = ".venv\Scripts\python.exe"

# 3. Dependencias
Paso "Instalando dependencias (puede tardar unos minutos)..."
& $venvPy -m pip install --upgrade pip --quiet
& $venvPy -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { Falla "No se pudieron instalar las dependencias." }

# 4. Aceleración por GPU (solo con tarjeta NVIDIA)
if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
    Paso "Tarjeta NVIDIA detectada: instalando librerias de CUDA (~1 GB)..."
    & $venvPy -m pip install -r requirements-gpu.txt
    if ($LASTEXITCODE -ne 0) { Falla "No se pudieron instalar las librerias de CUDA." }
} else {
    Write-Host "`nAVISO: no se detecto tarjeta NVIDIA. Susurro funcionara con el procesador," -ForegroundColor Yellow
    Write-Host "pero cada dictado tardara mas o menos lo mismo que lo que hablaste." -ForegroundColor Yellow
}

# 5. Modelo de Whisper (~1.6 GB, se descarga una sola vez)
Paso "Descargando el modelo de voz Whisper large-v3-turbo (~1.6 GB, solo la primera vez)..."
& $venvPy susurro.py --descargar-modelo
if ($LASTEXITCODE -ne 0) { Falla "No se pudo descargar el modelo. Revisa tu conexion a internet." }

# 6. Accesos directos (escritorio e inicio de Windows)
Paso "Creando accesos directos..."
& $venvPy susurro.py --crear-accesos
if ($LASTEXITCODE -ne 0) { Falla "No se pudieron crear los accesos directos." }

# 7. Abrir Susurro
Paso "Listo. Abriendo Susurro..."
Start-Process ".venv\Scripts\pythonw.exe" -ArgumentList "`"$PSScriptRoot\susurro.py`"" -WorkingDirectory $PSScriptRoot
Write-Host "`nSusurro esta en la bandeja del sistema (icono del microfono junto al reloj)." -ForegroundColor Green
Write-Host "Tarda unos 20 segundos en quedar listo. Toca Ctrl derecho para dictar." -ForegroundColor Green
