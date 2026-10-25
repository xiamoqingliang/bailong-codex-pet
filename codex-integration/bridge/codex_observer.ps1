[CmdletBinding()]
param(
    [string]$StatePath = '',
    [string]$StopFile = '',
    [string]$PythonPath = '',
    [ValidateRange(200, 10000)][int]$IntervalMs = 200,
    [switch]$Once,
    [switch]$ProbeIdentity,
    [long]$ProbeHwnd = 0,
    [int]$ProbePid = 0,
    [string]$ProbeReadyPath = '',
    [string]$ProbeToken = ''
)

# Launch with powershell.exe -NoProfile -WindowStyle Hidden -File <this file>.
# Read-only observer: never invokes UI controls or writes Codex settings.
$ErrorActionPreference = 'Stop'
if ([string]::IsNullOrWhiteSpace($StatePath)) { $StatePath = Join-Path $PSScriptRoot 'codex-state.json' }
if ([string]::IsNullOrWhiteSpace($StopFile)) { $StopFile = Join-Path $PSScriptRoot 'observer.stop' }
$stateDirectory = [IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($StatePath))
[IO.Directory]::CreateDirectory($stateDirectory) | Out-Null
$logPath = Join-Path $stateDirectory 'observer.log'
$utf8 = New-Object Text.UTF8Encoding($false)
$lastErrors = @{}

function Write-ObserverError([string]$Stage, [Exception]$Failure) {
    try {
        $kind = $Failure.GetType().Name
        $key = $Stage + ':' + $kind
        $now = [DateTime]::UtcNow
        if ($lastErrors.ContainsKey($key) -and ($now - $lastErrors[$key]).TotalSeconds -lt 30) { return }
        $lastErrors[$key] = $now
        if ([IO.File]::Exists($logPath) -and (Get-Item -LiteralPath $logPath).Length -gt 65536) {
            $text = [IO.File]::ReadAllText($logPath)
            [IO.File]::WriteAllText($logPath, $text.Substring([Math]::Max(0, $text.Length - 16000)), $utf8)
        }
        [IO.File]::AppendAllText($logPath, ($now.ToString('o') + ' ' + $key + [Environment]::NewLine), $utf8)
    } catch { }
}

try {
    Add-Type -AssemblyName UIAutomationClient
    Add-Type -AssemblyName UIAutomationTypes
    Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;
public static class BailongCodexObserverNative {
    private delegate bool EnumWindowsProc(IntPtr hwnd, IntPtr parameter);
    [DllImport("user32.dll")] private static extern bool EnumWindows(EnumWindowsProc callback, IntPtr parameter);
    [DllImport("user32.dll")] public static extern bool IsWindow(IntPtr hwnd);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hwnd);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] private static extern IntPtr GetProp(IntPtr hwnd, string name);
    public static bool HasBridgeMask(IntPtr hwnd) { return GetProp(hwnd, "BailongNativeMiniRegionOwnerV1") != IntPtr.Zero; }
    [DllImport("user32.dll")] private static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint pid);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] private static extern int GetClassName(IntPtr hwnd, StringBuilder value, int count);
    [DllImport("user32.dll", EntryPoint = "GetWindowLongW")] private static extern int GetWindowLong(IntPtr hwnd, int index);
    [DllImport("user32.dll")] private static extern uint GetDpiForWindow(IntPtr hwnd);
    [StructLayout(LayoutKind.Sequential)] public struct Rect { public int Left, Top, Right, Bottom; }
    [DllImport("user32.dll")] private static extern bool GetWindowRect(IntPtr hwnd, out Rect rectangle);
    [DllImport("user32.dll")] private static extern IntPtr SetThreadDpiAwarenessContext(IntPtr context);
    public sealed class Candidate {
        public IntPtr Handle;
        public int Pid;
        public bool Visible;
        public uint Dpi;
    }
    public static void UsePhysicalCoordinates() {
        try { SetThreadDpiAwarenessContext(new IntPtr(-4)); } catch (EntryPointNotFoundException) { }
    }
    public static Rect? WindowRectangle(IntPtr hwnd) {
        Rect rectangle;
        return GetWindowRect(hwnd, out rectangle) ? (Rect?)rectangle : null;
    }
    public static Candidate[] Find(int[] processIds) {
        var allowed = new HashSet<int>(processIds);
        var result = new List<Candidate>();
        EnumWindows(delegate(IntPtr hwnd, IntPtr unused) {
            uint pid;
            GetWindowThreadProcessId(hwnd, out pid);
            if (!allowed.Contains((int)pid)) return true;
            long extendedStyle = (uint)GetWindowLong(hwnd, -20);
            if ((extendedStyle & 0x88) != 0x88) return true;
            var name = new StringBuilder(256);
            GetClassName(hwnd, name, name.Capacity);
            if (name.ToString() != "Chrome_WidgetWin_1") return true;
            uint dpi = 96;
            try { uint value = GetDpiForWindow(hwnd); if (value > 0) dpi = value; }
            catch (EntryPointNotFoundException) { }
            result.Add(new Candidate { Handle = hwnd, Pid = (int)pid, Visible = IsWindowVisible(hwnd), Dpi = dpi });
            return true;
        }, IntPtr.Zero);
        return result.ToArray();
    }
}
'@
    [BailongCodexObserverNative]::UsePhysicalCoordinates()
} catch {
    Write-ObserverError 'initialize' $_.Exception
    throw
}

