# Builds the CadBridge plugin with the C# compiler (Roslyn) bundled in PowerShell 7, so no .NET SDK is needed.
# One source (CadBridge.cs), two builds, each compiled against Autodesk's official AutoCAD.NET reference
# packages from nuget.org (downloaded once, cached in %LOCALAPPDATA%\CadBridge\refs):
#   bin\net8\CadBridge.dll   AutoCAD.NET 25.0, .NET 8          -> AutoCAD 2025, 2026 and OEMs on them (CAD&PILLAR 11)
#   bin\net48\CadBridge.dll  AutoCAD.NET 24.0, .NET Framework 4.8 (NETFRAMEWORK symbol) -> AutoCAD 2021-2024
# Referencing the lowest release of a generation lets one DLL load in every later release of it.
#   pwsh src/build.ps1 [-Target net8|net48|all]
param(
    [ValidateSet('all', 'net8', 'net48')] [string]$Target = 'all',
    [string]$Out = (Join-Path $PSScriptRoot '..\bin')
)
$ErrorActionPreference = 'Stop'
$Version = (Get-Content (Join-Path $PSScriptRoot '..\VERSION') -Raw).Trim()   # the one place the version is typed
if ($Version -notmatch '^\d+\.\d+\.\d+$') { throw "VERSION must be MAJOR.MINOR.PATCH, got '$Version'" }
if ($PSVersionTable.PSEdition -ne 'Core') { throw 'Run with PowerShell 7 (pwsh): it carries the Roslyn compiler.' }
Add-Type -Path (Join-Path $PSHOME 'Microsoft.CodeAnalysis.dll')
Add-Type -Path (Join-Path $PSHOME 'Microsoft.CodeAnalysis.CSharp.dll')

function NuGetRefs($pkgs) {
    $cache = Join-Path $env:LOCALAPPDATA 'CadBridge\refs'
    foreach ($p in $pkgs) {
        $dir = Join-Path $cache "$($p.Id).$($p.Ver)"
        if (-not (Test-Path $dir)) {
            New-Item -ItemType Directory -Force $cache | Out-Null
            $zip = "$dir.zip"
            Invoke-WebRequest "https://api.nuget.org/v3-flatcontainer/$($p.Id)/$($p.Ver)/$($p.Id).$($p.Ver).nupkg" -OutFile $zip
            Expand-Archive $zip $dir; Remove-Item $zip
        }
        Get-ChildItem (Join-Path $dir 'lib') -Recurse -Filter $p.Dll | Select-Object -First 1 -ExpandProperty FullName
    }
}

function Usable($files) {
    $files | Where-Object { try { [Reflection.AssemblyName]::GetAssemblyName($_) | Out-Null; $true } catch { $false } }
}

function Build($tfm) {
    if ($tfm -eq 'net8') {
        $shared = 'C:\Program Files\dotnet\shared'
        $pick = { param($fw) Get-ChildItem (Join-Path $shared $fw) -Directory | Where-Object Name -like '8.0.*' |
                Sort-Object { [version]$_.Name } | Select-Object -Last 1 -ExpandProperty FullName }
        $core = & $pick 'Microsoft.NETCore.App'; $desk = & $pick 'Microsoft.WindowsDesktop.App'
        if (-not $core -or -not $desk) { throw '.NET 8 runtime (NETCore.App + WindowsDesktop.App 8.0.x) not found' }
        $refs = @(Usable (Get-ChildItem $core -Filter *.dll).FullName) + (Join-Path $desk 'WindowsBase.dll')
        $ver = '25.0'; $symbols = @()
        $pkgs = @(@{ Id = 'autocad.net'; Ver = '25.0.1'; Dll = 'AcMgd.dll' }, @{ Id = 'autocad.net.core'; Ver = '25.0.0'; Dll = 'AcCoreMgd.dll' },
                  @{ Id = 'autocad.net.model'; Ver = '25.0.0'; Dll = 'AcDbMgd.dll' })
    } else {
        $fx = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319'
        $refs = @('mscorlib.dll', 'System.dll', 'System.Core.dll', 'WPF\WindowsBase.dll') | ForEach-Object { Join-Path $fx $_ }
        $ver = '24.0'; $symbols = @('NETFRAMEWORK')
        $pkgs = @(@{ Id = 'autocad.net'; Ver = '24.0.0'; Dll = 'AcMgd.dll' }, @{ Id = 'autocad.net.core'; Ver = '24.0.0'; Dll = 'AcCoreMgd.dll' },
                  @{ Id = 'autocad.net.model'; Ver = '24.0.0'; Dll = 'AcDbMgd.dll' })
    }
    $refs += NuGetRefs $pkgs
    [Microsoft.CodeAnalysis.MetadataReference[]]$mrefs = $refs | ForEach-Object { [Microsoft.CodeAnalysis.MetadataReference]::CreateFromFile($_) }

    $parse = [Microsoft.CodeAnalysis.CSharp.CSharpParseOptions]::Default.WithLanguageVersion(
        [Microsoft.CodeAnalysis.CSharp.LanguageVersion]::Latest).WithPreprocessorSymbols([string[]]$symbols)
    $tree = [Microsoft.CodeAnalysis.CSharp.CSharpSyntaxTree]::ParseText((Get-Content (Join-Path $PSScriptRoot 'CadBridge.cs') -Raw), $parse)
    $opts = [Microsoft.CodeAnalysis.CSharp.CSharpCompilationOptions]::new([Microsoft.CodeAnalysis.OutputKind]::DynamicallyLinkedLibrary)
    $opts = $opts.WithPlatform([Microsoft.CodeAnalysis.Platform]::X64).WithOptimizationLevel([Microsoft.CodeAnalysis.OptimizationLevel]::Release)
    $stamp = [Microsoft.CodeAnalysis.CSharp.CSharpSyntaxTree]::ParseText(
        "[assembly: System.Reflection.AssemblyVersion(`"$Version.0`")][assembly: System.Reflection.AssemblyFileVersion(`"$Version.0`")]" +
        "[assembly: System.Reflection.AssemblyInformationalVersion(`"$Version`")]", $parse)
    $comp = [Microsoft.CodeAnalysis.CSharp.CSharpCompilation]::Create('CadBridge', [Microsoft.CodeAnalysis.SyntaxTree[]]@($tree, $stamp), $mrefs, $opts)

    $dir = Join-Path $Out $tfm
    New-Item -ItemType Directory -Force $dir | Out-Null
    $dll = Join-Path (Resolve-Path $dir) 'CadBridge.dll'
    $fs = [IO.File]::Create($dll)
    # Win32 version resource, so Explorer's file Properties shows the version too
    $win32 = $comp.CreateDefaultWin32Resources($true, $false, $null, $null)
    try { $res = $comp.Emit($fs, $null, $null, $win32) } finally { $fs.Dispose(); $win32.Dispose() }
    $res.Diagnostics | Where-Object { $_.Severity -ge 'Warning' } | ForEach-Object { "  [$tfm] $_" }
    if (-not $res.Success) { Remove-Item $dll; throw "build $tfm failed" }
    "built $dll  v$Version  (AutoCAD.NET $ver)"
}

foreach ($t in $(if ($Target -eq 'all') { 'net8', 'net48' } else { $Target })) { Build $t }
