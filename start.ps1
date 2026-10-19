<#
.SYNOPSIS
Starts PhotoMe with your photo folders mounted read-only inside the container.

.DESCRIPTION
Each folder gets a mount name; the database stores it in every file path ("/archive/old/a.jpg"),
so a folder must keep the same name for as long as its records exist.

  .\start.ps1 "photos=C:\Pictures" "archive=D:\archive"   each folder gets the name you give
  .\start.ps1 "C:\Pictures" "D:\archive"                  no names: /photos, /photos-2, ... in order

Every mount is read-only. To let PhotoMe's Sync panel copy files INTO a mount:

  -SyncInto NAME   (recommended) the mount itself stays read-only; only the folders
                   <mount>\_from-<other mount> become writable. PhotoMe can add files there and
                   nowhere else in that mount:
  .\start.ps1 -SyncInto archive "photos=C:\Pictures" "archive=D:\archive"

  -Writable NAME   the whole mount becomes writable (use only if -SyncInto does not work).

PhotoMe never overwrites or deletes anything in either case.

-RenameMount OLD=NEW renames a mount point that already has records: the database paths /OLD/...
become /NEW/... (a backup of the database is made first), then NEW is mounted as usual. Give the
folder its new name too:
  .\start.ps1 -RenameMount photos-2=archive "archive=D:\archive" "photos=C:\Pictures"

The script remembers the name -> folder pairs in .photome-mounts and stops if a name would point
at a different folder than last time. Use -Remap if you really moved a folder.
A name is letters, digits, '.', '_' or '-'; a leading '/' is allowed ("/archive=D:\archive").
#>
# Only the folder list is positional; every option must be named.
[CmdletBinding(PositionalBinding = $false)]
param(
    [switch]$Remap,
    [string[]]$SyncInto = @(),
    [string[]]$Writable = @(),
    [string]$RenameMount = '',
    [Parameter(Position = 0, ValueFromRemainingArguments = $true)]
    [string[]]$PhotoDirectory
)

$ErrorActionPreference = 'Stop'

function Stop-With([string]$message) {
    Write-Host $message -ForegroundColor Red
    exit 1
}

# Runs docker and returns its exit code. It deliberately avoids PowerShell's own handling of the
# command's output: docker writes its progress ("Container ... Stopping") to stderr, and Windows
# PowerShell 5.1 turns every stderr line of a redirected native command into an error, which
# $ErrorActionPreference = 'Stop' then makes fatal. Start-Process hands docker the console directly.
function Invoke-Docker([string[]]$DockerArguments, [switch]$Quiet) {
    $options = @{ FilePath = 'docker'; ArgumentList = $DockerArguments; NoNewWindow = $true; Wait = $true; PassThru = $true }
    if ($Quiet) {
        $quietOut = [System.IO.Path]::GetTempFileName()
        $quietErr = [System.IO.Path]::GetTempFileName()
        $options['RedirectStandardOutput'] = $quietOut
        $options['RedirectStandardError'] = $quietErr
    }
    try {
        $process = Start-Process @options
        return [int]$process.ExitCode
    } finally {
        if ($Quiet) { Remove-Item -LiteralPath $quietOut, $quietErr -Force -ErrorAction SilentlyContinue }
    }
}

function Split-Names($list) {
    @($list -split ',' | ForEach-Object { $_.Trim().TrimStart('/') } | Where-Object { $_ })
}

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Stop-With 'Docker is required. Install Docker Desktop and try again.'
}

if ((Invoke-Docker @('compose', 'version') -Quiet) -ne 0) {
    Stop-With 'Docker Compose is required. Install the Docker Compose plugin and try again.'
}

# Names the container already uses for itself; a photo mount there would break the app.
$reserved = @('bin', 'boot', 'data', 'dev', 'etc', 'home', 'lib', 'lib32', 'lib64', 'libx32', 'media',
              'mnt', 'opt', 'proc', 'root', 'run', 'sbin', 'srv', 'sys', 'tmp', 'usr', 'var', 'workspace')

$names = @()
$dirs = @()
$plain = @()
foreach ($arg in $PhotoDirectory) {
    # "name=folder". A Windows path such as D:\x never matches: its drive colon is not allowed in a name.
    if ($arg -cmatch '^/?([A-Za-z0-9][A-Za-z0-9._-]{0,39})=(.+)$') {
        $name = $Matches[1]
        $dir = $Matches[2]
        if ($reserved -ccontains $name) { Stop-With "The mount name '$name' is reserved by the container. Choose another." }
        if ($names -ccontains $name) { Stop-With "The mount name '$name' is used twice." }
        $names += $name
        $dirs += $dir
    } else {
        $plain += $arg
    }
}