# ASCII source constants also work when launched by Windows PowerShell 5.1.
$newChatNames = @(
    'Start new chat', 'Start a new chat',
    (-join [char[]]@(0x5F00, 0x59CB, 0x65B0, 0x804A, 0x5929)),
    (-join [char[]]@(0x958B, 0x59CB, 0x65B0, 0x804A, 0x5929))
)
$nameConditions = @($newChatNames | ForEach-Object {
    New-Object Windows.Automation.PropertyCondition([Windows.Automation.AutomationElement]::NameProperty, $_)
})
$nameCondition = New-Object Windows.Automation.OrCondition(,[Windows.Automation.Condition[]]$nameConditions)
$buttonCondition = New-Object Windows.Automation.PropertyCondition(
    [Windows.Automation.AutomationElement]::ControlTypeProperty,
    [Windows.Automation.ControlType]::Button
)
$toolbarButtonCondition = New-Object Windows.Automation.AndCondition($buttonCondition, $nameCondition)
$imageCondition = New-Object Windows.Automation.PropertyCondition(
    [Windows.Automation.AutomationElement]::ControlTypeProperty,
    [Windows.Automation.ControlType]::Image
)
$onScreenCondition = New-Object Windows.Automation.PropertyCondition(
    [Windows.Automation.AutomationElement]::IsOffscreenProperty, $false
)
$visibleImageCondition = New-Object Windows.Automation.AndCondition($imageCondition, $onScreenCondition)
$petNameSuffixes = @(
    (-join [char[]]@(0x865A, 0x62DF, 0x5BA0, 0x7269)),
    (-join [char[]]@(0x865B, 0x64EC, 0x5BF5, 0x7269))
)
$toolbarCache = @{}
$knownOverlayHandles = @{}
$packageExecutables = @()
$packageCheckedAt = [DateTime]::MinValue
$mainProcessIds = @()
$processFingerprint = ''
$mainProcessesCheckedAt = [DateTime]::MinValue
$globalStatePath = Join-Path ([Environment]::GetFolderPath('UserProfile')) '.codex\.codex-global-state.json'
$globalStateStamp = $null
$globalStateReadAt = [DateTime]::MinValue
$globalStateVerifiedAt = [DateTime]::MinValue
$requestedOpen = $null
$fullBailongMigratedIds = @()
$desktopConfigPath = Join-Path ([Environment]::GetFolderPath('UserProfile')) '.codex\config.toml'
$desktopConfigStamp = $null
$desktopConfigReadAt = [DateTime]::MinValue
$desktopConfigVerifiedAt = [DateTime]::MinValue
$desktopSelection = [ordered]@{ selectionKnown = $false; selectedAvatarId = $null; nativePetVisible = $null }
$selectionPolicyPath = Join-Path $PSScriptRoot 'bridge_policy.py'
if (!$ProbeIdentity -and [string]::IsNullOrWhiteSpace($PythonPath)) {
    try { $PythonPath = (Get-Command python.exe -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source }
    catch { Write-ObserverError 'selection-python' $_.Exception }
} elseif ([IO.Path]::GetFileName($PythonPath) -ieq 'pythonw.exe') {
    $PythonPath = Join-Path ([IO.Path]::GetDirectoryName($PythonPath)) 'python.exe'
}

function Test-TransientStateReadError {
    param([Exception] $Failure)
    for ($current = $Failure; $null -ne $current; $current = $current.InnerException) {
        if ($current -is [IO.IOException] -or $current -is [UnauthorizedAccessException] -or
            $current -is [System.Management.Automation.ItemNotFoundException]) { return $true }
    }
    return $false
}

function Test-RecentStateRead {
    param($VerifiedAt, [DateTime] $Now)
    if ($VerifiedAt -isnot [DateTime]) { return $false }
    $age = ($Now - $VerifiedAt).TotalSeconds
    return $age -ge 0 -and $age -le 1
}

function Read-DesktopProjection {
    # Preserve the difference between a sharing violation and malformed TOML.
    # The parser emits only the same three whitelisted selection fields plus
    # an error category; no configuration contents or paths are printed.
    $program = @'
import json,sys,tomllib
from pathlib import Path
value={'selectionKnown':False,'selectedAvatarId':None,'nativePetVisible':None,'readFailure':None}
try:
    path=Path(sys.argv[1])
    if path.stat().st_size>1048576:
        raise ValueError('oversize')
    desktop=tomllib.loads(path.read_text(encoding='utf-8-sig')).get('desktop',{})
    if not isinstance(desktop,dict):
        raise ValueError('invalid desktop')
    selected=desktop.get('selected-avatar-id')
    visible=desktop.get('avatar-overlay-pet-visible',True)
    if selected is not None and (not isinstance(selected,str) or not selected):
        raise ValueError('invalid selection')
    if type(visible) is not bool:
        raise ValueError('invalid visibility')
    value.update(selectionKnown=True,selectedAvatarId=selected,nativePetVisible=visible)
except OSError:
    value['readFailure']='io'
except (ValueError,UnicodeError,TypeError):
    value['readFailure']='invalid'
print(json.dumps(value,ensure_ascii=True,separators=(',',':')))
'@
    $encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($program))
    $bootstrap = "import base64;exec(base64.b64decode('$encoded'))"
    $output = & $PythonPath -c $bootstrap $desktopConfigPath
    if ($LASTEXITCODE -ne 0) { throw [InvalidOperationException]::new('Selection parser failed') }
    return ($output -join [Environment]::NewLine) | ConvertFrom-Json -ErrorAction Stop
}

