param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$PhotoDirectory
)

$ErrorActionPreference = 'Stop'

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Error 'Docker is required. Install Docker Desktop and try again.'
}

docker compose version *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Error 'Docker Compose is required. Install the Docker Compose plugin and try again.'
}

if ($PhotoDirectory) {
    $dirs = @($PhotoDirectory)
} elseif ($env:PHOTO_LIBRARY) {
    $dirs = @($env:PHOTO_LIBRARY)
} else {
    $default = Join-Path $PSScriptRoot 'photos'
    New-Item -ItemType Directory -Force -Path $default | Out-Null
    $dirs = @($default)
}

$resolved = foreach ($d in $dirs) {
    if (-not (Test-Path -LiteralPath $d -PathType Container)) {
        Write-Error "Photo directory does not exist: $d"
    }
    (Resolve-Path -LiteralPath $d).Path
}
$resolved = @($resolved)

# The first directory is /photos; extra ones are /photos-2, /photos-3, ...
$env:PHOTO_LIBRARY = $resolved[0]

Set-Location $PSScriptRoot
$extra = Join-Path $PSScriptRoot '.photome-extra.yml'
$composeArgs = @()
if ($resolved.Count -gt 1) {
    $lines = @('services:', '  photome:', '    volumes:')
    for ($i = 1; $i -lt $resolved.Count; $i++) {
        $src = $resolved[$i] -replace '\\', '/'
        $lines += "      - type: bind"
        $lines += "        source: '$src'"
        $lines += "        target: /photos-$($i + 1)"
        $lines += "        read_only: true"
    }
    Set-Content -LiteralPath $extra -Value $lines -Encoding ascii
    $composeArgs = @('-f', 'docker-compose.yml', '-f', '.photome-extra.yml')
} elseif (Test-Path -LiteralPath $extra) {
    Remove-Item -LiteralPath $extra
}

docker compose @composeArgs up --build
