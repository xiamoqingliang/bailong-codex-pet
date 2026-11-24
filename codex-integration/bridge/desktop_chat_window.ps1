$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient,UIAutomationTypes,PresentationFramework
Add-Type -Namespace Bailong -Name DesktopChatNative -MemberDefinition @'
[DllImport("user32.dll")] public static extern uint GetClipboardSequenceNumber();
[DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
[DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr window, out uint processId);
'@

$automation = [Windows.Automation.AutomationElement]
$control = [Windows.Automation.ControlType]
$descendants = [Windows.Automation.TreeScope]::Descendants
$expand = [Windows.Automation.ExpandCollapsePattern]::Pattern
$invoke = [Windows.Automation.InvokePattern]::Pattern
$value = [Windows.Automation.ValuePattern]::Pattern
$walker = [Windows.Automation.TreeWalker]::ControlViewWalker
$actions = @('聊天操作', '聊天動作', 'Chat actions', 'Conversation actions', 'Thread actions')
$copyLabels = @('复制', '複製', 'Copy')
$linkLabels = @('复制深度链接', '複製深層連結', 'Copy deep link', 'Copy chat deep link')
$sendLabels = @('发送', '发送消息', '傳送', '傳送訊息', 'Send', 'Send message', 'Send prompt')
$stopLabels = @('停止', '停止生成', '停止回應', 'Stop', 'Stop generating', 'Stop response')

function Publish([string]$Message) {
    [Console]::Out.WriteLine($Message)
    [Console]::Out.Flush()
}

function Find-Controls($Parent, $Type, $Names, [string]$Class = '') {
    $condition = [Windows.Automation.PropertyCondition]::new($automation::ControlTypeProperty, $Type)
    foreach ($element in $Parent.FindAll($descendants, $condition)) {
        $current = $element.Current
        if (!$current.IsOffscreen -and ($null -eq $Names -or $current.Name -in $Names) -and
            (!$Class -or ($current.ClassName -split '\s+') -ccontains $Class)) {
            $element
        }
    }
}

function Wait-Control($Parent, $Type, $Names) {
    $timer = [Diagnostics.Stopwatch]::StartNew()
    do {
        $matches = @(Find-Controls $Parent $Type $Names)
        if ($matches.Count -eq 1 -and $matches[0].Current.IsEnabled) { return $matches[0] }
        Start-Sleep -Milliseconds 55
    } while ($timer.ElapsedMilliseconds -lt 1000)
    throw 'chat'
}

function Root-Window($Request) {
    try { $desktop = Get-Process -Id ([int]$Request.pid) } catch { throw 'window' }
    $desktop.Refresh()
    $startedAt = ([DateTimeOffset]$desktop.StartTime).ToUnixTimeSeconds()
    if ($startedAt -ne $Request.startedAt -or $desktop.MainWindowHandle -eq [IntPtr]::Zero) { throw 'window' }
    $handle = $desktop.MainWindowHandle
    $foreground = [Bailong.DesktopChatNative]::GetForegroundWindow()
    [uint32]$foregroundPid = 0
    $null = [Bailong.DesktopChatNative]::GetWindowThreadProcessId($foreground, [ref]$foregroundPid)
    if ($foregroundPid -eq $desktop.Id) { $handle = $foreground }
    $automation::FromHandle($handle)
}

function Copy-ChatLink($Button) {
    # Save native formats before asking the app to copy. Restore only the clipboard write
    # we observed, so a user's concurrent copy operation wins.
    $before = [Bailong.DesktopChatNative]::GetClipboardSequenceNumber()
    try {
        $original = [Windows.Clipboard]::GetDataObject()
        $snapshot = [Windows.DataObject]::new()
        $formats = if ($null -ne $original) { @($original.GetFormats($false)) } else { @() }
        foreach ($format in $formats) {
            $data = $original.GetData($format, $false)
            if ($null -eq $data) { throw 'clipboard' }
            $snapshot.SetData($format, $data, $false)
        }
    } catch { throw 'clipboard' }
    if ([Bailong.DesktopChatNative]::GetClipboardSequenceNumber() -ne $before) { throw 'clipboard' }
    $copied = $null
    try {
        $Button.GetCurrentPattern($invoke).Invoke()
        $timer = [Diagnostics.Stopwatch]::StartNew()
        do {
            $sequence = [Bailong.DesktopChatNative]::GetClipboardSequenceNumber()
            if ($sequence -ne $before) {
                $link = [Windows.Clipboard]::GetText()
                if ($sequence -ne [Bailong.DesktopChatNative]::GetClipboardSequenceNumber()) { throw 'clipboard' }
                if ($link -notmatch '^codex://threads/[0-9a-f-]{36}(?:\?[^#\s]*)?$') { throw 'clipboard' }
                $copied = $sequence
                return $link
            }
            Start-Sleep -Milliseconds 30
        } while ($timer.ElapsedMilliseconds -lt 950)
        throw 'clipboard'
    } finally {
        if ($null -ne $copied -and $copied -eq [Bailong.DesktopChatNative]::GetClipboardSequenceNumber()) {
            if ($formats.Count) { [Windows.Clipboard]::SetDataObject($snapshot, $true) }
            else { [Windows.Clipboard]::Clear() }
        }
    }
}

function Check-Chat($Root, [string]$ThreadId) {
    $header = @(Find-Controls $Root $control::Button $actions 'conversation-header-control')
    if ($header.Count -ne 1 -or !$header[0].Current.IsEnabled) { throw 'chat' }
    $menu = $header[0].GetCurrentPattern($expand)
    if ($menu.Current.ExpandCollapseState -ne [Windows.Automation.ExpandCollapseState]::Collapsed) { throw 'chat' }
    $menu.Expand()
    try {
        $actionsMenu = Wait-Control $Root $control::Menu @($header[0].Current.Name)
        $copy = Wait-Control $actionsMenu $control::MenuItem $copyLabels
        $copy.GetCurrentPattern($expand).Expand()
        $copyMenu = Wait-Control $Root $control::Menu @($copy.Current.Name)
        $link = Copy-ChatLink (Wait-Control $copyMenu $control::MenuItem $linkLabels)
        if ($link -ine "codex://threads/$ThreadId" -and $link -ine "codex://threads/$($ThreadId)?hostId=local") {
            throw 'wrong-chat'
        }
    } finally {
        try {
            if ($menu.Current.ExpandCollapseState -eq [Windows.Automation.ExpandCollapseState]::Expanded) { $menu.Collapse() }
        } catch { }
    }
}

function Check-StagedChat($Root, [string]$ThreadId) {
    # The previous menu can still be closing when Chromium publishes the new draft.
    $timer = [Diagnostics.Stopwatch]::StartNew()
    do {
        try {
            Check-Chat $Root $ThreadId
            return
        } catch {
            if ($_.Exception.Message -ne 'chat' -or $timer.ElapsedMilliseconds -ge 1500) { throw }
            Start-Sleep -Milliseconds 90
        }
    } while ($true)
}

function Composer($Root) {
    $matches = @(Find-Controls $Root $control::Edit $null 'ProseMirror')
    if ($matches.Count -ne 1 -or !$matches[0].Current.IsEnabled) { throw 'editor' }
    $matches[0]
}

function Composer-Button($Editor, $Names) {
    $parent = $Editor
    for ($depth = 0; $depth -lt 7; $depth++) {
        $parent = $walker.GetParent($parent)
        if ($null -eq $parent) { return }
        $matches = @(Find-Controls $parent $control::Button $Names 'size-token-button-composer')
        if ($matches.Count -eq 1) { return $matches[0] }
        if ($matches.Count -gt 1) { throw 'editor' }
    }
}

function Normalize([string]$Text) { ($Text -replace '\r\n?', [char]10).Trim() }

function Check-Empty($Editor) {
    if (Composer-Button $Editor $stopLabels) { throw 'busy' }
    $pattern = $Editor.GetCurrentPattern($value)
    if ($pattern.Current.IsReadOnly) { throw 'editor' }
    $draft = Normalize $pattern.Current.Value
    if (($draft -and $draft -cne (Normalize $Editor.Current.Name)) -or (Composer-Button $Editor $sendLabels)) {
        throw 'draft'
    }
    $pattern
}

$invoked = $false
$staged = $false
$editorValue = $null
try {
    $request = [Console]::ReadLine() | ConvertFrom-Json
    if ($request.operation -notin @('send', 'stop') -or
        $request.threadId -notmatch '^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$') { throw 'chat' }
    $timer = [Diagnostics.Stopwatch]::StartNew()
    while ($true) {
        try {
            $root = Root-Window $request
            $editor = Composer $root
            Check-Chat $root $request.threadId
            $editor = Composer $root
            break
        } catch {
            if ($_.Exception.Message -eq 'clipboard' -or $timer.ElapsedMilliseconds -ge 8000) { throw }
            Start-Sleep -Milliseconds 100
        }
    }
    $editorId = $editor.GetRuntimeId() -join ':'
    if ($request.operation -eq 'stop') {
        $button = Composer-Button $editor $stopLabels
        if (!$button -or !$button.Current.IsEnabled) { throw 'busy' }
        $buttonId = $button.GetRuntimeId() -join ':'
    } else {
        $editorValue = Check-Empty $editor
    }

    Publish 'ready'
    if ([Console]::ReadLine() -cne 'commit') { return }

    if (((Composer $root).GetRuntimeId() -join ':') -ne $editorId) { throw 'chat' }
    if ($request.operation -eq 'stop') {
        $button = Composer-Button $editor $stopLabels
        if (!$button -or !$button.Current.IsEnabled -or ($button.GetRuntimeId() -join ':') -ne $buttonId) { throw 'busy' }
    } else {
        $editorValue = Check-Empty $editor
        $staged = $true
        $editorValue.SetValue([string]$request.prompt)
        Check-StagedChat $root $request.threadId
        $editor = Composer $root
        $editorId = $editor.GetRuntimeId() -join ':'
        $editorValue = $editor.GetCurrentPattern($value)
        if ((Normalize $editorValue.Current.Value) -cne (Normalize $request.prompt)) { throw 'editor' }
        if (Composer-Button $editor $stopLabels) { throw 'busy' }
        $button = Composer-Button $editor $sendLabels
        if (!$button -or !$button.Current.IsEnabled) { throw 'editor' }
    }
    $action = $button.GetCurrentPattern($invoke)
    $invoked = $true
    $action.Invoke()
    Publish 'invoked'
} catch {
    if ($invoked) { Publish 'unknown' }
    else {
        $reason = $_.Exception.Message
        if ($reason -eq 'wrong-chat') {
            $staged = $false
            $reason = 'chat'
        }
        if ($reason -notin @('window', 'chat', 'draft', 'busy', 'editor', 'clipboard')) { $reason = 'editor' }
        Publish "reject:$reason"
    }
} finally {
    if ($staged -and !$invoked) {
        try {
            if (((Composer $root).GetRuntimeId() -join ':') -eq $editorId -and
                (Normalize $editorValue.Current.Value) -ceq (Normalize $request.prompt)) {
                $editorValue.SetValue('')
            }
        } catch { }
    }
}