function Read-DesktopSelection {
    # Use Python's TOML parser, not line regexes: comments, quoted keys and
    # partially-written files must not accidentally select the wrong pet.
    $now = [DateTime]::UtcNow
    try {
        $info = Get-Item -LiteralPath $desktopConfigPath -ErrorAction Stop
        $stamp = $info.LastWriteTimeUtc.Ticks.ToString() + ':' + $info.Length
        if ($stamp -ne $script:desktopConfigStamp -or ($now - $script:desktopConfigReadAt).TotalSeconds -ge 5) {
            if ([string]::IsNullOrWhiteSpace($PythonPath) -or ![IO.File]::Exists($PythonPath)) {
                throw [IO.FileNotFoundException]::new('Selection parser runtime unavailable')
            }
            $projection = Read-DesktopProjection
            if ($projection.readFailure -eq 'io') { throw [IO.IOException]::new('Selection state temporarily unavailable') }
            if ($projection.readFailure -eq 'invalid') { throw [FormatException]::new('Invalid selection state') }
            if ($projection.selectionKnown -isnot [bool] -or
                ($null -ne $projection.selectedAvatarId -and $projection.selectedAvatarId -isnot [string]) -or
                ($projection.selectionKnown -and $projection.nativePetVisible -isnot [bool])) {
                throw [FormatException]::new('Invalid selection projection')
            }
            $script:desktopSelection = [ordered]@{
                selectionKnown = [bool]$projection.selectionKnown
                selectedAvatarId = $projection.selectedAvatarId
                nativePetVisible = $projection.nativePetVisible
            }
            $script:desktopConfigStamp = $stamp
            $script:desktopConfigReadAt = $now
        }
        # An unchanged stamp verifies that the successfully parsed projection
        # still belongs to this file; the grace budget starts at the last such
        # verification, and is never renewed on a failed read.
        $script:desktopConfigVerifiedAt = [DateTime]::UtcNow
    } catch {
        $keep = (Test-TransientStateReadError $_.Exception) -and
            (Test-RecentStateRead $script:desktopConfigVerifiedAt ([DateTime]::UtcNow))
        if (-not $keep) {
            $script:desktopSelection = [ordered]@{ selectionKnown = $false; selectedAvatarId = $null; nativePetVisible = $null }
            $script:desktopConfigVerifiedAt = [DateTime]::MinValue
        }
        $script:desktopConfigStamp = $null
        Write-ObserverError 'selection-read' $_.Exception
    }
    return $script:desktopSelection
}

function Read-GlobalPetIntent {
    $now = [DateTime]::UtcNow
    try {
        $info = Get-Item -LiteralPath $globalStatePath -ErrorAction Stop
        $stamp = $info.LastWriteTimeUtc.Ticks.ToString() + ':' + $info.Length
        if ($stamp -ne $script:globalStateStamp -or ($now - $script:globalStateReadAt).TotalSeconds -ge 5) {
            $document = [IO.File]::ReadAllText($globalStatePath) | ConvertFrom-Json -ErrorAction Stop
            if ($document -isnot [PSCustomObject]) { throw [FormatException]::new('Invalid global state') }
            $nextOpen = $null
            if ($document.'electron-avatar-overlay-open' -is [bool]) {
                $nextOpen = $document.'electron-avatar-overlay-open'
            }
            $migrationMap = $document.'electron-persisted-atom-state'.'migrated-cloud-pet-ids-v1'
            if ($null -ne $migrationMap -and $migrationMap -isnot [PSCustomObject]) {
                throw [FormatException]::new('Invalid pet migration state')
            }
            $nextIds = @(
                foreach ($accountMap in $migrationMap.PSObject.Properties) {
                    $mapped = $accountMap.Value.'custom:bailong-full'
                    if ($mapped -is [string] -and $mapped -cmatch '^pet_[A-Za-z0-9_-]+$') { $mapped }
                }
            ) | Sort-Object -Unique
            $script:requestedOpen = $nextOpen
            $script:fullBailongMigratedIds = @($nextIds)
            $document = $null
            $script:globalStateStamp = $stamp
            $script:globalStateReadAt = $now
        }
        $script:globalStateVerifiedAt = [DateTime]::UtcNow
    } catch {
        # Only transient I/O retains the last verified projection, at most 1s.
        # Successful false/new mappings and malformed JSON take effect now.
        $keep = (Test-TransientStateReadError $_.Exception) -and
            (Test-RecentStateRead $script:globalStateVerifiedAt ([DateTime]::UtcNow))
        if (-not $keep) {
            $script:requestedOpen = $null
            $script:fullBailongMigratedIds = @()
            $script:globalStateVerifiedAt = [DateTime]::MinValue
        }
        $script:globalStateStamp = $null
        Write-ObserverError 'intent-read' $_.Exception
    }
}

