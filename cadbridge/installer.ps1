# CadBridge - interactive installer / uninstaller (Windows PowerShell 5.1+, per Windows user, no admin rights).
# Normally started by double-clicking Install.cmd or Uninstall.cmd. Works for AutoCAD 2021-2026 (and AutoCAD-based
# verticals) and CAD&PILLAR 11: R24 (2021-2024) loads bin/net48, R25+ (2025, 2026, CAD&PILLAR 11) loads bin/net8.
# Everything missing is downloaded: uv, Python and the Python libraries.
#   -Uninstall            remove everything this installer added
#   -Cads "all"           skip the CAD question     (all supported CADs found)
#   -Clients "a,b"        skip the AI-app question  (claude-code, claude-desktop, codex, cursor, windsurf, vscode, antigravity, gemini-cli, all)
#   -Yes                  no questions: assume yes, do not wait for apps to be closed (CAD must be closed)
param(
    [switch]$Uninstall,
    [string]$Cads = "",
    [string]$Clients = "",
    [switch]$Yes
)
$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Name = 'cadbridge'
$Legacy = @('cadpillar')                      # earlier names of this server, removed when found
$DllDir = Join-Path $env:APPDATA 'CadBridge'
$PyDir = Join-Path $env:LOCALAPPDATA 'CadBridge'
$Py = Join-Path $PyDir 'venv\Scripts\python.exe'
$Server = Join-Path $PyDir 'cadbridge_mcp.py'
$Stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$Version = (Get-Content (Join-Path $Here 'VERSION') -Raw).Trim()

function Say($t, $c = 'Gray') { Write-Host $t -ForegroundColor $c }
function Step($n, $t) { Say ''; Say "  Step $n/5  $t" Cyan; Say ('  ' + ('-' * (10 + $t.Length))) DarkCyan }
function Ask($q) {
    if ($Yes) { return $true }
    $a = Read-Host "  $q [Y/n]"
    return ($a -eq '' -or $a -match '^[Yy]')
}
function Pick($items, $label, $preset) {
    # Multiple choice by number; returns the chosen items. $preset: '' (ask), 'all' or a comma list of keys.
    if ($preset) {
        if ($preset -eq 'all') { return $items | Where-Object { $_.Ok } }
        $keys = $preset -split '[,\s]+' | Where-Object { $_ }
        $sel = @($items | Where-Object { $keys -contains $_.Key })
        foreach ($k in $keys) { if (-not ($items | Where-Object { $_.Key -eq $k })) { throw "unknown choice: $k" } }
        return $sel
    }
    for ($i = 0; $i -lt $items.Count; $i++) {
        Say ("   {0}) {1,-40} {2}" -f ($i + 1), $items[$i].Label, $items[$i].Note)
    }
    $default = (@(for ($i = 0; $i -lt $items.Count; $i++) { if ($items[$i].Ok -and $items[$i].Found) { $i + 1 } }) -join ',')
    $ans = Read-Host "  $label - numbers separated by commas, A = all  [$default]"
    if (-not $ans) { $ans = $default }
    if ($ans -match '^[Aa]') { return $items | Where-Object { $_.Ok } }
    return @($ans -split '[,\s]+' | Where-Object { $_ } | ForEach-Object { $items[[int]$_ - 1] })
}
function Write-Utf8($path, $text) {
    New-Item -ItemType Directory -Force (Split-Path -Parent $path) | Out-Null
    [IO.File]::WriteAllText($path, $text, (New-Object Text.UTF8Encoding $false))
}
function Backup($path) { if (Test-Path $path) { Copy-Item $path "$path.bak-$Name-$Stamp" } }

