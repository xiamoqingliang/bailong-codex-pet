"""Abort the original constructor before decoding if Codex closes mid-start."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import bridge_pet


class LoadingExitTests(unittest.TestCase):
    def test_host_closes_before_first_decode_leaves_no_loading_window(self):
        program = r'''
import os,sys,json
from pathlib import Path
from unittest import mock
os.environ['QT_QPA_PLATFORM']='offscreen'
sys.path.insert(0,sys.argv[1])
import bridge_pet, bridge_loader, asset_source, desktop_chat_link
original=bridge_loader.load_original_module()
fixture=Path(sys.argv[2])
(fixture/'素材清单.json').write_text(json.dumps({'idle':{'file':'unused.webp'},'variants':[]}),encoding='utf-8')
class Socket:
    def connectToServer(self,*a):pass
    def waitForConnected(self,*a):return False
class Server:
    @staticmethod
    def removeServer(*a):pass
    def listen(self,*a):return True
    def close(self):pass
records=[]
visible={'running':True,'pid':101}
with mock.patch.object(original,'QLocalSocket',Socket), mock.patch.object(original,'QLocalServer',Server), \
     mock.patch.object(original,'ASSETS',fixture), \
     mock.patch.object(original.Pet,'load',side_effect=AssertionError('Decoder must not run after host closes')), \
     mock.patch.object(asset_source,'configure_asset_source',return_value={}), \
     mock.patch.object(desktop_chat_link,'install_desktop_chat_link'), \
     mock.patch.object(bridge_pet,'STOP_FILE',fixture/'pet.stop'), \
     mock.patch.object(bridge_pet,'save_status',side_effect=records.append), \
     mock.patch.object(bridge_pet,'read_state',side_effect=[visible,visible,{'running':False,'pid':None}]):
    assert bridge_pet.main()==0
assert records[-1]['phase']=='stopped'
assert records[-1]['exitReason']=='codex-closed'
assert records[-1]['exitHostPid']==101
assert not any(window.isVisible() for window in original.QApplication.topLevelWidgets())
print(json.dumps({'passed':True,'decodedFrames':0,'visibleWindows':0}))
'''
        with tempfile.TemporaryDirectory(prefix="bailong-loading-exit-") as directory:
            result = subprocess.run([sys.executable, "-c", program,
                str(Path(bridge_pet.__file__).parent), directory],
                capture_output=True, timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        self.assertTrue(json.loads(result.stdout)["passed"])


if __name__ == "__main__":
    unittest.main()