function Get-ToolbarRect([IntPtr]$Handle) {
    $key = $Handle.ToInt64().ToString()
    $group = $toolbarCache[$key]
    if ($null -ne $group) {
        try {
            $current = $group.Current
            $rect = $current.BoundingRectangle
            if (!$current.IsOffscreen -and !$rect.IsEmpty -and $rect.Width -gt 0 -and $rect.Height -gt 0) {
                return [ordered]@{ x = [Math]::Round($rect.X, 2); y = [Math]::Round($rect.Y, 2); width = [Math]::Round($rect.Width, 2); height = [Math]::Round($rect.Height, 2) }
            }
        } catch { }
        $toolbarCache.Remove($key)
    }
    try {
        $root = [Windows.Automation.AutomationElement]::FromHandle($Handle)
        if ($null -eq $root) { return $null }
        # Conditions request only the named control, never conversation text.
        $button = $root.FindFirst([Windows.Automation.TreeScope]::Descendants, $toolbarButtonCondition)
        if ($null -eq $button -or $button.Current.IsOffscreen) { return $null }
        $group = [Windows.Automation.TreeWalker]::ControlViewWalker.GetParent($button)
        if ($null -eq $group -or $group.Current.ControlType -ne [Windows.Automation.ControlType]::Group) { return $null }
        $current = $group.Current
        $rect = $current.BoundingRectangle
        if ($current.IsOffscreen -or $rect.IsEmpty -or $rect.Width -le 0 -or $rect.Height -le 0) { return $null }
        $toolbarCache[$key] = $group
        return [ordered]@{ x = [Math]::Round($rect.X, 2); y = [Math]::Round($rect.Y, 2); width = [Math]::Round($rect.Width, 2); height = [Math]::Round($rect.Height, 2) }
    } catch {
        Write-ObserverError 'uia' $_.Exception
        return $null
    }
}

function Get-PetImageRect([IntPtr]$Handle) {
    try {
        $root = [Windows.Automation.AutomationElement]::FromHandle($Handle)
        if ($null -eq $root) { return $null }
        # This is called only for verified Codex overlay window candidates.
        # Ask UIA only for visible Image controls, never arbitrary text nodes.
        $images = $root.FindAll([Windows.Automation.TreeScope]::Descendants, $visibleImageCondition)
        foreach ($image in $images) {
            $current = $image.Current
            $name = $current.Name
            $matchesPet = $name -match '(?i)(?:^|\s)pet\s*$'
            foreach ($suffix in $petNameSuffixes) {
                if ($name.TrimEnd().EndsWith($suffix, [StringComparison]::Ordinal)) { $matchesPet = $true; break }
            }
            if (!$matchesPet -or $current.IsOffscreen) { continue }
            $rect = $current.BoundingRectangle
            if ($rect.IsEmpty -or $rect.Width -le 0 -or $rect.Height -le 0) { continue }
            return [ordered]@{ x = [Math]::Round($rect.X, 2); y = [Math]::Round($rect.Y, 2); width = [Math]::Round($rect.Width, 2); height = [Math]::Round($rect.Height, 2) }
        }
    } catch { Write-ObserverError 'uia-pet-image' $_.Exception }
    return $null
}

function Test-HiddenMiniIdentity([IntPtr]$Handle) {
    try {
        $root = [Windows.Automation.AutomationElement]::FromHandle($Handle)
        if ($null -eq $root) { return $false }
        # Only fixed control names and pet image labels are inspected, including
        # a prewarmed invisible Mini. No chat text is read.
        if ($null -ne $root.FindFirst([Windows.Automation.TreeScope]::Descendants, $toolbarButtonCondition)) { return $true }
        $images = $root.FindAll([Windows.Automation.TreeScope]::Descendants, $imageCondition)
        foreach ($image in $images) {
            $name = $image.Current.Name
            if ($name -match '(?i)(?:^|\s)pet\s*$') { return $true }
            foreach ($suffix in $petNameSuffixes) {
                if ($name.TrimEnd().EndsWith($suffix, [StringComparison]::Ordinal)) { return $true }
            }
        }
    } catch { }
    return $false
}