# ================================================================ CADs
function Find-Cads {
    $roots = @(
        @{ Root = 'HKLM:\SOFTWARE\Autodesk\AutoCAD'; Rx = '^ACAD-[0-9A-Fa-f]+:[0-9A-Fa-f]+$' },
        @{ Root = 'HKLM:\SOFTWARE\Digicorp Ingegneria\CADPILLAR'; Rx = '^cadp-\d+:\d+$' }
    )
    foreach ($r in $roots) {
        if (-not (Test-Path $r.Root)) { continue }
        foreach ($rel in Get-ChildItem $r.Root -ErrorAction SilentlyContinue) {
            foreach ($prod in Get-ChildItem $rel.PSPath -ErrorAction SilentlyContinue | Where-Object { $_.PSChildName -match $r.Rx }) {
                $p = Get-ItemProperty $prod.PSPath
                $loc = $p.AcadLocation; if (-not $loc) { $loc = $p.Location }
                if (-not $loc -or -not (Test-Path $loc)) { continue }
                $exe = @('acad', 'cadp') | Where-Object { Test-Path (Join-Path $loc "$_.exe") } | Select-Object -First 1
                $acmgd = Join-Path $loc 'acmgd.dll'          # absent in AutoCAD LT (no .NET API): skipped
                if (-not $exe -or -not (Test-Path $acmgd)) { continue }
                # The platform generation comes from the .NET API version, not the registry release:
                # OEMs number their own releases (CAD&PILLAR 11 = R11.0 on the AutoCAD 25.x platform).
                $major = [Reflection.AssemblyName]::GetAssemblyName($acmgd).Version.Major
                $label = $p.ProductName; if (-not $label) { $label = "$exe $($rel.PSChildName)" }
                $userKey = 'HKCU:\' + ($prod.Name -replace '^HKEY_LOCAL_MACHINE\\', '')
                $ok = $major -ge 24
                $tfm = $(if ($major -ge 25) { 'net8' } else { 'net48' })
                [pscustomobject]@{
                    Key = $exe; Label = "$label (platform $major)"; Exe = $exe; UserKey = $userKey; Ok = $ok; Found = $true; Tfm = $tfm
                    Note = $(if (-not $ok) { 'not supported (needs AutoCAD 2021 or newer)' } elseif (-not (Test-Path $userKey)) { 'start it once first' } else { '' })
                }
            }
        }
    }
}

# ================================================================ AI apps
$ClaudeDesktopFiles = @(Join-Path $env:APPDATA 'Claude\claude_desktop_config.json')
Get-ChildItem (Join-Path $env:LOCALAPPDATA 'Packages') -Directory -Filter 'Claude_*' -ErrorAction SilentlyContinue |
    ForEach-Object { $ClaudeDesktopFiles += Join-Path $_.FullName 'LocalCache\Roaming\Claude\claude_desktop_config.json' }

$GeminiDir = Join-Path $env:USERPROFILE '.gemini'   # shared by Google Antigravity and the Gemini CLI
# Procs: "ProcessName|path regex". Claude Desktop and the Claude Code CLI are both claude.exe: told apart by path.
$AppList = @(
    [pscustomobject]@{ Key = 'claude-code'; Label = 'Claude Code (CLI / IDE)'; Found = [bool](Get-Command claude -ErrorAction SilentlyContinue); Procs = @() }
    [pscustomobject]@{ Key = 'claude-desktop'; Label = 'Claude Desktop'; Found = (Test-Path (Join-Path $env:APPDATA 'Claude')) -or ($ClaudeDesktopFiles.Count -gt 1); Procs = @('Claude|WindowsApps\\Claude_|AnthropicClaude') }
    [pscustomobject]@{ Key = 'codex'; Label = 'OpenAI Codex (CLI / IDE)'; Found = Test-Path (Join-Path $env:USERPROFILE '.codex'); Procs = @('codex|') }
    [pscustomobject]@{ Key = 'cursor'; Label = 'Cursor'; Found = Test-Path (Join-Path $env:USERPROFILE '.cursor'); Procs = @('Cursor|') }
    [pscustomobject]@{ Key = 'windsurf'; Label = 'Windsurf'; Found = Test-Path (Join-Path $env:USERPROFILE '.codeium\windsurf'); Procs = @('Windsurf|') }
    [pscustomobject]@{ Key = 'vscode'; Label = 'VS Code (GitHub Copilot agent)'; Found = Test-Path (Join-Path $env:APPDATA 'Code\User'); Procs = @('Code|\\Microsoft VS Code\\') }
    [pscustomobject]@{ Key = 'antigravity'; Label = 'Google Antigravity (IDE / agy CLI)'; Found = (Test-Path (Join-Path $GeminiDir 'config')) -or (Test-Path (Join-Path $GeminiDir 'antigravity')) -or [bool](Get-Command agy -ErrorAction SilentlyContinue); Procs = @('Antigravity|', 'agy|') }
    [pscustomobject]@{ Key = 'gemini-cli'; Label = 'Gemini CLI'; Found = [bool](Get-Command gemini -ErrorAction SilentlyContinue) -or (Test-Path (Join-Path $GeminiDir 'settings.json')); Procs = @() }
)
foreach ($a in $AppList) { $a | Add-Member Ok $true; $a | Add-Member Note $(if ($a.Found) { '(found)' } else { '' }) }

