"""Exercise real Image accessibility labels in an isolated offscreen WPF window."""
import base64
import json
import os
from pathlib import Path
import subprocess
import unittest


@unittest.skipUnless(os.name == "nt", "Windows UIAutomation fixture")
class ObserverPetIdentityTests(unittest.TestCase):
    def test_current_and_legacy_image_labels_identify_only_pet_images(self):
        source = str(Path(__file__).with_name("codex_observer.ps1")).replace("'", "''")
        labels = [
            {"label": "白龙完整版 智能伙伴", "expected": True},
            {"label": "白龍完整版 智能夥伴", "expected": True},
            {"label": "白龍完整版 寵物", "expected": True},
            {"label": "Bailong pet", "expected": True},
            {"label": "白龙完整版 虚拟宠物", "expected": True},
            {"label": "白龍完整版 虛擬寵物", "expected": True},
            {"label": "白龙完整版 智能伙伴预览", "expected": False},
            {"label": "Unrelated illustration", "expected": False},
        ]
        program = r'''
$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type -AssemblyName PresentationFramework
$tokens=$null; $errors=$null
$ast=[System.Management.Automation.Language.Parser]::ParseFile('__SOURCE__',[ref]$tokens,[ref]$errors)
if($errors.Count){throw 'Observer source has parser errors'}
foreach($name in @('newChatNames','nameConditions','nameCondition','buttonCondition','toolbarButtonCondition','imageCondition','petNameSuffixes')){
    $node=$ast.Find({param($n) $n -is [System.Management.Automation.Language.AssignmentStatementAst] -and $n.Left -is [System.Management.Automation.Language.VariableExpressionAst] -and $n.Left.VariablePath.UserPath -eq $name},$true)
    if($null -eq $node){throw ('Missing observer condition: '+$name)}
    . ([ScriptBlock]::Create($node.Extent.Text))
}
$function=$ast.Find({param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Test-HiddenMiniIdentity'},$true)
if($null -eq $function){throw 'Observer identity function missing'}
. ([ScriptBlock]::Create($function.Extent.Text))
Add-Type -ReferencedAssemblies PresentationFramework,PresentationCore,WindowsBase,System.Xaml -TypeDefinition @"
using System;
using System.Threading;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Automation;
using System.Windows.Interop;
using System.Windows.Threading;
public static class BailongImageIdentityFixture {
    static Thread thread;
    static Window window;
    static Image image;
    static readonly ManualResetEvent ready = new ManualResetEvent(false);
    public static IntPtr Handle;
    public static void Start() {
        thread = new Thread(() => {
            window = new Window { ShowInTaskbar = false, ShowActivated = false,
                Left = -30000, Top = -30000, Width = 100, Height = 100 };
            image = new Image { Width = 64, Height = 64 };
            AutomationProperties.SetName(image, "Fixture image");
            window.Content = image;
            window.Show();
            Handle = new WindowInteropHelper(window).Handle;
            ready.Set();
            Dispatcher.Run();
        });
        thread.SetApartmentState(ApartmentState.STA);
        thread.IsBackground = true;
        thread.Start();
        if (!ready.WaitOne(3000)) throw new InvalidOperationException("Fixture window startup timed out");
    }
    public static void Rename(string name) {
        window.Dispatcher.Invoke(new Action(() => AutomationProperties.SetName(image, name)));
    }
    public static void Stop() {
        if (window != null) window.Dispatcher.BeginInvoke(new Action(() => {
            window.Close();
            Dispatcher.CurrentDispatcher.BeginInvokeShutdown(DispatcherPriority.Send);
        }));
        if (thread != null) thread.Join(1000);
    }
}
"@
$checks=0
try {
    [BailongImageIdentityFixture]::Start()
    $handle=[BailongImageIdentityFixture]::Handle
    foreach($case in ('__LABELS__' | ConvertFrom-Json)) {
        [BailongImageIdentityFixture]::Rename($case.label)
        $root=[Windows.Automation.AutomationElement]::FromHandle($handle)
        $images=$root.FindAll([Windows.Automation.TreeScope]::Descendants,$imageCondition)
        if($images.Count -ne 1 -or $images[0].Current.Name -cne $case.label){throw 'Fixture did not expose the actual Image label'}
        $actual=Test-HiddenMiniIdentity $handle
        if($actual -ne $case.expected){throw ('Image identity mismatch at case '+$checks)}
        $checks++
    }
    @{passed=$true;imageChecks=$checks;realCodexAccessed=$false} | ConvertTo-Json -Compress
} finally { [BailongImageIdentityFixture]::Stop() }
'''.replace("__SOURCE__", source).replace("__LABELS__", json.dumps(labels, ensure_ascii=True))
        encoded = base64.b64encode(program.encode("utf-16-le")).decode("ascii")
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-MTA", "-EncodedCommand", encoded],
            capture_output=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(completed.returncode, 0, completed.stderr.decode(errors="replace"))
        result = json.loads(completed.stdout)
        self.assertTrue(result["passed"])
        self.assertEqual(result["imageChecks"], len(labels))
        self.assertFalse(result["realCodexAccessed"])


if __name__ == "__main__":
    unittest.main()