if ($ProbeIdentity) {
    # UIA can wait inside Electron for many seconds. Keep it in a disposable
    # read-only child, never on the observer's host/visibility sampling loop.
    $verified = $false
    $reason = 'target-not-verified'
    try {
        $target = Get-Process -Id $ProbePid -ErrorAction Stop
        if ($target.Path -cmatch '\\WindowsApps\\OpenAI\.Codex_[^\\]+\\app\\ChatGPT\.exe$') {
            $match = @([BailongCodexObserverNative]::Find([int[]]@($ProbePid)) |
                Where-Object { $_.Handle.ToInt64() -eq $ProbeHwnd })
            if ($match.Count -eq 1) {
                # Signal only after PowerShell, UIAutomation and native-code
                # initialization. The parent's UIA budget starts at this point.
                if (![string]::IsNullOrWhiteSpace($ProbeReadyPath) -and $ProbeToken -cmatch '^[a-f0-9]{32}$') {
                    $ready = [ordered]@{ token = $ProbeToken; helperPid = $PID; pid = $ProbePid; hwnd = $ProbeHwnd }
                    [IO.File]::WriteAllText($ProbeReadyPath, ($ready | ConvertTo-Json -Compress), $utf8)
                }
                $verified = Test-HiddenMiniIdentity ([IntPtr]$ProbeHwnd)
                $reason = $(if ($verified) {'verified'} else {'identity-not-found'})
            }
        }
    } catch { $reason = 'probe-error' }
    [ordered]@{ pid = $ProbePid; hwnd = $ProbeHwnd; token = $ProbeToken;
        verified = [bool]$verified; reason = $reason } | ConvertTo-Json -Compress
    exit 0
}

function Initialize-IdentityProbeState {
    $script:identityProbe = $null
    $script:identityRetryAt = @{}
    $script:identityFailures = @{}
    $script:identityLastAttempt = @{}
    $script:identityCleanupPaths = @{}
    $script:identityInitializationSeconds = 20
    $script:identityQuerySeconds = 3
    $script:identityDiagnostic = [ordered]@{
        attempts = 0; verified = 0; failures = 0; cancelled = 0
        initializationTimeouts = 0; queryTimeouts = 0
        lastReason = $null; lastDurationMs = $null; lastInitializationMs = $null
    }
}

function Remove-IdentityProbeFiles {
    # Only paths generated for this observer's own children enter this list.
    # A sharing violation gets another try on a later sampling iteration.
    foreach ($path in @($script:identityCleanupPaths.Keys)) {
        try { [IO.File]::Delete($path); $script:identityCleanupPaths.Remove($path) }
        catch {
            if ([DateTime]::UtcNow -gt $script:identityCleanupPaths[$path]) {
                $script:identityCleanupPaths.Remove($path)
                Write-ObserverError 'identity-cleanup' $_.Exception
            }
        }
    }
}

function Close-IdentityProbe([string]$Reason = 'cancelled', [switch]$Completed) {
    if ($null -eq $script:identityProbe) { return }
    $probe = $script:identityProbe
    $process = $probe.Process
    try { if (!$process.HasExited) { $process.Kill() } } catch { }
    try { $process.Dispose() } catch { }
    if (!$Completed) { $script:identityDiagnostic.cancelled++ }
    $script:identityDiagnostic.lastReason = $Reason
    $script:identityDiagnostic.lastDurationMs = [int][Math]::Max(0, ([DateTime]::UtcNow - $probe.StartedAt).TotalMilliseconds)
    $script:identityCleanupPaths[$probe.ReadyPath] = [DateTime]::UtcNow.AddSeconds(30)
    $script:identityProbe = $null
    Remove-IdentityProbeFiles
}

function Get-IdentityProbeKey($Candidate) {
    return ('{0}:{1}' -f $Candidate.Pid, $Candidate.Handle.ToInt64())
}

function Set-IdentityProbeFailure([string]$Key, [string]$Reason) {
    $count = 1
    if ($script:identityFailures.ContainsKey($Key)) { $count += $script:identityFailures[$Key] }
    $script:identityFailures[$Key] = $count
    $seconds = [Math]::Min(15, [Math]::Pow(2, [Math]::Min(4, $count - 1)))
    $script:identityRetryAt[$Key] = [DateTime]::UtcNow.AddSeconds($seconds)
    $script:identityDiagnostic.failures++
    $script:identityDiagnostic.lastReason = $Reason
}

function Read-IdentityProbeReady($Probe) {
    if ($null -ne $Probe.ReadyAt) { return }
    try {
        $info = Get-Item -LiteralPath $Probe.ReadyPath -ErrorAction Stop
        if ($info.Length -gt 1024) { return }
        $ready = [IO.File]::ReadAllText($Probe.ReadyPath) | ConvertFrom-Json -ErrorAction Stop
        if ($ready.token -ceq $Probe.Token -and $ready.helperPid -eq $Probe.Process.Id -and
            $ready.pid -eq $Probe.Pid -and $ready.hwnd -eq $Probe.Handle) {
            $Probe.ReadyAt = [DateTime]::UtcNow
            $script:identityDiagnostic.lastInitializationMs = [int][Math]::Max(0, ($Probe.ReadyAt - $Probe.StartedAt).TotalMilliseconds)
        }
    } catch { } # Missing/partially written handshake is not a completed phase.
}

