"""Validate the v0.5.1 transparent WebM set without importing Qt."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path

EXPECTED_ANIMATIONS = 35


class AssetValidationError(ValueError):
    pass


def validate_webm_assets(root):
    root = Path(root).resolve()
    config_file = root / 'codex-integration/bridge/bridge-config.json'
    config = json.loads(config_file.read_text(encoding='utf-8-sig')) if config_file.exists() else {}
    directory = Path(config.get('assetsDirectory', 'outputs/白龙桌宠素材'))
    assets = (root / directory).resolve()
    if directory.is_absolute() or directory.drive or not assets.is_relative_to(root):
        raise AssetValidationError('动画目录必须位于本版本目录内')
    raw = (assets / '素材清单.json').read_bytes()
    manifest = json.loads(raw.decode('utf-8-sig'))
    records = []
    def visit(value):
        if isinstance(value, dict):
            if 'file' in value:
                records.append(value)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
    visit(manifest)
    paths = set()
    for record in records:
        for key in ('file', 'playbackFile'):
            relative = Path(record.get(key, record['file']))
            target = (assets / relative).resolve()
            if (relative.is_absolute() or relative.drive or '..' in relative.parts
                    or not target.is_relative_to(assets) or target.suffix.lower() != '.webm'
                    or not target.is_file() or target.stat().st_size < 16):
                raise AssetValidationError('透明 WebM 清单或资源不完整：' + str(relative))
            with target.open('rb') as stream:
                if stream.read(4) != b'\x1aE\xdf\xa3':
                    raise AssetValidationError('透明 WebM 文件头无效：' + str(relative))
            paths.add(target)
    if len(records) != EXPECTED_ANIMATIONS or len(paths) != EXPECTED_ANIMATIONS:
        raise AssetValidationError('v0.5.1 必须包含 35 段独立动画')
    if not (root / 'tools/ffmpeg.exe').is_file():
        raise AssetValidationError('缺少透明视频解码器 tools/ffmpeg.exe')
    repaired = (assets / 'repair-validation.json').is_file()
    if repaired:
        validation = json.loads((assets / 'repair-validation.json').read_text(encoding='utf-8'))
        if not (validation.get('animations') == 35 and validation.get('rgbExactAfterDecoding') is True
                and validation.get('alphaExactAfterDecoding') is True
                and validation.get('manifestSha256') == hashlib.sha256(raw).hexdigest()):
            raise AssetValidationError('去噪 WebM 验证记录不完整')
    return {'assetSource': 'clean-webm' if repaired else 'upstream-webm', 'assetsDirectory': str(assets),
            'assetSourceError': None, 'assetAnimations': len(paths),
            'assetManifestSha256': hashlib.sha256(raw).hexdigest()}


def configure_asset_source(original_module):
    result = validate_webm_assets(original_module.ROOT)
    original_module.ASSETS = Path(result['assetsDirectory'])
    return result
