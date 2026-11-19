$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$Repo = if ($env:MISAKA_REPO) { $env:MISAKA_REPO } else { "Luciole-Studio/Misaka-Agent" }
$Prefix = if ($env:MISAKA_PREFIX) { $env:MISAKA_PREFIX } else { Join-Path $env:LOCALAPPDATA "misaka" }
$Version = $env:MISAKA_VERSION
if ($args.Count -ge 1 -and $args[0]) { $Version = $args[0] }
if ($Version) { $Version = $Version.TrimStart("v") }

switch ([System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture) {
    "X64" { $Target = "windows-x86_64" }
    "Arm64" { $Target = "windows-arm64" }
    default {
        throw "No MISAKA build for $([System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture). Published Windows architectures: windows-x86_64, windows-arm64."
    }
}

if (-not $Version) {
    $latest = Invoke-RestMethod -Uri "https://api.github.com/repos/$Repo/releases/latest" -Headers @{ "User-Agent" = "misaka-release" }
    $Version = [string]$latest.tag_name
    if ($Version.StartsWith("v")) { $Version = $Version.Substring(1) }
}
if (-not $Version) { throw "Could not tell which release to install. Set MISAKA_VERSION." }

$Name = "misaka-$Version-$Target.zip"
$Url = "https://github.com/$Repo/releases/download/v$Version/$Name"
$SumsUrl = "https://github.com/$Repo/releases/download/v$Version/SHA256SUMS"
$Work = Join-Path ([System.IO.Path]::GetTempPath()) ("misaka-install-" + [guid]::NewGuid().ToString("n"))
New-Item -ItemType Directory -Path $Work | Out-Null
try {
    $Archive = Join-Path $Work $Name
    $Sums = Join-Path $Work "SHA256SUMS"
    Write-Host "Downloading $Name"
    Invoke-WebRequest -Uri $Url -OutFile $Archive
    Invoke-WebRequest -Uri $SumsUrl -OutFile $Sums
    $Expected = $null
    foreach ($line in Get-Content -Path $Sums) {
        $parts = $line -split "\s+", 2
        if ($parts.Length -eq 2 -and $parts[1] -eq $Name) { $Expected = $parts[0].ToLower() }
    }
    if (-not $Expected) { throw "SHA256SUMS has no entry for $Name." }
    $Actual = (Get-FileHash -Path $Archive -Algorithm SHA256).Hash.ToLower()
    if ($Expected -ne $Actual) { throw "Checksum mismatch for $Name.`nexpected $Expected`nactual   $Actual" }

    New-Item -ItemType Directory -Force -Path $Prefix | Out-Null
    $Dest = Join-Path $Prefix "misaka-$Version-$Target"
    if (Test-Path $Dest) { Remove-Item -Recurse -Force $Dest }
    Expand-Archive -Path $Archive -DestinationPath $Prefix -Force
    $Bin = Join-Path $Dest "bin"
    $UserPath = [Environment]::GetEnvironmentVariable("Path", "User")
    if (-not $UserPath) { $UserPath = "" }
    $Earlier = Join-Path $Prefix "misaka-*\bin"
    $pieces = $UserPath -split ";" | Where-Object {
        $_ -and ($_.TrimEnd("\") -ne $Bin.TrimEnd("\")) -and -not ($_.TrimEnd("\") -like $Earlier)
    }
    $Updated = (@($Bin) + @($pieces)) -join ";"
    [Environment]::SetEnvironmentVariable("Path", $Updated, "User")
    $Others = Get-Command misaka -All -ErrorAction SilentlyContinue | Where-Object {
        $_.Source -and -not $_.Source.StartsWith($Prefix, [System.StringComparison]::OrdinalIgnoreCase)
    }
    foreach ($Other in $Others) {
        Write-Warning "Another MISAKA is on PATH: $($Other.Source). New terminals run this one first; remove the other (uv tool uninstall misaka) if you no longer want it."
    }
    Write-Host "Installed $Dest"
    Write-Host "Command: $(Join-Path $Bin 'misaka.exe')"
    Write-Host "Open a new terminal, then: mkdir `$HOME\Documents\my-research; cd `$HOME\Documents\my-research; misaka setup"
}
finally {
    if (Test-Path $Work) { Remove-Item -Recurse -Force $Work }
}