function Update-IdentityProbe($Candidates) {
    Remove-IdentityProbeFiles
    $keys = @($Candidates | ForEach-Object { Get-IdentityProbeKey $_ })
    foreach ($table in @($script:identityRetryAt, $script:identityFailures, $script:identityLastAttempt)) {
        foreach ($key in @($table.Keys)) { if ($key -notin $keys) { $table.Remove($key) } }
    }
    if ($null -eq $script:identityProbe) { return }
    $probe = $script:identityProbe
    $present = @($Candidates | Where-Object { $_.Pid -eq $probe.Pid -and $_.Handle.ToInt64() -eq $probe.Handle })
    if ($present.Count -ne 1) { Close-IdentityProbe 'target-disappeared'; return }
    Read-IdentityProbeReady $probe
    if ($probe.Process.HasExited) {
        $verified = $false
        $reason = 'invalid-result'
        try {
            $output = $probe.Process.StandardOutput.ReadToEnd()
            if ($output.Length -le 4096) {
                $result = $output | ConvertFrom-Json -ErrorAction Stop
                if ($result.token -ceq $probe.Token -and $result.pid -eq $probe.Pid -and
                    $result.hwnd -eq $probe.Handle -and $result.verified -is [bool]) {
                    $verified = $result.verified
                    $reason = $(if ($verified) {'verified'} elseif ($result.reason -in @('identity-not-found','target-not-verified','probe-error')) {$result.reason} else {'identity-not-found'})
                }
            }
        } catch { }
        if ($verified) {
            $knownOverlayHandles[$probe.HandleKey] = $probe.Pid
            $script:identityDiagnostic.verified++
            $script:identityFailures.Remove($probe.Key)
            $script:identityRetryAt.Remove($probe.Key)
        } else { Set-IdentityProbeFailure $probe.Key $reason }
        Close-IdentityProbe $reason -Completed
    } elseif ($null -eq $probe.ReadyAt -and ([DateTime]::UtcNow - $probe.StartedAt).TotalSeconds -ge $script:identityInitializationSeconds) {
        $script:identityDiagnostic.initializationTimeouts++
        Set-IdentityProbeFailure $probe.Key 'initialization-timeout'
        Close-IdentityProbe 'initialization-timeout' -Completed
    } elseif ($null -ne $probe.ReadyAt -and ([DateTime]::UtcNow - $probe.ReadyAt).TotalSeconds -ge $script:identityQuerySeconds) {
        $script:identityDiagnostic.queryTimeouts++
        Set-IdentityProbeFailure $probe.Key 'uia-timeout'
        Close-IdentityProbe 'uia-timeout' -Completed
    }
}

function New-IdentityProbeProcess($Info) {
    return [Diagnostics.Process]::Start($Info)
}

function Start-IdentityProbe($Candidate) {
    if ($null -ne $script:identityProbe) { return }
    $key = Get-IdentityProbeKey $Candidate
    if ($identityRetryAt.ContainsKey($key) -and [DateTime]::UtcNow -lt $identityRetryAt[$key]) { return }
    $handleKey = $Candidate.Handle.ToInt64().ToString()
    $token = [Guid]::NewGuid().ToString('N')
    $readyPath = Join-Path $stateDirectory ('.identity-probe.' + $PID + '.' + $token + '.json')
    $info = New-Object Diagnostics.ProcessStartInfo
    $info.FileName = Join-Path $PSHOME 'powershell.exe'
    $info.Arguments = '-NoProfile -ExecutionPolicy Bypass -File "' + $PSCommandPath +
        '" -ProbeIdentity -ProbePid ' + $Candidate.Pid + ' -ProbeHwnd ' + $handleKey +
        ' -ProbeReadyPath "' + $readyPath + '" -ProbeToken ' + $token
    if (![string]::IsNullOrWhiteSpace($PythonPath)) { $info.Arguments += ' -PythonPath "' + $PythonPath + '"' }
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.WindowStyle = [Diagnostics.ProcessWindowStyle]::Hidden
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $script:identityDiagnostic.attempts++
    $script:identityDiagnostic.lastInitializationMs = $null
    $script:identityLastAttempt[$key] = [DateTime]::UtcNow
    try {
        $process = New-IdentityProbeProcess $info
        $script:identityProbe = @{ Process = $process; Pid = $Candidate.Pid;
            Handle = $Candidate.Handle.ToInt64(); HandleKey = $handleKey; Key = $key;
            Token = $token; ReadyPath = $readyPath; ReadyAt = $null; StartedAt = [DateTime]::UtcNow }
    } catch {
        Set-IdentityProbeFailure $key 'start-failed'
        Write-ObserverError 'identity-start' $_.Exception
    }
}

