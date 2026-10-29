"""Exercise Windows PowerShell's real redirected-output encoding, without UI."""
import json
import os
from pathlib import Path
import subprocess
import unittest


@unittest.skipUnless(os.name == 'nt', 'Windows PowerShell integration')
class NativeControllerWireTests(unittest.TestCase):
    def test_unicode_result_is_valid_ascii_json_over_oem_stdout(self):
        powershell = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
        script = Path(__file__).with_name('native_mini_control.ps1')
        result = subprocess.run(
            [str(powershell), '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
             str(script), '-Action', '编码测试'], capture_output=True, timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        self.assertEqual(result.returncode, 2)
        self.assertTrue(all(value < 128 for value in result.stdout))
        payload = json.loads(result.stdout.decode('utf-8'))
        self.assertEqual(payload['action'], '编码测试')
        self.assertEqual(payload['reason'], 'invalid-action')


if __name__ == '__main__':
    unittest.main()
