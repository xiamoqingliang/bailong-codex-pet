# Invoke an existing native Mini toggle without navigating the application.
# Show may use its verified default global shortcut when no button is present.
# Hide first invokes the verified Mini's own menu, retaining its warm renderer.
# Only a failure before menu action delivery may fall back to verified WM_CLOSE.
# -Pid is an alias because PowerShell's automatic $PID variable is read-only.
[CmdletBinding()]
param(
    [string] $Action = '',
    [Alias('Pid')]
    [int] $TargetProcessId = 0,
    [long] $OverlayHwnd = 0,
    [AllowEmptyString()]
    [string] $ExpectedSelectedAvatarId = '',
    [string] $ExpectedNativePetVisible = '',
    [string] $PythonPath = '',
    [string] $ConfigPath = '',
    [switch] $OnlyWhenPersistedOpen
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$result = [ordered]@{
    action = $Action
    targetPid = $TargetProcessId
    overlayHwnd = $OverlayHwnd
    acted = $false
    method = 'none'
    reason = $null
    globalOpenMayRemain = $false
    scrolledIntoView = $false
    mainHwnd = $null
    controlName = $null
}

function Write-Result {
    param([string] $Reason)
    $result.reason = $Reason
    $json = $result | ConvertTo-Json -Depth 3 -Compress
    # Windows PowerShell 5.1 may use an OEM code page for redirected stdout.
    # Emit ASCII JSON escapes so the supervisor can always decode UTF-8.
    [regex]::Replace($json, '[^\x00-\x7F]', {
        param($match)
        '\u{0:x4}' -f [int][char]$match.Value
    })
}

# These checks precede native API loading and any UI access.
if ($Action -cnotin @('Hide', 'Show')) {
    Write-Result 'invalid-action'
    exit 2
}
if ($TargetProcessId -le 0) {
    Write-Result 'invalid-pid'
    exit 2
}
if ($OverlayHwnd -lt 0) {
    Write-Result 'invalid-overlay-hwnd'
    exit 2
}
if ($ExpectedNativePetVisible -cnotin @('true', 'false')) {
    Write-Result 'missing-or-invalid-expected-selection'
    exit 2
}
if ($Action -eq 'Hide' -and (
    [string]::IsNullOrEmpty($ExpectedSelectedAvatarId) -or
    $ExpectedNativePetVisible -cne 'true')) {
    Write-Result 'hide-requires-enabled-pet-selection'
    exit 2
}
if ([string]::IsNullOrWhiteSpace($PythonPath) -or
    -not [System.IO.Path]::IsPathRooted($PythonPath) -or
    -not [System.IO.File]::Exists($PythonPath)) {
    Write-Result 'invalid-python-path'
    exit 2
}
if ([string]::IsNullOrEmpty($ConfigPath)) {
    $ConfigPath = Join-Path ([Environment]::GetFolderPath('UserProfile')) '.codex\config.toml'
}
if (-not [System.IO.Path]::IsPathRooted($ConfigPath) -or
    $ConfigPath.IndexOfAny([char[]] @('"', "`r", "`n")) -ge 0 -or
    $PSScriptRoot.IndexOfAny([char[]] @('"', "`r", "`n")) -ge 0) {
    Write-Result 'invalid-config-path'
    exit 2
}

function Test-ExpectedSelection {
    # The caller determines full_mode_selected using its verified migration
    # mapping. This fresh TOML projection prevents acting on that decision after
    # the user has selected another pet or changed the visibility setting.
    $process = $null
    try {
        if ($OnlyWhenPersistedOpen) {
            $intentPath = Join-Path (Split-Path -Parent $ConfigPath) '.codex-global-state.json'
            $intent = [IO.File]::ReadAllText($intentPath) | ConvertFrom-Json -ErrorAction Stop
            if ($intent.'electron-avatar-overlay-open' -isnot [bool] -or
                -not $intent.'electron-avatar-overlay-open') { return $false }
        }
        $policyPath = Join-Path $PSScriptRoot 'bridge_policy.py'
        if (-not [System.IO.File]::Exists($policyPath)) { return $false }
        $start = New-Object System.Diagnostics.ProcessStartInfo
        $start.FileName = $PythonPath
        $start.Arguments = '-B "' + $policyPath + '" --selection "' + $ConfigPath + '"'
        $start.UseShellExecute = $false
        $start.CreateNoWindow = $true
        $start.RedirectStandardOutput = $true
        $start.RedirectStandardError = $true
        $process = New-Object System.Diagnostics.Process
        $process.StartInfo = $start
        if (-not $process.Start()) { return $false }
        if (-not $process.WaitForExit(4000)) {
            # Only our own bounded, read-only parser process is terminated.
            try { $process.Kill() } catch { }
            return $false
        }
        $raw = $process.StandardOutput.ReadToEnd()
        if ($process.ExitCode -ne 0 -or $raw.Length -gt 4096) { return $false }
        $selection = $raw | ConvertFrom-Json
        if ($selection.selectionKnown -isnot [bool] -or -not $selection.selectionKnown) { return $false }
        if ($selection.nativePetVisible -isnot [bool]) { return $false }
        if ($selection.nativePetVisible -ne ($ExpectedNativePetVisible -ceq 'true')) { return $false }
        if ([string]::IsNullOrEmpty($ExpectedSelectedAvatarId)) {
            return $null -eq $selection.selectedAvatarId
        }
        return ($selection.selectedAvatarId -is [string] -and
            $selection.selectedAvatarId -ceq $ExpectedSelectedAvatarId)
    } catch {
        return $false
    } finally {
        if ($null -ne $process) { $process.Dispose() }
    }
}

if (-not (Test-ExpectedSelection)) {
    Write-Result 'selection-changed-or-unavailable'
    exit 0
}

try {
    Add-Type -AssemblyName UIAutomationClient
    Add-Type -AssemblyName UIAutomationTypes
    Add-Type -Path (Join-Path $PSScriptRoot 'native_menu_shield.cs')
    Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;

public static class BailongMiniNativeControl {
    private delegate bool EnumWindowsCallback(IntPtr hwnd, IntPtr parameter);
    [DllImport("user32.dll")]
    private static extern bool EnumWindows(EnumWindowsCallback callback, IntPtr parameter);
    [DllImport("user32.dll")]
    private static extern bool EnumChildWindows(IntPtr parent, EnumWindowsCallback callback, IntPtr parameter);
    [DllImport("user32.dll")]
    public static extern bool IsChild(IntPtr parent, IntPtr child);
    [StructLayout(LayoutKind.Sequential)]
    public struct Point { public int x, y; }
    [DllImport("user32.dll")]
    public static extern bool ScreenToClient(IntPtr hwnd, ref Point point);
    [DllImport("user32.dll")]
    public static extern bool IsWindow(IntPtr hwnd);
    [DllImport("user32.dll")]
    public static extern bool IsWindowVisible(IntPtr hwnd);
    [DllImport("user32.dll")]
    public static extern bool IsIconic(IntPtr hwnd);
    [DllImport("user32.dll")]
    public static extern IntPtr GetWindow(IntPtr hwnd, uint command);
    [DllImport("user32.dll")]
    private static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint processId);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern int GetClassName(IntPtr hwnd, StringBuilder text, int capacity);
    [DllImport("user32.dll", EntryPoint = "GetWindowLongPtrW")]
    private static extern IntPtr GetWindowLongPtr64(IntPtr hwnd, int index);
    [DllImport("user32.dll", EntryPoint = "GetWindowLongW")]
    private static extern int GetWindowLong32(IntPtr hwnd, int index);
    [DllImport("user32.dll", SetLastError = true)]
    public static extern bool PostMessage(IntPtr hwnd, uint message, IntPtr wparam, IntPtr lparam);
    [DllImport("user32.dll")]
    public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")]
    public static extern bool SetForegroundWindow(IntPtr hwnd);
    [DllImport("user32.dll")]
    private static extern short GetAsyncKeyState(int key);
    [StructLayout(LayoutKind.Sequential)]
    private struct KeyboardInput {
        public ushort key, scan;
        public uint flags, time;
        public UIntPtr extra;
    }
    [StructLayout(LayoutKind.Sequential)]
    private struct MouseInput {
        public int x, y;
        public uint data, flags, time;
        public UIntPtr extra;
    }
    [StructLayout(LayoutKind.Explicit)]
    private struct InputUnion {
        [FieldOffset(0)] public KeyboardInput keyboard;
        [FieldOffset(0)] public MouseInput mouse;
    }
    [StructLayout(LayoutKind.Sequential)]
    private struct Input {
        public uint type;
        public InputUnion data;
    }
    [DllImport("user32.dll", SetLastError = true)]
    private static extern uint SendInput(uint count, [In] Input[] values, int size);
    private static Input Key(ushort key, bool release) {
        var value = new Input { type = 1 };
        value.data.keyboard = new KeyboardInput { key = key, flags = release ? 2U : 0U };
        return value;
    }
    public static bool ShortcutKeysAreUp() {
        foreach (int key in new int[] { 0x10, 0x11, 0x12, 0x5B, 0x5C, 0x50 })
            if ((GetAsyncKeyState(key) & 0x8000) != 0) return false;
        return true;
    }
    public static bool PressDefaultMiniHotkey() {
        if (!ShortcutKeysAreUp()) return false;
        var values = new Input[] { Key(0x5B, false), Key(0x12, false), Key(0x50, false),
                                   Key(0x50, true), Key(0x12, true), Key(0x5B, true) };
        try {
            return SendInput((uint)values.Length, values, Marshal.SizeOf(typeof(Input))) == values.Length;
        } finally {
            // Also release every injected key after a partial SendInput failure.
            var release = new Input[] { Key(0x50, true), Key(0x12, true), Key(0x5B, true) };
            SendInput((uint)release.Length, release, Marshal.SizeOf(typeof(Input)));
        }
    }

    public static uint ProcessId(IntPtr hwnd) {
        uint processId;
        GetWindowThreadProcessId(hwnd, out processId);
        return processId;
    }
    public static string ClassName(IntPtr hwnd) {
        var text = new StringBuilder(256);
        GetClassName(hwnd, text, text.Capacity);
        return text.ToString();
    }
    public static long ExtendedStyle(IntPtr hwnd) {
        return IntPtr.Size == 8 ? GetWindowLongPtr64(hwnd, -20).ToInt64()
                               : GetWindowLong32(hwnd, -20);
    }
    public static long[] TopLevelWindows(int processId) {
        var handles = new List<long>();
        EnumWindows((hwnd, ignored) => {
            if (ProcessId(hwnd) == (uint) processId) handles.Add(hwnd.ToInt64());
            return true;
        }, IntPtr.Zero);
        return handles.ToArray();
    }
    public static long[] RendererChildren(IntPtr parent, int processId) {
        var handles = new List<long>();
        EnumChildWindows(parent, (hwnd, ignored) => {
            if (ProcessId(hwnd) == (uint)processId && ClassName(hwnd) == "Chrome_RenderWidgetHostHWND")
                handles.Add(hwnd.ToInt64());
            return true;
        }, IntPtr.Zero);
        return handles.ToArray();
    }
}
'@

    function Test-MainWindow {
        param([IntPtr] $Handle, [switch] $MustBeVisible)
        if (-not [BailongMiniNativeControl]::IsWindow($Handle)) { return $false }
        if ([BailongMiniNativeControl]::ProcessId($Handle) -ne $TargetProcessId) { return $false }
        if ([BailongMiniNativeControl]::ClassName($Handle) -cne 'Chrome_WidgetWin_1') { return $false }
        # WS_EX_TOOLWINDOW excludes the overlay and tool panels; an unowned
        # top-level Chromium window is the only main-window candidate.
        if (([BailongMiniNativeControl]::ExtendedStyle($Handle) -band 0x80) -ne 0) { return $false }
        if ([BailongMiniNativeControl]::GetWindow($Handle, 4) -ne [IntPtr]::Zero) { return $false }
        if ($MustBeVisible -and (
            -not [BailongMiniNativeControl]::IsWindowVisible($Handle) -or
            [BailongMiniNativeControl]::IsIconic($Handle))) { return $false }
        return $true
    }

    function Invoke-MiniContextHide {
        param([IntPtr] $Handle)
        $menuResult = @{ acted = $false; uncertain = $false; reason = 'mini-context-target-unavailable' }
        $renderer = [IntPtr]::Zero
        $menuSent = $false
        $invokeAttempted = $false
        $menuShield = $null
        try {
            $children = @([BailongMiniNativeControl]::RendererChildren($Handle, $TargetProcessId))
            if ($children.Count -ne 1) { return $menuResult }
            $renderer = [IntPtr] $children[0]
            $menuShield = New-Object BailongNativeMenuShield($Handle, $TargetProcessId, $renderer)
            $root = [System.Windows.Automation.AutomationElement]::FromHandle($Handle)
            $imageCondition = New-Object System.Windows.Automation.PropertyCondition(
                [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
                [System.Windows.Automation.ControlType]::Image)
            # Only the dedicated full-entry mascot can be right-clicked. Never
            # use a chat card, input, arbitrary image, or global screen position.
            $mascots = @($root.FindAll([System.Windows.Automation.TreeScope]::Descendants, $imageCondition) |
                Where-Object { $_.Current.Name -cin @('白龙完整版 虚拟宠物', '白龙完整版 pet',
                    '白龙完整版 Pet', '白龙完整版 virtual pet') })
            if ($mascots.Count -ne 1) { return $menuResult }
            $bounds = $mascots[0].Current.BoundingRectangle
            if ($bounds.IsEmpty -or $bounds.Width -le 0 -or $bounds.Height -le 0) { return $menuResult }
            if (-not (Test-ExpectedSelection) -or
                -not [BailongMiniNativeControl]::IsWindowVisible($Handle) -or
                -not [BailongMiniNativeControl]::IsChild($Handle, $renderer) -or
                [BailongMiniNativeControl]::ProcessId($renderer) -ne $TargetProcessId) {
                $menuResult.reason = 'mini-context-preconditions-changed'
                return $menuResult
            }
            $point = New-Object BailongMiniNativeControl+Point
            $point.x = [int]($bounds.X + $bounds.Width / 2)
            $point.y = [int]($bounds.Y + $bounds.Height / 2)
            if (-not [BailongMiniNativeControl]::ScreenToClient($renderer, [ref] $point)) { return $menuResult }
            $packed = ([long]($point.x -band 65535)) -bor (([long]($point.y -band 65535)) -shl 16)
            # Address only the verified renderer. This does not move the mouse,
            # activate a window, inject global keys, or expose the masked Mini.
            $menuSent = [BailongMiniNativeControl]::PostMessage($renderer, 0x0204, [IntPtr]2, [IntPtr]$packed)
            $released = [BailongMiniNativeControl]::PostMessage($renderer, 0x0205, [IntPtr]0, [IntPtr]$packed)
            if (-not $menuSent -or -not $released) { return $menuResult }
            $menuCondition = New-Object System.Windows.Automation.PropertyCondition(
                [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
                [System.Windows.Automation.ControlType]::MenuItem)
            $deadline = [DateTime]::UtcNow.AddMilliseconds(1200)
            do {
                $items = $root.FindAll([System.Windows.Automation.TreeScope]::Descendants, $menuCondition)
                foreach ($item in $items) {
                    $current = $item.Current
                    if (-not $current.IsEnabled -or $current.Name -cnotin @('隐藏 Mini', '隐藏Mini',
                        '隐藏', '关闭 Mini', '关闭Mini', 'Hide Mini', 'Hide mini', 'Hide', 'Close Mini', 'Close mini')) { continue }
                    $invoke = $null
                    if (-not $item.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern,
                        [ref] $invoke)) { continue }
                    if (-not (Test-ExpectedSelection) -or
                        -not [BailongMiniNativeControl]::IsWindow($Handle) -or
                        [BailongMiniNativeControl]::ProcessId($Handle) -ne $TargetProcessId -or
                        -not [BailongMiniNativeControl]::IsChild($Handle, $renderer)) {
                        $menuResult.reason = 'mini-context-preconditions-changed'
                        return $menuResult
                    }
                    # Invocation can be delivered even if UIA then throws.
                    # Mark that boundary before calling and never send WM_CLOSE
                    # or retry the action after an uncertain delivery.
                    $invokeAttempted = $true
                    ([System.Windows.Automation.InvokePattern]$invoke).Invoke()
                    $menuResult.acted = $true
                    $menuResult.reason = 'native-menu-hide-invoked'
                    return $menuResult
                }
                Start-Sleep -Milliseconds 40
            } while ([DateTime]::UtcNow -lt $deadline)
            $menuResult.reason = 'mini-context-hide-not-found'
        } catch {
            $menuResult.uncertain = $invokeAttempted
            $menuResult.reason = if ($invokeAttempted) { 'mini-context-invoke-uncertain' } else { 'mini-context-unavailable' }
        } finally {
            if ($menuSent -and -not $menuResult.acted -and
                [BailongMiniNativeControl]::IsChild($Handle, $renderer) -and
                [BailongMiniNativeControl]::ProcessId($renderer) -eq $TargetProcessId) {
                [void][BailongMiniNativeControl]::PostMessage($renderer, 0x0100, [IntPtr]0x1B, [IntPtr]1)
                [void][BailongMiniNativeControl]::PostMessage($renderer, 0x0101, [IntPtr]0x1B, [IntPtr]1)
            }
            if ($null -ne $menuShield) {
                $menuShield.Dispose()
                $result.menuShield = $menuShield.Snapshot()
            }
        }
        return $menuResult
    }

    $mainHandles = @([BailongMiniNativeControl]::TopLevelWindows($TargetProcessId) |
        Where-Object { Test-MainWindow ([IntPtr] $_) })
    if ($OverlayHwnd -gt 0) {
        # A previous one-shot helper may have been cancelled. Its process/start
        # token must be dead before any shielded popup is eligible for recovery.
        $result.recoveredMenuShields = [BailongNativeMenuShield]::RecoverAbandoned(
            [IntPtr]$OverlayHwnd, $TargetProcessId, [IntPtr]::Zero)
    }

    # Exact observer-verified HWND operations and the verified native shortcut
    # precede UIA. Electron's full accessibility-tree walk can block for tens
    # of seconds even while the Mini itself responds immediately.
    $keybindingsPath = Join-Path (Split-Path -Parent $ConfigPath) 'keybindings.json'
    if ($Action -eq 'Hide' -and $OverlayHwnd -gt 0) {
    $overlay = [IntPtr] $OverlayHwnd
    $rejection = $null
    if (-not [BailongMiniNativeControl]::IsWindow($overlay)) {
        $rejection = 'overlay-window-missing'
    } elseif ([BailongMiniNativeControl]::ProcessId($overlay) -ne $TargetProcessId) {
        $rejection = 'overlay-pid-mismatch'
    } elseif ([BailongMiniNativeControl]::ClassName($overlay) -cne 'Chrome_WidgetWin_1') {
        $rejection = 'overlay-class-mismatch'
    } elseif (([BailongMiniNativeControl]::ExtendedStyle($overlay) -band 0x88) -ne 0x88) {
        $rejection = 'overlay-style-mismatch'
    } elseif ($mainHandles -contains $OverlayHwnd) {
        $rejection = 'overlay-is-main-window'
    } elseif (@($mainHandles | Where-Object {
        $_ -ne $OverlayHwnd -and (Test-MainWindow ([IntPtr] $_))
    }).Count -eq 0) {
        $rejection = 'no-separate-main-window'
    } elseif (-not [BailongMiniNativeControl]::IsWindowVisible($overlay)) {
        $rejection = 'overlay-already-hidden'
    }
    if ($null -ne $rejection) {
        Write-Result $rejection
        exit 0
    }

    if (-not (Test-ExpectedSelection)) {
        Write-Result 'selection-changed-or-unavailable'
        exit 0
    }
    $menuResult = Invoke-MiniContextHide $overlay
    if ($menuResult.acted -or $menuResult.uncertain) {
        $result.method = 'mini-context-menu'
        $result.acted = $menuResult.acted
        Write-Result $menuResult.reason
        exit 0
    }
    # A target/menu unavailable before action delivery may use the old fallback.
    # WM_CLOSE destroys the renderer and is deliberately not the normal Hide.
    $result.contextFallbackReason = $menuResult.reason
    if (-not (Test-ExpectedSelection)) {
        Write-Result 'selection-changed-or-unavailable'
        exit 0
    }
    if (-not [BailongMiniNativeControl]::IsWindow($overlay) -or
        [BailongMiniNativeControl]::ProcessId($overlay) -ne $TargetProcessId -or
        [BailongMiniNativeControl]::ClassName($overlay) -cne 'Chrome_WidgetWin_1' -or
        ([BailongMiniNativeControl]::ExtendedStyle($overlay) -band 0x88) -ne 0x88) {
        Write-Result 'overlay-changed-before-close'
        exit 0
    }
    $result.method = 'close-overlay'
    $result.globalOpenMayRemain = $true
    $result.acted = [BailongMiniNativeControl]::PostMessage(
        $overlay, 0x0010, [IntPtr]::Zero, [IntPtr]::Zero)
    if ($result.acted) {
        Write-Result 'overlay-close-posted'
    } else {
        Write-Result 'overlay-close-post-failed'
    }
        exit 0
    }

    if ($Action -eq 'Show' -and -not (Test-Path -LiteralPath $keybindingsPath)) {
        if ($mainHandles.Count -eq 0) {
            # A cold host can be observed before its UI/hotkey registration.
            # No input has been sent; the controller may retry readiness.
            Write-Result 'main-window-not-ready'
            exit 0
        }
        # The verified Windows default openAvatarOverlay accelerator is
        # Super+Alt+P. It is suitable only for a currently closed full-pet Mini:
        # an already open Mini would focus/toggle Quick Chat instead of Show.
        function Test-CurrentCodexProcess {
            try {
                $process = Get-Process -Id $TargetProcessId -ErrorAction Stop
                return ($process.Path -cmatch '\\WindowsApps\\OpenAI\.Codex_[^\\]+\\app\\ChatGPT\.exe$' -and
                        -not $process.HasExited)
            } catch { return $false }
        }
        function Get-NativeMiniCandidates {
            @([BailongMiniNativeControl]::TopLevelWindows($TargetProcessId) | Where-Object {
                $candidate = [IntPtr] $_
                [BailongMiniNativeControl]::IsWindow($candidate) -and
                [BailongMiniNativeControl]::ProcessId($candidate) -eq $TargetProcessId -and
                [BailongMiniNativeControl]::ClassName($candidate) -ceq 'Chrome_WidgetWin_1' -and
                ([BailongMiniNativeControl]::ExtendedStyle($candidate) -band 0x88) -eq 0x88
            })
        }
        function Test-NoVisibleMini {
            foreach ($candidate in @(Get-NativeMiniCandidates)) {
                if ([BailongMiniNativeControl]::IsWindowVisible([IntPtr] $candidate)) { return $false }
            }
            return $true
        }
        function Test-FullPetHotkeySelection {
            if ($ExpectedNativePetVisible -cne 'true' -or
                $ExpectedSelectedAvatarId -cnotmatch '^(custom:bailong-full|pet_[A-Za-z0-9_-]+)$') { return $false }
            try {
                $config = [IO.File]::ReadAllText((Join-Path $PSScriptRoot 'bridge-config.json')) | ConvertFrom-Json
                if (@($config.fullAvatarIds) -ccontains $ExpectedSelectedAvatarId) { return $true }
                if (@($config.fullAvatarIds) -cnotcontains 'custom:bailong-full') { return $false }
                $statePath = Join-Path (Split-Path -Parent $ConfigPath) '.codex-global-state.json'
                $state = [IO.File]::ReadAllText($statePath) | ConvertFrom-Json
                $migrations = $state.'electron-persisted-atom-state'.'migrated-cloud-pet-ids-v1'
                foreach ($account in $migrations.PSObject.Properties) {
                    if ($account.Value.'custom:bailong-full' -ceq $ExpectedSelectedAvatarId) { return $true }
                }
            } catch { }
            return $false
        }
        $keybindingsPath = Join-Path (Split-Path -Parent $ConfigPath) 'keybindings.json'
        if (-not (Test-FullPetHotkeySelection)) {
            Write-Result 'hotkey-requires-full-pet-selection'
            exit 0
        }
        if (Test-Path -LiteralPath $keybindingsPath) {
            Write-Result 'custom-keybindings-disable-hotkey-fallback'
            exit 0
        }
        if (-not (Test-CurrentCodexProcess)) {
            Write-Result 'hotkey-target-process-unavailable'
            exit 0
        }
        if (-not (Test-NoVisibleMini)) {
            Write-Result 'native-mini-already-visible'
            exit 0
        }
        if (-not [BailongMiniNativeControl]::ShortcutKeysAreUp()) {
            Write-Result 'shortcut-keys-held'
            exit 0
        }
        $previousForeground = [BailongMiniNativeControl]::GetForegroundWindow()
        if (-not (Test-ExpectedSelection) -or -not (Test-FullPetHotkeySelection) -or
            (Test-Path -LiteralPath $keybindingsPath) -or -not (Test-CurrentCodexProcess) -or
            -not (Test-NoVisibleMini)) {
            Write-Result 'hotkey-preconditions-changed'
            exit 0
        }
        $result.method = 'global-hotkey'
        $result.shortcut = 'Win+Alt+P'
        $result.focusRestored = $false
        $result.acted = [BailongMiniNativeControl]::PressDefaultMiniHotkey()
        if (-not $result.acted) {
            Write-Result 'hotkey-input-not-delivered'
            exit 0
        }
        # The shortcut can focus Quick Chat. Restore the previous foreground
        # only while focus still belongs to the newly opened Mini; never steal
        # focus back after the user has selected another window.
        $seenMini = $false
        $focusDeadline = [DateTime]::UtcNow.AddMilliseconds(2500)
        while ([DateTime]::UtcNow -lt $focusDeadline -and (Test-CurrentCodexProcess)) {
            $foreground = [BailongMiniNativeControl]::GetForegroundWindow()
            $visibleMini = @(Get-NativeMiniCandidates | Where-Object {
                [BailongMiniNativeControl]::IsWindowVisible([IntPtr] $_)
            })
            if ($visibleMini.Count -gt 0) { $seenMini = $true }
            $confirmedMini = $OverlayHwnd
            if ($visibleMini.Count -eq 1 -and (Test-ExpectedSelection)) {
                # No Mini was visible before our verified Show shortcut. Its
                # sole resulting tool window is returned with the action ack;
                # the controller still validates PID/class/style before masking.
                $confirmedMini = [long]$visibleMini[0]
                $result['openedOverlayHwnd'] = $confirmedMini
            }
            # A newly created HWND is confirmed by the existing observer,
            # rather than treating another Codex tool panel as the Mini.
            try {
                $observerPath = Join-Path $PSScriptRoot 'codex-state.json'
                $observerInfo = Get-Item -LiteralPath $observerPath -ErrorAction Stop
                if ($observerInfo.Length -le 65536 -and
                    ([DateTime]::UtcNow - $observerInfo.LastWriteTimeUtc).TotalSeconds -le 6) {
                    $observer = [IO.File]::ReadAllText($observerPath) | ConvertFrom-Json
                    if ($observer.pid -eq $TargetProcessId -and
                        $observer.selectedAvatarId -ceq $ExpectedSelectedAvatarId -and
                        $observer.overlayHwnd -gt 0 -and $visibleMini -contains [long]$observer.overlayHwnd) {
                        $confirmedMini = [long] $observer.overlayHwnd
                    }
                }
            } catch { }
            if ($previousForeground -ne [IntPtr]::Zero -and
                [BailongMiniNativeControl]::IsWindow($previousForeground) -and
                $confirmedMini -gt 0 -and $foreground.ToInt64() -eq $confirmedMini -and
                $visibleMini -contains $confirmedMini) {
                if ([BailongMiniNativeControl]::GetForegroundWindow() -eq $foreground) {
                    $result.focusRestored = [BailongMiniNativeControl]::SetForegroundWindow($previousForeground)
                }
                if ($result.focusRestored) { break }
            }
            Start-Sleep -Milliseconds 40
        }
        Write-Result $(if ($seenMini) { 'native-hotkey-open-observed' } else { 'native-hotkey-sent-awaiting-window' })
        exit 0
    }
    # Only unusual configurations without the direct path reach UIA. The
    # owning controller bounds this one-shot process and cancels obsolete input.
    $names = if ($Action -eq 'Hide') {
        @('隐藏 Mini', '隐藏Mini', '隐藏 mini', '隐藏mini', '隐藏虚拟宠物',
          '隐藏宠物', 'Hide Mini', 'Hide mini', 'Hide Pet', 'Hide pet')
    } else {
        @('显示 Mini', '显示Mini', '显示 mini', '显示mini', '显示虚拟宠物',
          '显示宠物', 'Show Mini', 'Show mini', 'Show Pet', 'Show pet')
    }
    $nameConditions = [System.Windows.Automation.Condition[]] @(
        foreach ($name in $names) {
            New-Object System.Windows.Automation.PropertyCondition(
                [System.Windows.Automation.AutomationElement]::NameProperty, $name)
        }
    )
    $nameCondition = New-Object System.Windows.Automation.OrCondition(,$nameConditions)
    $buttonCondition = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
        [System.Windows.Automation.ControlType]::Button)
    $condition = New-Object System.Windows.Automation.AndCondition($buttonCondition, $nameCondition)

    foreach ($mainHandle in $mainHandles) {
        $handle = [IntPtr] $mainHandle
        if (-not (Test-MainWindow $handle -MustBeVisible)) { continue }
        try {
            $root = [System.Windows.Automation.AutomationElement]::FromHandle($handle)
            if ($null -eq $root) { continue }
            $buttons = $root.FindAll([System.Windows.Automation.TreeScope]::Descendants, $condition)
            foreach ($button in $buttons) {
                # Revalidate both the window and the button immediately before
                # invoking. FindAll alone may include off-screen settings pages.
                if (-not (Test-MainWindow $handle -MustBeVisible)) { break }
                $current = $button.Current
                if (-not $current.IsEnabled) { continue }
                if ($names -cnotcontains $current.Name) { continue }
                $bounds = $current.BoundingRectangle
                $windowBounds = $root.Current.BoundingRectangle
                if ($current.IsOffscreen -or $bounds.IsEmpty -or
                    -not $bounds.IntersectsWith($windowBounds)) {
                    # A settings header may be above the current scroll position.
                    # Scroll only this already-present, exactly named toggle;
                    # never navigate, synthesize keys, or set keyboard focus.
                    $scrollItem = $null
                    if (-not $button.TryGetCurrentPattern(
                        [System.Windows.Automation.ScrollItemPattern]::Pattern,
                        [ref] $scrollItem)) { continue }
                    if (-not (Test-ExpectedSelection)) {
                        Write-Result 'selection-changed-or-unavailable'
                        exit 0
                    }
                    if (-not (Test-MainWindow $handle -MustBeVisible)) { break }
                    $current = $button.Current
                    if (-not $current.IsEnabled -or
                        $names -cnotcontains $current.Name) { continue }
                    try {
                        ([System.Windows.Automation.ScrollItemPattern] $scrollItem).ScrollIntoView()
                        $result.scrolledIntoView = $true
                    } catch { continue }
                    # Allow the same existing control's geometry to settle.
                    # Visibility and ownership are still checked again below.
                    for ($scrollAttempt = 0; $scrollAttempt -lt 4; $scrollAttempt++) {
                        $current = $button.Current
                        $bounds = $current.BoundingRectangle
                        $windowBounds = $root.Current.BoundingRectangle
                        if (-not $current.IsOffscreen -and -not $bounds.IsEmpty -and
                            $bounds.IntersectsWith($windowBounds)) { break }
                        Start-Sleep -Milliseconds 50
                    }
                }
                if ($current.IsOffscreen -or -not $current.IsEnabled -or
                    $names -cnotcontains $current.Name) { continue }
                if ($bounds.IsEmpty -or $bounds.Width -le 0 -or $bounds.Height -le 0) { continue }
                if (-not $bounds.IntersectsWith($windowBounds)) { continue }
                $invoke = $null
                if (-not $button.TryGetCurrentPattern(
                    [System.Windows.Automation.InvokePattern]::Pattern, [ref] $invoke)) { continue }
                if (-not (Test-ExpectedSelection)) {
                    Write-Result 'selection-changed-or-unavailable'
                    exit 0
                }
                if (-not (Test-MainWindow $handle -MustBeVisible)) { break }
                $current = $button.Current
                if ($current.IsOffscreen -or -not $current.IsEnabled -or
                    $names -cnotcontains $current.Name) { continue }
                $bounds = $current.BoundingRectangle
                if ($bounds.IsEmpty -or $bounds.Width -le 0 -or $bounds.Height -le 0 -or
                    -not $bounds.IntersectsWith($root.Current.BoundingRectangle)) { continue }
                try {
                    ([System.Windows.Automation.InvokePattern] $invoke).Invoke()
                } catch {
                    # Invocation can have been delivered even if UIA reports an
                    # error. Do not try a second toggle or WM_CLOSE in that case.
                    $result.method = 'uia-invoke'
                    $result.mainHwnd = $mainHandle
                    $result.controlName = $current.Name
                    Write-Result 'invoke-failed-or-uncertain'
                    exit 1
                }
                $result.acted = $true
                $result.method = 'uia-invoke'
                $result.mainHwnd = $mainHandle
                $result.controlName = $current.Name
                Write-Result 'native-control-invoked'
                exit 0
            }
        } catch {
            # A closing/stale main window is not a reason to interact with some
            # unrelated window. Continue only with other verified candidates.
            continue
        }
    }

    Write-Result $(if ($Action -eq 'Show') {
        'custom-keybindings-and-no-visible-show-control'
    } else { 'no-verified-hide-target' })

} catch {
    $result.error = $_.Exception.Message
    Write-Result 'native-control-error'
    exit 1
}