# Folders given without a name take the first unused default name: photos, photos-2, photos-3, ...
$next = 1
foreach ($dir in $plain) {
    do {
        $candidate = if ($next -eq 1) { 'photos' } else { "photos-$next" }
        $next++
    } while ($names -ccontains $candidate)
    $names += $candidate
    $dirs += $dir
}

if ($names.Count -eq 0) {
    if ($env:PHOTO_LIBRARY) {
        $names = @('photos')
        $dirs = @($env:PHOTO_LIBRARY)
    } else {
        $default = Join-Path $PSScriptRoot 'photos'
        New-Item -ItemType Directory -Force -Path $default | Out-Null
        $names = @('photos')
        $dirs = @($default)
    }
}

$resolved = @()
foreach ($d in $dirs) {
    if (-not (Test-Path -LiteralPath $d -PathType Container)) {
        Stop-With "Photo directory does not exist: $d"
    }
    $resolved += (Resolve-Path -LiteralPath $d).Path
}

function Get-Folder([string]$mount) {
    for ($i = 0; $i -lt $names.Count; $i++) { if ($names[$i] -ceq $mount) { return $resolved[$i] } }
    return $null
}

$writableNames = Split-Names $Writable
$syncIntoNames = Split-Names $SyncInto
foreach ($item in @($writableNames) + @($syncIntoNames)) {
    if (-not ($names -ccontains $item)) {
        Stop-With "'${item}' is not one of the mounts (the mounts are: $($names -join ', '))."
    }
}
foreach ($item in $syncIntoNames) {
    if ($writableNames -ccontains $item) { Stop-With "'${item}' is named in both -SyncInto and -Writable; choose one." }
}

$renameOld = ''
$renameNew = ''
if ($RenameMount) {
    if ($RenameMount -notmatch '=') { Stop-With '-RenameMount needs OLD=NEW, for example -RenameMount photos-2=archive.' }
    $renameOld = $RenameMount.Substring(0, $RenameMount.IndexOf('=')).TrimStart('/')
    $renameNew = $RenameMount.Substring($RenameMount.IndexOf('=') + 1).TrimStart('/')
    if ($renameOld -cnotmatch '^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$') { Stop-With "-RenameMount: '$renameOld' is not a valid mount name." }
    if ($renameOld -ceq $renameNew) { Stop-With '-RenameMount: the old and the new name are the same.' }
    if (-not ($names -ccontains $renameNew)) {
        Stop-With "-RenameMount: '$renameNew' must be one of the mounts you pass (name the folder $renameNew=...), so the renamed mount is mounted under its new name."
    }
    if ($names -ccontains $renameOld) {
        Stop-With "-RenameMount: '$renameOld' is still passed as a mount; remove it, because it is being renamed to '$renameNew'."
    }
}

# ---- keep names and folders consistent between runs ------------------------------------
$state = Join-Path $PSScriptRoot '.photome-mounts'
$previous = New-Object System.Collections.Specialized.OrderedDictionary
if (Test-Path -LiteralPath $state) {
    foreach ($line in [System.IO.File]::ReadAllLines($state)) {
        $at = $line.IndexOf('=')
        if ($at -gt 0) { $previous[$line.Substring(0, $at)] = $line.Substring($at + 1) }
    }
}

$conflicts = @()
for ($i = 0; $i -lt $names.Count; $i++) {
    $before = $previous[[string]$names[$i]]
    if ($before -and ($before -ine $resolved[$i])) {
        $conflicts += "  /$($names[$i]) was $before and would now be $($resolved[$i])"
    }
}
if ($renameOld) {
    $before = $previous[[string]$renameOld]
    if ($before -and ($before -ine (Get-Folder $renameNew))) {
        $conflicts += "  /$renameOld was $before and /$renameNew would be $(Get-Folder $renameNew)"
    }
}
if ($conflicts.Count -gt 0 -and -not $Remap) {
    Write-Host 'A mount name would point at a different folder than last time:' -ForegroundColor Red
    $conflicts | ForEach-Object { Write-Host $_ -ForegroundColor Red }
    Write-Host ''
    Write-Host 'The records already in the database under that name belong to the old folder, so this' -ForegroundColor Red
    Write-Host 'would make their paths wrong. Give the folders the names they had before, or, if you' -ForegroundColor Red
    Write-Host 'really moved the folder, run again with -Remap.' -ForegroundColor Red
    exit 1
}

