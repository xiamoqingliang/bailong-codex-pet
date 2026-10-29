"""Manifest validation rejects incomplete or escaping WebM resources."""
import json
import hashlib
from pathlib import Path
import tempfile
import unittest
from asset_source import validate_webm_assets, AssetValidationError


class AssetTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.assets = self.root / 'outputs/白龙桌宠素材'
        self.assets.mkdir(parents=True)
        (self.root / 'tools').mkdir()
        (self.root / 'tools/ffmpeg.exe').write_bytes(b'fixture')
        self.records = [{'id': str(i), 'file': f'{i}.webm'} for i in range(35)]
        for record in self.records:
            (self.assets / record['file']).write_bytes(b'\x1aE\xdf\xa3' + b'fixture' * 4)
        self.write_manifest()

    def write_manifest(self):
        (self.assets / '素材清单.json').write_text(json.dumps({'animations': self.records}), encoding='utf-8')

    def test_complete_set_is_accepted_without_legacy_frame_limits(self):
        self.assertEqual(validate_webm_assets(self.root)['assetAnimations'], 35)

    def test_release_canonical_directory_retains_repaired_asset_identity(self):
        raw = (self.assets / '素材清单.json').read_bytes()
        metadata = {'animations': 35, 'rgbExactAfterDecoding': True,
                    'alphaExactAfterDecoding': True, 'manifestSha256': hashlib.sha256(raw).hexdigest()}
        (self.assets / 'repair-validation.json').write_text(json.dumps(metadata), encoding='utf-8')
        self.assertEqual(validate_webm_assets(self.root)['assetSource'], 'clean-webm')

    def test_missing_decoder_rejected(self):
        (self.root / 'tools/ffmpeg.exe').unlink()
        with self.assertRaises(AssetValidationError):
            validate_webm_assets(self.root)

    def test_missing_animation_rejected(self):
        (self.assets / '5.webm').unlink()
        with self.assertRaises(AssetValidationError):
            validate_webm_assets(self.root)

    def test_invalid_header_rejected(self):
        (self.assets / '5.webm').write_bytes(b'not a video' * 10)
        with self.assertRaises(AssetValidationError):
            validate_webm_assets(self.root)

    def test_escaping_manifest_path_rejected(self):
        self.records[0]['file'] = '../../outside.webm'
        self.write_manifest()
        with self.assertRaises(AssetValidationError):
            validate_webm_assets(self.root)

    def test_duplicate_animation_rejected(self):
        self.records[0]['file'] = '1.webm'
        self.write_manifest()
        with self.assertRaises(AssetValidationError):
            validate_webm_assets(self.root)
