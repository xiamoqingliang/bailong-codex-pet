"""Build the v051 companion from this version directory only."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys
import zipfile
sys.path.insert(0, str(Path(__file__).resolve().parent))
from package_commands import uninstall_command

ROOT = Path(__file__).resolve().parents[1]
VERSION = '0.5.1.1'
NAME = 'Bailong-Codex-v051'
BRIDGE_FILES = '''bridge_loader.py bridge_pet.py pet_lifecycle.py bridge_policy.py
bridge_supervisor.py bailong_toolbar.py bailong_theme.py bailong_settings.py
dock_controls.py v051_behavior.py companion_layout.py window_policy.py ui_motion.py quota_panel.py activity_cards.py activity_runtime.py activity_source.py
activity_ui_hints.ps1 codex_observer.ps1 native_mini_control.ps1 native_menu_shield.cs
native_overlay_mask.py native_visibility.py desktop_ipc.py desktop_chat_adapter.py
desktop_chat_link.py conversation_sync.py chat_ui_style.py file_diff.py detached_launcher.py asset_source.py'''.split()
README_FILE = ROOT / 'docs/发行版README.md'
README = (README_FILE if README_FILE.is_file() else ROOT / 'README.md').read_text(encoding='utf-8')


def release_config():
    return {'fullAvatarIds': ['custom:bailong-full'], 'fullAvatarSlug': 'bailong-full',
            'mode': 'dedicated-toolbar', 'upstreamVersion': '0.5.1',
            'assetsDirectory': 'outputs/白龙桌宠素材'}


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def copy_file(source, target):
    if source.is_symlink() or not source.is_file():
        raise ValueError('Invalid release input: ' + str(source))
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def prepare(output):
    folder = output / 'stage' / NAME
    if folder.exists():
        raise RuntimeError('Stage already exists; use --seal or another --output')
    sys.path.insert(0, str(ROOT / 'codex-integration/bridge'))
    from asset_source import validate_webm_assets
    asset_status = validate_webm_assets(ROOT)
    data = folder / 'data'
    for directory in ('_internal', 'tools', 'licenses', 'app/sounds',
                      'runtime', 'codex-integration/full-entry'):
        for path in (ROOT / directory).rglob('*'):
            if path.is_file() and '__pycache__' not in path.parts:
                copy_file(path, data / path.relative_to(ROOT))
    selected_assets = Path(asset_status['assetsDirectory'])
    for path in selected_assets.rglob('*'):
        if path.is_file():
            copy_file(path, data / 'outputs/白龙桌宠素材' / path.relative_to(selected_assets))
    for name in ('BailongPet.exe', 'ASSETS.md', 'BUILD_INFO.json', 'VERSION.txt'):
        copy_file(ROOT / name, data / name)
    for name in BRIDGE_FILES:
        copy_file(ROOT / 'codex-integration/bridge' / name, data / 'codex-integration/bridge' / name)
    write_json(data / 'codex-integration/bridge/bridge-config.json', release_config())
    write_json(data / 'ADAPTATION_BUILD.json', {
        'version': VERSION, 'upstreamVersion': '0.5.1', 'architecture': 'Windows x64',
        'animationCount': 35, 'assetManifestSha256': asset_status['assetManifestSha256'],
        'bridgeSourceSha256': {name: digest(ROOT / 'codex-integration/bridge' / name) for name in BRIDGE_FILES}})
    copy_file(ROOT / 'packaging/compact_install.py', data / 'install.py')
    copy_file(ROOT / 'packaging/uninstall_pet.ps1', data / 'uninstall.ps1')
    (folder / 'README.md').write_text(README, encoding='utf-8')
    (folder / '一键安装到Codex.cmd').write_bytes(
        b'@echo off\r\nsetlocal\r\n"%~dp0data\\runtime\\python.exe" -X utf8 "%~dp0data\\install.py" %*\r\nset result=%errorlevel%\r\npause\r\nexit /b %result%\r\n')
    (folder / '一键卸载白龙.cmd').write_bytes(uninstall_command('full'))
    files = [str(path.relative_to(folder).as_posix()) for path in sorted(folder.rglob('*')) if path.is_file()]
    write_json(output / 'package-files.json', files)
    return folder, files


def seal(output):
    folder = output / 'stage' / NAME
    files = json.loads((output / 'package-files.json').read_text(encoding='utf-8'))
    actual = [str(path.relative_to(folder).as_posix()) for path in sorted(folder.rglob('*')) if path.is_file()]
    assert actual == files, 'Stage changed after preparation'
    assert {Path(name).parts[0] for name in files} == {'README.md', '一键安装到Codex.cmd', '一键卸载白龙.cmd', 'data'}
    assert not any(Path(name).name.startswith('test_') or '__pycache__' in name
                   or Path(name).suffix in {'.jsonl', '.log'} for name in files)
    assert len([name for name in files if name.endswith('.webm')]) == 35
    assert not any('clean-assets' in name or 'bridge-layout' in name or 'conversations' in name for name in files)
    data = folder / 'data'
    assert json.loads((data / 'codex-integration/bridge/bridge-config.json').read_text(encoding='utf-8')) == release_config()
    for name in BRIDGE_FILES:
        assert digest(data / 'codex-integration/bridge' / name) == digest(ROOT / 'codex-integration/bridge' / name), name
    assert digest(data / 'install.py') == digest(ROOT / 'packaging/compact_install.py')
    assert digest(data / 'uninstall.ps1') == digest(ROOT / 'packaging/uninstall_pet.ps1')
    hashes = {name: digest(folder / name) for name in files}
    archive = output / f'{NAME}-{VERSION}-windows-x64.zip'
    with zipfile.ZipFile(archive, 'x', zipfile.ZIP_DEFLATED, compresslevel=6) as target:
        for name in files:
            target.write(folder / name, f'{NAME}/{name}')
    with zipfile.ZipFile(archive) as target:
        assert len(target.infolist()) == len(files)
        for name, expected in hashes.items():
            with target.open(f'{NAME}/{name}') as stream:
                assert hashlib.file_digest(stream, 'sha256').hexdigest() == expected, name
    result = {'version': VERSION, 'archive': str(archive), 'bytes': archive.stat().st_size,
              'MiB': round(archive.stat().st_size / 1024**2, 2), 'sha256': digest(archive),
              'files': len(files), 'webmAnimations': 35, 'verified': True, 'personalDataIncluded': False}
    write_json(output / 'verification.json', result)
    write_json(output / 'file-hashes.json', hashes)
    (output / 'SHA256SUMS.txt').write_text(result['sha256'] + '  ' + archive.name + '\n', encoding='ascii')
    return result


def source_package(output):
    """Export maintained source and tools, never local state or analysis logs."""
    name = NAME + '-source'
    folder = output / 'source-stage' / name
    if folder.exists():
        raise RuntimeError('Source stage already exists; choose another output')
    for directory, suffixes in (('codex-integration/bridge', {'.py', '.ps1', '.cs'}),
                                ('codex-integration/edge-repair', {'.py'}),
                                ('packaging', {'.py', '.ps1'}), ('docs', {'.md'})):
        for path in sorted((ROOT / directory).glob('*')):
            if path.is_file() and path.suffix in suffixes:
                copy_file(path, folder / path.relative_to(ROOT))
    for directory in ('licenses', 'codex-integration/full-entry'):
        for path in sorted((ROOT / directory).rglob('*')):
            if path.is_file() and '__pycache__' not in path.parts:
                copy_file(path, folder / path.relative_to(ROOT))
    for filename in ('install.py', 'uninstall.ps1', 'ASSETS.md', 'BUILD_INFO.json', 'VERSION.txt', 'RELEASE_NOTES.md'):
        copy_file(ROOT / filename, folder / filename)
    write_json(folder / 'codex-integration/bridge/bridge-config.json', release_config())
    (folder / 'README.md').write_text(f'''# 白龙 v{VERSION} 项目源码与维护工具

本包包含本次适配的 Python／PowerShell／C# 源码、测试、打包脚本、去噪工具与说明。
可直接使用的完整程序见同目录的 Windows x64 安装包，其内置引擎、Python、解码器、音效和动画。

开发时先完整解压 Windows 安装包，再将本源码包内文件复制到其 data 目录中。
这样 data 即为项目根目录，无需复制本机账号、聊天记录、个人布局或运行日志。

在项目根目录运行：

    runtime\\python.exe -X utf8 -B -m unittest discover -s codex-integration\\bridge -p "test_*.py"
    runtime\\python.exe -X utf8 -B -m unittest discover -s packaging -p "test_compact_install.py"
    runtime\\python.exe -X utf8 -B packaging\\build_v051.py --output releases\\my-build
    runtime\\python.exe -X utf8 -B packaging\\build_v051.py --output releases\\my-build --seal
    runtime\\python.exe -X utf8 -B packaging\\build_v051.py --output releases\\my-build --source

build_v051.py 的 VERSION 定义适配版号；VERSION.txt 和 BUILD_INFO.json 保留原作者引擎信息。
素材去噪工具的额外开发依赖为 OpenCV、NumPy、Pillow；普通安装与运行无需这些额外开发依赖。
重新处理素材时使用原作者素材副本，现成安装包中的动画已经完成去噪。
''', encoding='utf-8')
    files = [str(path.relative_to(folder).as_posix()) for path in sorted(folder.rglob('*')) if path.is_file()]
    hashes = {relative: digest(folder / relative) for relative in files}
    archive = output / f'{NAME}-{VERSION}-project-source.zip'
    with zipfile.ZipFile(archive, 'x', zipfile.ZIP_DEFLATED, compresslevel=6) as target:
        for relative in files:
            target.write(folder / relative, f'{name}/{relative}')
    with zipfile.ZipFile(archive) as target:
        assert len(target.infolist()) == len(files)
        for relative, expected in hashes.items():
            with target.open(f'{name}/{relative}') as stream:
                assert hashlib.file_digest(stream, 'sha256').hexdigest() == expected
    write_json(output / 'source-file-hashes.json', hashes)
    result = {'version': VERSION, 'archive': str(archive), 'bytes': archive.stat().st_size,
              'sha256': digest(archive), 'files': len(files), 'verified': True}
    write_json(output / 'source-verification.json', result)
    with (output / 'SHA256SUMS.txt').open('a', encoding='ascii') as stream:
        stream.write(result['sha256'] + '  ' + archive.name + '\n')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'releases' / VERSION)
    parser.add_argument('--seal', action='store_true')
    parser.add_argument('--source', action='store_true')
    args = parser.parse_args()
    output = args.output.resolve()
    if args.seal and args.source:
        parser.error('Use --seal or --source separately')
    if args.source:
        result = source_package(output)
    elif args.seal:
        result = seal(output)
    else:
        folder, files = prepare(output)
        result = {'stage': str(folder), 'files': len(files)}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