function Get-IdentityProbeCandidates($Candidates) {
    # Visible windows come first. Within each group, try the least recently
    # attempted target so an unrelated tool window cannot monopolize probes.
    return @($Candidates | Sort-Object @{Expression = { [bool]$_.Visible }; Descending = $true},
        @{Expression = { $key = Get-IdentityProbeKey $_; if ($script:identityLastAttempt.ContainsKey($key)) {$script:identityLastAttempt[$key]} else {[DateTime]::MinValue} }})
}

function Get-IdentityProbeDiagnostic {
    $value = [ordered]@{}
    foreach ($key in $script:identityDiagnostic.Keys) { $value[$key] = $script:identityDiagnostic[$key] }
    $value.phase = $(if ($null -eq $script:identityProbe) {'idle'} elseif ($null -eq $script:identityProbe.ReadyAt) {'initializing'} else {'querying-uia'})
    $value.helperPid = $(if ($null -ne $script:identityProbe) {$script:identityProbe.Process.Id} else {$null})
    return $value
}

Initialize-IdentityProbeState

function Write-AtomicState([string]$Content) {
    $temporary = Join-Path $stateDirectory ('.codex-state.' + $PID + '.tmp')
    try {
        [IO.File]::WriteAllText($temporary, $Content + [Environment]::NewLine, $utf8)
        for ($attempt = 0; $attempt -lt 3; $attempt++) {
            try {
                if ([IO.File]::Exists($StatePath)) {
                    [IO.File]::Replace($temporary, $StatePath, [NullString]::Value)
                } else {
                    [IO.File]::Move($temporary, $StatePath)
                }
                return $true
            } catch {
                if ($attempt -ge 2) { throw }
                Start-Sleep -Milliseconds 25
            }
        }
    } catch {
        Write-ObserverError 'state-write' $_.Exception
        return $false
    } finally {
        if ([IO.File]::Exists($temporary)) { [IO.File]::Delete($temporary) }
    }
}

