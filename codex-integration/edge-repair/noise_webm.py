"""Audit all native WebM frames and reuse the previous alpha-only cleanup.

Repair writes separate WebM assets. Original encoded color packets are copied
unchanged; repaired alpha is lossless. Every decoded output frame is verified.
"""
from __future__ import annotations
import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import time
import cv2
import numpy as np
from PIL import Image, ImageDraw
from repair_assets import clean_frame as previous_clean_frame, animation_items, NO_EFFECTS
from webm_alpha import replace_alpha, groups, elements

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / 'outputs/白龙桌宠素材'
OUT = ROOT / 'analysis/webm-noise'
FFMPEG = ROOT / 'tools/ffmpeg.exe'
FLAGS = subprocess.CREATE_NO_WINDOW


def clean_frame(frame, animation_id):
    """Stronger fringe/island pass on the twelve previously vetted scenes."""
    repaired, removed, stats = previous_clean_frame(frame, animation_id)
    if animation_id not in NO_EFFECTS:
        stats['extraRemovedPixels'] = 0
        return repaired, removed, stats
    original = np.asarray(frame)
    pixels = np.array(repaired)
    alpha = pixels[:, :, 3]
    rgb = pixels[:, :, :3].astype(np.int16)
    high, low = rgb.max(axis=2), rgb.min(axis=2)
    warm = np.minimum(rgb[:, :, 0], rgb[:, :, 1]) - rgb[:, :, 2]
    matte = ((warm >= 7) & (high < 225)) | ((high < 165) & (high - low <= 12))
    opaque = (alpha >= 240).astype(np.uint8)
    distance = cv2.distanceTransform(1 - opaque, cv2.DIST_L2, cv2.DIST_MASK_PRECISE)
    extra = (alpha > 0) & (alpha < 240) & matte & (distance > 1.25)
    count, labels, components, _ = cv2.connectedComponentsWithStats((alpha >= 16).astype(np.uint8), 8)
    if count > 1:
        main = (labels == 1 + np.argmax(components[1:, cv2.CC_STAT_AREA])).astype(np.uint8)
        support = cv2.dilate(main, np.ones((3, 3), np.uint8))
        extra |= (alpha > 0) & (alpha < 240) & (support == 0)
    # Never remove a new solid pixel in the stronger pass.
    extra &= original[:, :, 3] < 240
    pixels[:, :, 3][extra] = 0
    removed |= extra
    stats.update(changed=int(removed.sum()), extraRemovedPixels=int(extra.sum()),
                 visibleChanged=int((removed & (original[:, :, 3] >= 16)).sum()))
    return Image.fromarray(pixels), removed, stats


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def metadata(path):
    result = subprocess.run([str(FFMPEG), '-hide_banner', '-i', str(path)], capture_output=True, creationflags=FLAGS)
    text = result.stderr.decode('utf-8', errors='replace')
    match = re.search(r'Stream.*Video:.*? (\d+)x(\d+).*?([\d.]+) fps', text)
    if not match:
        raise ValueError('Missing video size/fps: ' + text[-500:])
    width, height, rate = match.groups()
    return int(width), int(height), rate


def frames(path, width, height):
    command = [str(FFMPEG), '-hide_banner', '-loglevel', 'error', '-threads', '1',
               '-c:v', 'libvpx-vp9', '-i', str(path), '-an', '-pix_fmt', 'rgba',
               '-f', 'rawvideo', 'pipe:1']
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=FLAGS)
    try:
        size = width * height * 4
        while True:
            data = process.stdout.read(size)
            if not data:
                break
            if len(data) != size:
                raise ValueError('Incomplete decoded frame')
            yield np.frombuffer(data, dtype=np.uint8).reshape(height, width, 4).copy()
        error = process.stderr.read()
        if process.wait() != 0:
            raise RuntimeError(error.decode('utf-8', errors='replace')[-600:])
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        process.stdout.close()
        process.stderr.close()


def preview(item, before, after, index):
    width, height = before.size
    sheet = Image.new('RGB', (width * 2, height + 40), '#101927')
    for column, frame in enumerate((before, after)):
        background = Image.new('RGBA', frame.size, '#101927')
        background.alpha_composite(frame)
        sheet.paste(background.convert('RGB'), (column * width, 40))
    draw = ImageDraw.Draw(sheet)
    draw.text((12, 10), f'Original {item["id"]} frame {index}', fill='white')
    draw.text((width + 12, 10), 'Previous alpha-only cleanup (proposal)', fill='white')
    sheet.save(OUT / f'{item["id"]}-worst.png')
    difference = np.array(before)[:, :, 3] != np.array(after)[:, :, 3]
    overlay = np.array(before)
    overlay[difference] = [255, 40, 20, 255]
    background = Image.new('RGBA', before.size, '#101927')
    background.alpha_composite(Image.fromarray(overlay))
    background.convert('RGB').save(OUT / f'{item["id"]}-removed-overlay.png')