function Set-JsonServer($path, $root, $server, $entry) {
    # Sets $root.$server to $entry ($null = remove) and drops legacy names. Never rewrites a file it cannot parse.
    $obj = New-Object PSObject
    if (Test-Path $path) {
        $raw = [IO.File]::ReadAllText($path)
        if ($raw.Trim()) {
            try { $obj = $raw | ConvertFrom-Json } catch {
                Say "    ! $path is not plain JSON (comments?); left untouched." Yellow
                if ($entry) { Say ("    Add by hand under `"$root`":  `"$server`": " + ($entry | ConvertTo-Json -Depth 10 -Compress)) Yellow }
                return
            }
        }
    } elseif (-not $entry) { return }
    if (-not ($obj.PSObject.Properties.Name -contains $root)) {
        if (-not $entry) { return }
        $obj | Add-Member -NotePropertyName $root -NotePropertyValue (New-Object PSObject)
    }
    $servers = $obj.$root
    foreach ($n in @($server) + $Legacy) { if ($servers.PSObject.Properties.Name -contains $n) { $servers.PSObject.Properties.Remove($n) } }
    if ($entry) { $servers | Add-Member -NotePropertyName $server -NotePropertyValue $entry }
    Backup $path
    Write-Utf8 $path ($obj | ConvertTo-Json -Depth 50)
    Say "    - $path" DarkGray
}

function Set-CodexToml($add) {
    $path = Join-Path $env:USERPROFILE '.codex\config.toml'
    $lines = @(); if (Test-Path $path) { $lines = @(Get-Content $path) } elseif (-not $add) { return }
    $names = (@($Name) + $Legacy) -join '|'
    $out = New-Object System.Collections.Generic.List[string]; $skip = $false
    foreach ($l in $lines) {
        if ($l -match '^\s*\[') { $skip = $l -match "^\s*\[mcp_servers\.($names)(\.|\])" }
        if (-not $skip) { $out.Add($l) }
    }
    while ($out.Count -and -not $out[$out.Count - 1].Trim()) { $out.RemoveAt($out.Count - 1) }
    if ($add) {
        $out.Add(''); $out.Add("[mcp_servers.$Name]"); $out.Add("command = '$Py'"); $out.Add("args = ['$Server']")
        $out.Add('startup_timeout_sec = 60')
    }
    Backup $path
    Write-Utf8 $path (($out -join "`r`n") + "`r`n")
    Say "    - $path" DarkGray
}

function Configure($key, $add) {
    $stdio = $null; if ($add) { $stdio = [ordered]@{ command = $Py; args = @($Server) } }
    switch ($key) {
        'claude-code' {
            if (-not (Get-Command claude -ErrorAction SilentlyContinue)) { Say '    ! claude CLI not found; skipped' Yellow; return }
            foreach ($n in @($Name) + $Legacy) { cmd /c "claude mcp remove $n -s user >nul 2>&1" }
            if ($add) { claude mcp add $Name -s user -- $Py $Server | Out-Null }
            Say '    - claude mcp, user scope' DarkGray
        }
        'claude-desktop' {
            foreach ($f in $ClaudeDesktopFiles) {
                if ((Test-Path (Split-Path -Parent $f)) -or $f -eq $ClaudeDesktopFiles[0]) { Set-JsonServer $f 'mcpServers' $Name $stdio }
            }
        }
        'codex' { Set-CodexToml $add }
        'cursor' { Set-JsonServer (Join-Path $env:USERPROFILE '.cursor\mcp.json') 'mcpServers' $Name $stdio }
        'windsurf' { Set-JsonServer (Join-Path $env:USERPROFILE '.codeium\windsurf\mcp_config.json') 'mcpServers' $Name $stdio }
        'vscode' {
            $e = $null; if ($add) { $e = [ordered]@{ type = 'stdio'; command = $Py; args = @($Server) } }
            Set-JsonServer (Join-Path $env:APPDATA 'Code\User\mcp.json') 'servers' $Name $e
        }
        'antigravity' {
            # Current releases read .gemini\config\mcp_config.json, earlier ones .gemini\antigravity\mcp_config.json:
            # every one of the two folders that exists gets the entry; on a fresh PC, the current one.
            $files = @(@('config', 'antigravity') | Where-Object { Test-Path (Join-Path $GeminiDir $_) } |
                ForEach-Object { Join-Path $GeminiDir "$_\mcp_config.json" })
            if (-not $files) {
                if (-not $add) { return }
                New-Item -ItemType Directory -Force (Join-Path $GeminiDir 'config') | Out-Null
                $files = @(Join-Path $GeminiDir 'config\mcp_config.json')
            }
            foreach ($f in $files) { Set-JsonServer $f 'mcpServers' $Name $stdio }
        }
        'gemini-cli' {
            $e = $null; if ($add) { $e = [ordered]@{ command = $Py; args = @($Server); timeout = 60000 } }
            if ($add) { New-Item -ItemType Directory -Force $GeminiDir | Out-Null }
            Set-JsonServer (Join-Path $GeminiDir 'settings.json') 'mcpServers' $Name $e
        }
    }
}

function Wait-Closed($procs) {
    while ($true) {
        $running = @(foreach ($p in $procs) {
            $n, $rx = $p -split '\|', 2
            Get-Process -Name $n -ErrorAction SilentlyContinue |
                Where-Object { -not $rx -or ($_.Path -and $_.Path -match $rx) } | ForEach-Object { $_.ProcessName }
        }) | Select-Object -Unique
        if (-not $running) { Say '    all closed' DarkGray; return }
        $cad = @($running | Where-Object { $_ -in @('acad', 'cadp') })
        Say ("    still running: " + ($running -join ', ')) Yellow
        if ($Yes) {
            if ($cad) { throw 'Close the CAD first: the plugin file is in use while it runs.' }
            Say '    (continuing; restart those apps afterwards)' Yellow; return
        }
        $a = Read-Host '    Close them (save your work first), then press Enter.  S = skip'
        if ($a -match '^[Ss]') {
            if ($cad) { throw 'The CAD must be closed: the plugin file is in use while it runs.' }
            return
        }
    }
}

# ================================================================ run
Say ''
Say "  CadBridge $Version - AI agents edit the drawing open in AutoCAD / CAD&PILLAR, live, as DWG" Cyan
Say '  =================================================================================' Cyan
if ($Uninstall) { Say '  UNINSTALL' Yellow }

Step 1 'CAD programs on this PC'
$cadList = @(Find-Cads)
if (-not $cadList) { Say '  No AutoCAD or CAD&PILLAR found on this PC.' Red; exit 1 }
$cadSel = @(Pick $cadList $(if ($Uninstall) { 'Remove from which' } else { 'Install for which' }) $Cads)
$bad = @($cadSel | Where-Object { -not $_.Ok }); if ($bad) { Say ("  Not supported: " + ($bad.Label -join ', ')) Red; exit 1 }
if (-not $Uninstall) {
    $fresh = @($cadSel | Where-Object { -not (Test-Path $_.UserKey) })
    if ($fresh) { Say ("  Start these once, close them, then run the installer again: " + ($fresh.Label -join ', ')) Yellow; exit 1 }
}
if (-not $cadSel) { Say '  Nothing selected.' Yellow; exit 0 }
Say ('  -> ' + ($cadSel.Label -join ', ')) Green

Step 2 'AI apps'
$appSel = @(Pick $AppList $(if ($Uninstall) { 'Remove from which apps' } else { 'Install for which apps' }) $Clients)
Say ('  -> ' + $(if ($appSel) { $appSel.Label -join ', ' } else { 'none' })) Green

Step 3 'Close programs'
Say '  Close these now (save your work first), including every open terminal session of the AI apps,'
Say '  so they load the new tools when started again:'
Say ('    ' + ((@($cadSel.Label) + @($appSel | ForEach-Object { $_.Label })) -join ', '))
if (-not $Yes) { Read-Host '  Press Enter when done' | Out-Null }
Wait-Closed (@('acad|', 'cadp|') + @($appSel | ForEach-Object { $_.Procs }))

if ($Uninstall) {
    Step 4 'Removing'
    foreach ($a in $appSel) { Say "  $($a.Label)"; Configure $a.Key $false }
    foreach ($c in $cadSel) { Remove-Item (Join-Path $c.UserKey 'Applications\CadBridge') -Recurse -ErrorAction SilentlyContinue; Say "  $($c.Label): autoload removed" }
    if (Ask 'Also delete the plugin file and the Python environment (needed by any CAD still set up)?') {
        Remove-Item $DllDir, $PyDir -Recurse -Force -ErrorAction SilentlyContinue
        Say "  deleted $DllDir and $PyDir" DarkGray
    }
    Step 5 'Done'; Say '  Uninstalled.' Green; exit 0
}

Step 4 'Installing'
Say '  Plugin'
foreach ($t in @($cadSel | ForEach-Object { $_.Tfm } | Select-Object -Unique)) {
    New-Item -ItemType Directory -Force (Join-Path $DllDir $t) | Out-Null
    Copy-Item (Join-Path $Here "bin\$t\CadBridge.dll") (Join-Path $DllDir $t) -Force
}
Remove-Item (Join-Path $DllDir 'CadBridge.dll') -ErrorAction SilentlyContinue   # layout of earlier versions
foreach ($c in $cadSel) {
    $k = Join-Path $c.UserKey 'Applications\CadBridge'
    New-Item $k -Force | Out-Null
    Set-ItemProperty $k DESCRIPTION 'CadBridge: live-DWG pipe server for AI agents (MCP)'
    Set-ItemProperty $k LOADER (Join-Path $DllDir "$($c.Tfm)\CadBridge.dll")
    New-ItemProperty $k LOADCTRLS -Value 2 -PropertyType DWord -Force | Out-Null   # load at startup
    New-ItemProperty $k MANAGED -Value 1 -PropertyType DWord -Force | Out-Null     # .NET assembly
    Say "    - $($c.Label): loads $($c.Tfm)\CadBridge.dll at every start" DarkGray
}

Say '  Python (downloaded automatically when missing)'
$uv = Get-Command uv -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source
if (-not $uv) { $uv = @("$env:USERPROFILE\.local\bin\uv.exe", "$env:USERPROFILE\.cargo\bin\uv.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1 }
if (-not $uv) {
    Say '    - installing uv (astral.sh), the Python manager' DarkGray
    $env:UV_NO_MODIFY_PATH = '1'
    Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    $uv = "$env:USERPROFILE\.local\bin\uv.exe"
    if (-not (Test-Path $uv)) { throw 'uv could not be installed (internet access?)' }
}
New-Item -ItemType Directory -Force $PyDir | Out-Null
if (-not (Test-Path $Py)) { & $uv venv --python 3.12 --python-preference managed (Join-Path $PyDir 'venv') 2>&1 | Out-Null }
if (-not (Test-Path $Py)) { throw 'Python could not be installed (internet access?)' }
& $uv pip install --quiet --python $Py 'fastmcp>=4.2,<5' pywin32 pillow 2>&1 | Out-Null
if ($LASTEXITCODE) { throw 'Python libraries could not be installed (internet access?)' }
Copy-Item (Join-Path $Here 'src\cadbridge_mcp.py') $Server -Force
Copy-Item (Join-Path $Here 'VERSION') (Join-Path $PyDir 'VERSION') -Force
Get-ChildItem $PyDir -Filter '*.py' | Where-Object { $_.Name -ne 'cadbridge_mcp.py' -and $Legacy -contains $_.BaseName.Replace('_mcp', '') } | Remove-Item
Push-Location $PyDir
try { & $Py -c "import cadbridge_mcp" } finally { Pop-Location }
if ($LASTEXITCODE) { throw 'the MCP server does not start' }
Say "    - $Server" DarkGray

if ($appSel) { Say '  AI apps' }
foreach ($a in $appSel) { Say "   $($a.Label)"; Configure $a.Key $true }

Step 5 'Done'
Say '  1. Start your CAD and open a drawing.' Green
Say '  2. Start your AI app and ask, for example:' Green
Say '       "use cad_status, then list the layers of the open drawing"'
Say "  Plugin log: $DllDir\cadbridge.log     Remove: double-click Uninstall.cmd"
Say "  Any other MCP app: command $Py  argument $Server  (stdio)"
Say ''
