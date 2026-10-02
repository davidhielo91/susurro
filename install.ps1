# Instalacion rapida de Susurro (tambien sirve para actualizar).
# Uso, en PowerShell:
#     irm https://susurro.hieloco.de/install.ps1 | iex
#
# Que hace:
#   1. Instala Python 3.12 con winget si no lo tienes.
#   2. Descarga la ultima version de Susurro desde GitHub a %LOCALAPPDATA%\Susurro
#      (o a la carpeta en la variable SUSURRO_DIR, si existe).
#   3. Ejecuta instalar.ps1: dependencias, modelo de Whisper y accesos directos.
# Al actualizar conserva tu configuracion (config.json) y tu historial (historial.json).

& {
    $ErrorActionPreference = "Stop"
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $ProgressPreference = "SilentlyContinue"   # descargas mucho mas rapidas en PowerShell 5

    $repoZip = "https://github.com/davidhielo91/susurro/archive/refs/heads/main.zip"
    $dest = if ($env:SUSURRO_DIR) { $env:SUSURRO_DIR } else { Join-Path $env:LOCALAPPDATA "Susurro" }

    function Paso($texto) { Write-Host "`n==> $texto" -ForegroundColor Cyan }

    function Find-Python {
        foreach ($c in @(@("py", "-3.12"), @("py", "-3.11"), @("py", "-3.13"), @("py", "-3.10"), @("python"))) {
            if (-not (Get-Command $c[0] -ErrorAction SilentlyContinue)) { continue }
            try {
                $extra = @($c | Select-Object -Skip 1)
                $v = & $c[0] @($extra + @("-c", "import sys; print('%d.%d' % sys.version_info[:2])")) 2>$null
                if ($LASTEXITCODE -eq 0 -and $v -match '^3\.1[0-3]$') { return $v }
            } catch { }
        }
        return $null
    }

    Write-Host "Susurro - dictado por voz local con Whisper" -ForegroundColor Magenta
    Write-Host "Se instalara en: $dest"

    # 1. Python
    Paso "Revisando Python..."
    if (-not (Find-Python)) {
        if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
            Write-Host "No hay Python ni winget. Instala Python 3.12 desde https://www.python.org/downloads/" -ForegroundColor Red
            Write-Host "(marca 'Add python.exe to PATH') y vuelve a ejecutar este comando." -ForegroundColor Red
            return
        }
        Write-Host "Instalando Python 3.12 con winget..."
        winget install --exact --id Python.Python.3.12 --scope user --silent `
            --accept-package-agreements --accept-source-agreements
        # Recargar el PATH para ver el Python recien instalado sin abrir otra ventana
        $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                    [Environment]::GetEnvironmentVariable("Path", "User")
        if (-not (Find-Python)) {
            Write-Host "Python se instalo, pero esta ventana aun no lo ve. Cierra PowerShell, abre otra y vuelve a ejecutar el comando." -ForegroundColor Yellow
            return
        }
    }
    Write-Host "Python $(Find-Python) listo."

    # 2. Cerrar Susurro si ya estaba abierto desde esta carpeta (para actualizar)
    $script = Join-Path $dest "susurro.py"
    Get-CimInstance Win32_Process -Filter "name like 'python%'" |
        Where-Object { $_.CommandLine -and $_.CommandLine -like "*$script*" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

    # 3. Descargar la ultima version
    Paso "Descargando Susurro desde GitHub..."
    $tmp = Join-Path $env:TEMP ("susurro-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Force $tmp | Out-Null
    try {
        Invoke-WebRequest $repoZip -OutFile "$tmp\susurro.zip" -UseBasicParsing
        Expand-Archive "$tmp\susurro.zip" -DestinationPath $tmp -Force
        $src = Get-ChildItem $tmp -Directory | Select-Object -First 1
        New-Item -ItemType Directory -Force $dest | Out-Null
        # Copia los archivos del programa; config.json, historial.json y .venv no vienen en el ZIP
        Copy-Item (Join-Path $src.FullName "*") $dest -Recurse -Force
    } finally {
        Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
    }

    # 4. Instalar (en otro proceso, para no cerrar esta ventana si algo falla)
    powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $dest "instalar.ps1")
}