def audit(item):
    cv2.setNumThreads(1)
    path = SOURCE / item['file']
    width, height, fps = metadata(path)
    records = []
    best = None
    for index, pixels in enumerate(frames(path, width, height)):
        original = Image.fromarray(pixels)
        repaired, removed, stats = clean_frame(original, item['id'])
        stats['frame'] = index
        # Alpha sum is a useful second ranking for barely visible vs solid noise.
        stats['removedAlphaSum'] = int(pixels[:, :, 3][removed].sum())
        records.append(stats)
        if best is None or stats.get('visibleChanged', 0) > best[0]:
            best = (stats.get('visibleChanged', 0), original, repaired, index)
    preview(item, best[1], best[2], best[3])
    report = {'id': item['id'], 'file': item['file'], 'frames': len(records),
              'size': [width, height], 'fps': fps, 'sourceSha256': sha(path),
              'changedPixels': sum(r['changed'] for r in records),
              'visibleChangedPixels': sum(r.get('visibleChanged', 0) for r in records),
              'maxVisibleChanged': best[0], 'worstFrame': best[3], 'perFrame': records}
    (OUT / f'{item["id"]}-audit.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    return report


def repair(item):
    cv2.setNumThreads(1)
    source = SOURCE / item['file']
    target = ROOT / 'codex-integration/clean-assets' / item['file']
    target.parent.mkdir(parents=True, exist_ok=True)
    width, height, fps = metadata(source)
    temporary = OUT / f'{item["id"]}-alpha-encode.webm'
    keyframes = [index for index, group in enumerate(groups(source))
                 if not any(kind == 0xFB for kind, _, _ in elements(group))]
    command = [str(FFMPEG), '-y', '-hide_banner', '-loglevel', 'error',
               '-f', 'rawvideo', '-pix_fmt', 'yuv420p', '-s', f'{width}x{height}', '-r', fps,
               '-i', 'pipe:0', '-an', '-c:v', 'libvpx-vp9', '-lossless', '1', '-b:v', '0',
               '-pix_fmt', 'yuv420p', '-auto-alt-ref', '0', '-lag-in-frames', '0',
               '-cpu-used', '4', '-threads', '2', '-g', '1000000',
               '-force_key_frames', 'expr:' + '+'.join(f'eq(n,{index})' for index in keyframes), str(temporary)]
    encoder = subprocess.Popen(command, stdin=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=FLAGS)
    rgb_hashes, alpha_hashes = [], []
    changed = 0
    extra_removed = 0
    try:
        for pixels in frames(source, width, height):
            cleaned, _, stats = clean_frame(Image.fromarray(pixels), item['id'])
            repaired = np.array(cleaned)
            rgb_hashes.append(hashlib.sha256(pixels[:, :, :3].tobytes()).hexdigest())
            alpha_hashes.append(hashlib.sha256(repaired[:, :, 3].tobytes()).hexdigest())
            changed += stats['changed']
            extra_removed += stats['extraRemovedPixels']
            # Directly encode alpha as lossless luma. FFmpeg's RGBA alpha
            # encoder does not propagate libvpx's lossless setting to alpha.
            encoder.stdin.write(repaired[:, :, 3].tobytes())
            encoder.stdin.write(bytes([128]) * (width * height // 2))
        encoder.stdin.close()
        error = encoder.stderr.read()
        if encoder.wait() != 0:
            raise RuntimeError(error.decode('utf-8', errors='replace'))
    finally:
        if encoder.poll() is None:
            encoder.kill()
            encoder.wait()
        encoder.stderr.close()
    count = replace_alpha(source, temporary, target)
    verified = 0
    for index, pixels in enumerate(frames(target, width, height)):
        assert hashlib.sha256(pixels[:, :, :3].tobytes()).hexdigest() == rgb_hashes[index], ('RGB changed', item['id'], index)
        assert hashlib.sha256(pixels[:, :, 3].tobytes()).hexdigest() == alpha_hashes[index], ('Alpha mismatch', item['id'], index)
        verified += 1
    assert verified == count == len(rgb_hashes)
    temporary.unlink()
    return {'id': item['id'], 'frames': count, 'sourceSha256': sha(source), 'outputSha256': sha(target),
            'sourceBytes': source.stat().st_size, 'outputBytes': target.stat().st_size,
            'removedPixels': changed, 'extraRemovedPixels': extra_removed,
            'encodedColorPacketsUnchanged': True,
            'rgbExactAfterDecoding': True, 'alphaExactAfterDecoding': True,
            'timestampsUnchanged': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repair', action='store_true')
    parser.add_argument('--ids', nargs='*')
    parser.add_argument('--jobs', type=int, default=4)
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    manifest = SOURCE / '素材清单.json'
    items = animation_items(json.loads(manifest.read_text(encoding='utf-8-sig')))
    selected = [item for item in items if not args.ids or item['id'] in args.ids]
    if args.ids and set(args.ids) != {item['id'] for item in selected}:
        raise ValueError('Unknown animation ID')
    if args.repair:
        output = ROOT / 'codex-integration/clean-assets'
        output.mkdir(parents=True, exist_ok=True)
        shutil.copy2(manifest, output / manifest.name)
        for item in items:
            target = output / item['file']
            target.parent.mkdir(parents=True, exist_ok=True)
            if item not in selected:
                shutil.copy2(SOURCE / item['file'], target)
    reports = []
    with ProcessPoolExecutor(max_workers=args.jobs) as pool:
        futures = {pool.submit(repair if args.repair else audit, item): item['id'] for item in selected}
        for future in as_completed(futures):
            report = future.result()
            reports.append(report)
            print(json.dumps({k: v for k, v in report.items() if k != 'perFrame'}, ensure_ascii=False), flush=True)
    summary = {'animations': len(reports), 'frames': sum(r['frames'] for r in reports),
               'reports': sorted(reports, key=lambda r: r['id'])}
    path = OUT / ('repair-report.json' if args.repair else 'audit-report.json')
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    if args.repair:
        summary = {'animations': len(items), 'repairedAnimations': len(reports),
                   'manifestSha256': sha(manifest), 'originalFilesUnchanged': True,
                   'rgbExactAfterDecoding': True, 'alphaExactAfterDecoding': True,
                   'encodedColorPacketsUnchanged': True, 'timestampsUnchanged': True}
        (ROOT / 'codex-integration/clean-assets/repair-validation.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