# ---- the environment the app (and docker compose) needs ------------------------------------
# /photos always comes from PHOTO_LIBRARY (docker-compose.yml); the others are added below.
$library = $null
$hostPaths = @()
for ($i = 0; $i -lt $names.Count; $i++) {
    if ($names[$i] -ceq 'photos') { $library = $resolved[$i] }
    $hostPaths += "/$($names[$i])=$($resolved[$i])"
}
$placeholder = $false
if (-not $library) {
    $library = Join-Path $PSScriptRoot 'photos'
    New-Item -ItemType Directory -Force -Path $library | Out-Null
    $placeholder = $true
}
$env:PHOTO_LIBRARY = $library
# Tell the app which folder on this PC is behind each mount, for the copy scripts it can write.
$env:PHOTOME_HOST_PATHS = $hostPaths -join '|'
Set-Location $PSScriptRoot

# ---- rename a mount point in the database (before anything is started) -------------------------
if ($renameOld) {
    Write-Host "Renaming mount /$renameOld to /$renameNew in the database (a backup is made first)..."
    $null = Invoke-Docker @('compose', 'stop')   # nothing may be writing to the database meanwhile
    $renameExit = Invoke-Docker @('compose', 'run', '--rm', '--no-deps', '-T', 'photome', 'python', 'app.py', '--rename-mount', $renameOld, $renameNew)
    if ($renameExit -ne 0) {
        Stop-With "The rename failed (docker exit code $renameExit), so nothing was changed and the app was not started. See the message above."
    }
}

# ---- remember the current pairs, and keep the old ones that are not mounted this time ----------
$remember = @()
for ($i = 0; $i -lt $names.Count; $i++) { $remember += "$($names[$i])=$($resolved[$i])" }
$unmounted = @()
foreach ($key in @($previous.Keys)) {
    if (($key -cne $renameOld) -and -not ($names -ccontains $key)) {
        $remember += "$key=$($previous[$key])"
        $unmounted += "  /$key  ($($previous[$key]))"
    }
}
[System.IO.File]::WriteAllLines($state, [string[]]$remember)

if ($placeholder) { Write-Host "Note: no mount is named 'photos', so /photos is an empty folder." }
Write-Host 'Mount points (the name is what the database stores):'
for ($i = 0; $i -lt $names.Count; $i++) {
    $mode = ''
    if ($writableNames -ccontains $names[$i]) { $mode = '   [READ-WRITE]' }
    if ($syncIntoNames -ccontains $names[$i]) { $mode = '   [read-only; add-only _from-* folders]' }
    Write-Host ('  /{0,-12} <- {1}{2}' -f $names[$i], $resolved[$i], $mode)
}
foreach ($item in $writableNames) {
    Write-Host "WRITABLE: /$item. PhotoMe may add files anywhere in it, but only inside _from-<source> folders; it never overwrites or deletes." -ForegroundColor Yellow
}
foreach ($item in $syncIntoNames) {
    Write-Host "SYNC INTO: /$item stays read-only; only its _from-<source> folders are writable. PhotoMe never overwrites or deletes." -ForegroundColor Yellow
}
if ($unmounted.Count -gt 0) {
    Write-Host 'Mounted last time but not now (their records stay in the database):'
    $unmounted | ForEach-Object { Write-Host $_ }
}

# ---- the compose override: extra mounts, and the writable folders --------------------------------
$extra = Join-Path $PSScriptRoot '.photome-extra.yml'
$composeArgs = @()
$lines = @('services:', '  photome:', '    volumes:')
$extraMounts = 0
for ($i = 0; $i -lt $names.Count; $i++) {
    $isWritable = $writableNames -ccontains $names[$i]
    # /photos comes from docker-compose.yml (read-only) unless it was made writable here
    if (($names[$i] -ceq 'photos') -and -not $isWritable) { continue }
    $src = ($resolved[$i] -replace '\\', '/') -replace "'", "''"
    $lines += '      - type: bind'
    $lines += "        source: '$src'"
    $lines += "        target: /$($names[$i])"
    $lines += "        read_only: $(if ($isWritable) { 'false' } else { 'true' })"
    $extraMounts++
}
# The folders that receive copies must exist on this PC before they can be mounted, and they are
# the only part of a -SyncInto mount that is writable.
foreach ($target in $syncIntoNames) {
    foreach ($other in $names) {
        if ($other -ceq $target) { continue }
        $folder = Join-Path (Get-Folder $target) "_from-$other"
        New-Item -ItemType Directory -Force -Path $folder | Out-Null
        $src = ($folder -replace '\\', '/') -replace "'", "''"
        $lines += '      - type: bind'
        $lines += "        source: '$src'"
        $lines += "        target: /$target/_from-$other"
        $lines += '        read_only: false'
        $extraMounts++
    }
}
if ($extraMounts -gt 0) {
    [System.IO.File]::WriteAllLines($extra, [string[]]$lines)
    $composeArgs = @('-f', 'docker-compose.yml', '-f', '.photome-extra.yml')
} elseif (Test-Path -LiteralPath $extra) {
    Remove-Item -LiteralPath $extra -Force
}

docker compose @composeArgs up --build
exit $LASTEXITCODE
