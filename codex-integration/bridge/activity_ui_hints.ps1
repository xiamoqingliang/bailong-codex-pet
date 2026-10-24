# Read only known task titles and fixed native state labels. Never open views.
[CmdletBinding()]
param([int]$TargetProcessId = 0)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
function Send-Result($value) {
    $json = $value | ConvertTo-Json -Depth 5 -Compress
    [regex]::Replace($json, '[^\x00-\x7F]', { param($m) '\u{0:x4}' -f [int][char]$m.Value })
}
try {
    if ($TargetProcessId -le 0) { throw 'Invalid process' }
    $query = [Console]::In.ReadToEnd() | ConvertFrom-Json
    $titles = @($query.titles | Where-Object { $_ -is [string] -and $_.Length -gt 0 -and $_.Length -le 160 } | Select-Object -Unique -First 128)
    if ($titles.Count -eq 0) { Send-Result @{available=$true;hints=@()}; exit 0 }
    $process = Get-Process -Id $TargetProcessId -ErrorAction Stop
    if ($process.Path -notmatch '\\OpenAI\.Codex_[^\\]+\\app\\ChatGPT\.exe$') { throw 'Wrong application' }
    Add-Type -AssemblyName UIAutomationClient
    Add-Type -AssemblyName UIAutomationTypes
    $root = [Windows.Automation.AutomationElement]::RootElement
    $pidCondition = New-Object Windows.Automation.PropertyCondition([Windows.Automation.AutomationElement]::ProcessIdProperty,$TargetProcessId)
    $buttonType = New-Object Windows.Automation.PropertyCondition([Windows.Automation.AutomationElement]::ControlTypeProperty,[Windows.Automation.ControlType]::Button)
    $labels = @(
        @{name=(-join [char[]]@(0x7B49,0x5F85,0x6279,0x51C6));state='needs_input'},
        @{name=(-join [char[]]@(0x9700,0x8981,0x7528,0x6237,0x8F93,0x5165));state='needs_input'},
        @{name='Waiting for approval';state='needs_input'},
        @{name='Needs user input';state='needs_input'},
        @{name=(-join [char[]]@(0x804A,0x5929,0x53D1,0x751F,0x7CFB,0x7EDF,0x9519,0x8BEF));state='failed'},
        @{name=(-join [char[]]@(0x5904,0x7406,0x4E2D));state='running'},
        @{name='Working';state='running'}
    )
    $hints = @{}
    foreach ($window in $root.FindAll([Windows.Automation.TreeScope]::Children,$pidCondition)) {
        $sidebar = $null
        foreach ($label in @((-join [char[]]@(0x67E5,0x770B,0x6D3B,0x52A8)),'View activity')) {
            $condition = New-Object Windows.Automation.PropertyCondition([Windows.Automation.AutomationElement]::NameProperty,$label)
            $activityButton = $window.FindFirst([Windows.Automation.TreeScope]::Descendants,$condition)
            if ($null -ne $activityButton) {
                $sidebar = [Windows.Automation.TreeWalker]::ControlViewWalker.GetParent($activityButton)
                break
            }
        }
        if ($null -eq $sidebar) { continue }
        foreach ($title in $titles) {
            if ($hints.ContainsKey($title)) { continue }
            $nameCondition = New-Object Windows.Automation.PropertyCondition([Windows.Automation.AutomationElement]::NameProperty,$title)
            $condition = New-Object Windows.Automation.AndCondition($buttonType,$nameCondition)
            $row = $sidebar.FindFirst([Windows.Automation.TreeScope]::Descendants,$condition)
            if ($null -eq $row) { continue }
            # The main row's aria-label is explicitly the chat title. Do not
            # read ListItem.Name or arbitrary secondary/preview text.
            foreach ($label in $labels) {
                if ($title -eq $label.name) { continue }
                $name = New-Object Windows.Automation.PropertyCondition([Windows.Automation.AutomationElement]::NameProperty,$label.name)
                $status = $row.FindFirst([Windows.Automation.TreeScope]::Descendants,$name)
                if ($null -ne $status) {
                    $hints[$title] = @{title=$title;state=$label.state}
                    break
                }
            }
        }
    }
    Send-Result @{available=$true;hints=@($hints.Values)}
} catch {
    Send-Result @{available=$false;reason=$_.Exception.GetType().Name;hints=@()}
}