$lastPayload = $null
$lastWrittenAt = [DateTime]::MinValue
do {
    if ([IO.File]::Exists($StopFile)) { break }
    $startedAt = [DateTime]::UtcNow
    $running = $false
    $overlayOpen = $false
    $toolbarRect = $null
    $petRect = $null
    $overlayHandle = $null
    $overlayWindowRect = $null
    $dpi = $null
    $guiPid = $null
    $appStartedAt = $null
    try {
        if (($startedAt - $packageCheckedAt).TotalSeconds -ge 30) {
            $packageCheckedAt = $startedAt
            try {
                $registered = @(Get-AppxPackage -Name 'OpenAI.Codex' -ErrorAction Stop)
                $packageExecutables = @($registered | ForEach-Object {
                    if (![string]::IsNullOrWhiteSpace($_.InstallLocation)) {
                        [IO.Path]::GetFullPath((Join-Path $_.InstallLocation 'app\ChatGPT.exe'))
                    }
                })
            } catch { Write-ObserverError 'package-query' $_.Exception }
        }
        $packageProcesses = @(Get-Process -Name 'ChatGPT' -ErrorAction SilentlyContinue | Where-Object {
            try { $packageExecutables -contains $_.Path } catch { $false }
        })
        $fingerprint = (@($packageProcesses.Id | Sort-Object) -join ',')
        $now = [DateTime]::UtcNow
        if ($fingerprint -ne $processFingerprint -or ($now - $mainProcessesCheckedAt).TotalSeconds -ge 10) {
            try {
                # Only process classification is retained; never log command lines.
                # Renderer/GPU leftovers must not become a fake Codex restart.
                $ids = @($packageProcesses.Id)
                $mainProcessIds = @(Get-CimInstance Win32_Process -Filter "Name = 'ChatGPT.exe'" -ErrorAction Stop |
                    Where-Object { $_.ProcessId -in $ids -and $_.CommandLine -is [string] -and
                        $_.CommandLine -notmatch '(?i)(?:^|\s)--type=' } | ForEach-Object { [int]$_.ProcessId })
                $processFingerprint = $fingerprint
                $mainProcessesCheckedAt = $now
            } catch { Write-ObserverError 'main-process-query' $_.Exception }
        }
        $guiProcesses = @($packageProcesses | Where-Object { $_.Id -in $mainProcessIds })
        $running = $guiProcesses.Count -gt 0
        if ($running) {
            $processIds = [int[]]@($guiProcesses | ForEach-Object { $_.Id })
            $candidates = @([BailongCodexObserverNative]::Find($processIds))
            Update-IdentityProbe $candidates
            $currentHandleKeys = @($candidates | ForEach-Object { $_.Handle.ToInt64().ToString() })
            foreach ($key in @($toolbarCache.Keys)) { if ($key -notin $currentHandleKeys) { $toolbarCache.Remove($key) } }
            foreach ($key in @($knownOverlayHandles.Keys)) { if ($key -notin $currentHandleKeys) { $knownOverlayHandles.Remove($key) } }
            # Identify the toolbar or named pet Image before trusting a tool window.
            $knownVisible = $null
            $knownHidden = $null
            foreach ($candidate in (Get-IdentityProbeCandidates $candidates)) {
                $key = $candidate.Handle.ToInt64().ToString()
                $known = ($knownOverlayHandles.ContainsKey($key) -and $knownOverlayHandles[$key] -eq $candidate.Pid)
                if (!$known -and [BailongCodexObserverNative]::HasBridgeMask($candidate.Handle)) { $known = $true }
                if ($known) {
                    $knownOverlayHandles[$key] = $candidate.Pid
                    if ($candidate.Visible) { $knownVisible = $candidate }
                    else { $knownHidden = $candidate; continue }
                    # Identity is already established. Re-entering Electron's
                    # accessibility provider during WM_CLOSE can block this
                    # lifecycle observer and delay the next Show/host exit.
                    continue
                }
                Start-IdentityProbe $candidate
            }
            if (!$overlayOpen -and $null -ne $knownVisible) {
                $overlayOpen = $true
                $overlayHandle = $knownVisible.Handle
                $dpi = [int]$knownVisible.Dpi
                $guiPid = $knownVisible.Pid
            }
            if ($null -eq $overlayHandle -and $null -ne $knownHidden) {
                $overlayHandle = $knownHidden.Handle
                $dpi = [int]$knownHidden.Dpi
                $guiPid = $knownHidden.Pid
            }
            if ($null -eq $guiPid) {
                if ($candidates.Count -gt 0) { $guiPid = $candidates[0].Pid }
                else { $guiPid = ($guiProcesses | Sort-Object StartTime | Select-Object -First 1).Id }
            }
        } else {
            Close-IdentityProbe 'codex-not-running'
            $identityRetryAt.Clear()
            $identityFailures.Clear()
            $identityLastAttempt.Clear()
            $toolbarCache.Clear()
            $knownOverlayHandles.Clear()
        }
        if ($null -ne $overlayHandle) {
            $nativeRect = [BailongCodexObserverNative]::WindowRectangle($overlayHandle)
            if ($null -ne $nativeRect) {
                $overlayWindowRect = [ordered]@{ x = $nativeRect.Left; y = $nativeRect.Top; width = $nativeRect.Right - $nativeRect.Left; height = $nativeRect.Bottom - $nativeRect.Top }
            }
        }
    } catch {
        Write-ObserverError 'observe' $_.Exception
        # Do not keep an old visible position after an incomplete observation.
        $overlayOpen = $false
        $toolbarRect = $null
        $petRect = $null
        $overlayHandle = $null
        $overlayWindowRect = $null
        $dpi = $null
    }
    # Selection remains observable even when a native overlay is hidden or a
    # temporary UIA failure prevented window geometry observation.
    $selection = Read-DesktopSelection
    Read-GlobalPetIntent
    if ($running -and $null -ne $guiPid) {
        try {
            $processStarted = (Get-Process -Id $guiPid -ErrorAction Stop).StartTime.ToUniversalTime()
            $appStartedAt = ([DateTimeOffset]$processStarted).ToUnixTimeSeconds()
        } catch { Write-ObserverError 'app-start-time' $_.Exception }
    }
    $payload = [ordered]@{
        running = [bool]$running
        overlayOpen = [bool]$overlayOpen
        requestedOpen = $requestedOpen
        selectionKnown = [bool]$selection.selectionKnown
        selectedAvatarId = $selection.selectedAvatarId
        nativePetVisible = $selection.nativePetVisible
        fullBailongMigratedIds = @($fullBailongMigratedIds)
        toolbarRect = $toolbarRect
        petRect = $petRect
        overlayHwnd = $(if ($null -ne $overlayHandle) { $overlayHandle.ToInt64() } else { $null })
        overlayWindowRect = $overlayWindowRect
        dpi = $dpi
        pid = $guiPid
        appStartedAt = $appStartedAt
        identityProbe = Get-IdentityProbeDiagnostic
    }
    $comparison = ConvertTo-Json -InputObject $payload -Depth 4 -Compress
    $now = [DateTime]::UtcNow
    if ($comparison -ne $lastPayload -or ($now - $lastWrittenAt).TotalSeconds -ge 5) {
        $payload['observedAt'] = $now.ToString('o')
        $content = ConvertTo-Json -InputObject $payload -Depth 4 -Compress
        if (Write-AtomicState $content) { $lastPayload = $comparison; $lastWrittenAt = $now }
    }
    if ($Once) { break }
    $delay = [Math]::Max(25, $IntervalMs - ([DateTime]::UtcNow - $startedAt).TotalMilliseconds)
    Start-Sleep -Milliseconds ([int]$delay)
} while ($true)
Close-IdentityProbe 'observer-stopped'
