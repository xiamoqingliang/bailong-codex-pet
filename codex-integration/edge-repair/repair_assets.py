"""Conservative alpha-only cleanup of original Bailong animations.

Source RGB, solid character pixels, animation frame count and timing are kept.
Output is a separate manifest-compatible asset directory, never the source.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
import hashlib
import json
from pathlib import Path
import shutil
import time

import cv2
import numpy as np
from PIL import Image, ImageDraw

from webp_exact import ExactWebPWriter

BASE = Path(__file__).resolve().parent
ROOT = BASE.parents[1]
SOURCE = ROOT / 'outputs' / '白龙桌宠素材'
OUTPUT = ROOT / 'codex-integration' / 'clean-assets'
NO_EFFECTS = frozenset(('idle', 'idle2', '01', '05', '08', '09', '14', '20', '22', '26', 'drag', 'landing'))


def animation_items(value):
    found = {}
    def visit(node):
        if isinstance(node, dict):
            if 'id' in node and 'file' in node:
                found[node['file']] = node
            for child in node.values():
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)
    visit(value)
    return list(found.values())


def clean_frame(frame, animation_id):
    pixels = np.array(frame, dtype=np.uint8)
    alpha = pixels[:, :, 3]
    rgb = pixels[:, :, :3].astype(np.int16)
    low = rgb.min(axis=2)
    high = rgb.max(axis=2)
    warm = np.minimum(rgb[:, :, 0], rgb[:, :, 1]) - rgb[:, :, 2]
    # Gray/olive matte and black shadow fragments, not purple ink or red hearts.
    matte = ((warm >= 7) & (high < 225)) | ((high < 165) & (high - low <= 12))
    opaque = (alpha >= 240).astype(np.uint8)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(opaque, 8)
    if count <= 1:
        return frame.copy(), np.zeros(alpha.shape, dtype=bool), {'changed': 0, 'protectedBodyChanged': 0}
    main = labels == (1 + np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    # Some matte blocks are almost opaque and touch the outline. Their olive
    # hue differs from the character's purple ink. Limit this pass to the
    # outer five source pixels, never the solid interior of the drawing.
    inside_distance = cv2.distanceTransform((alpha > 0).astype(np.uint8), cv2.DIST_L2, cv2.DIST_MASK_PRECISE)
    olive_edge = (warm >= 9) & (np.abs(rgb[:, :, 0] - rgb[:, :, 1]) <= 24) & (high < 235)
    border_matte = (alpha > 0) & olive_edge & (inside_distance <= 5.0)
    protected_body = main & ~border_matte if animation_id in NO_EFFECTS else main
    distance_all = cv2.distanceTransform(1 - opaque, cv2.DIST_L2, cv2.DIST_MASK_PRECISE)
    remove = (alpha > 0) & (alpha < 240) & (distance_all > 2.0) & matte
    if animation_id in NO_EFFECTS:
        remove |= border_matte
        # These sequences were checked for independent particles. Keep the
        # opaque subject and its antialiasing margin; clear matte islands even
        # when an individual island happens to be nearly opaque.
        distance_main = cv2.distanceTransform((~main).astype(np.uint8), cv2.DIST_L2, cv2.DIST_MASK_PRECISE)
        remove |= (alpha > 0) & matte & (distance_main > 2.0)
    else:
        # Preserve glow around detached solid stars/confetti/coins/flames.
        # Fading stars/paper may become gray; opacity and spatial separation
        # remain reliable even after their original color has disappeared.
        effect_core = opaque.astype(bool) & ~main
        if effect_core.any():
            effect_guard = cv2.dilate(effect_core.astype(np.uint8), cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (13, 13)))
            remove &= effect_guard == 0
        # A complete fading particle can have no fully opaque pixel left.
        # Preserve substantial detached shapes regardless of their current
        # gray/gold color; only clearly smaller matte debris remains eligible.
        weak = (alpha >= 16).astype(np.uint8)
        weak_count, weak_labels, weak_stats, _ = cv2.connectedComponentsWithStats(weak, 8)
        if weak_count > 1:
            weak_main = 1 + np.argmax(weak_stats[1:, cv2.CC_STAT_AREA])
            effect_ids = [label for label in range(1, weak_count)
                          if label != weak_main and weak_stats[label, cv2.CC_STAT_AREA] >= 32]
            if effect_ids:
                fading = np.isin(weak_labels, effect_ids).astype(np.uint8)
                fade_guard = cv2.dilate(fading, np.ones((5, 5), np.uint8))
                remove &= fade_guard == 0
        if animation_id == '04':
            # The light bulb and its gold sparkle halo occupy this known area.
            remove[:360, :230] = False
        if animation_id == '24':
            # The chest, gold coins and gold sparkle outlines overlap the
            # matte hue; preserve this complete effect area conservatively.
            remove[340:, :360] = False
        if animation_id == '13':
            # Anger symbols fade from red to dull gray/gold. Their complete
            # trajectories must survive the color-key cleanup during fading.
            remove[160:380, :190] = False
            remove[160:380, 520:] = False
    remove[protected_body] = False
    if animation_id in NO_EFFECTS:
        surviving = ((alpha >= 16) & ~remove).astype(np.uint8)
        component_count, component_labels, component_stats, _ = cv2.connectedComponentsWithStats(surviving, 8)
        if component_count > 1:
            body = component_labels == (1 + np.argmax(component_stats[1:, cv2.CC_STAT_AREA]))
            # Keep a two-pixel antialiasing margin. Only the twelve sequences
            # confirmed to have no separate particles use this island pass.
            support = cv2.dilate(body.astype(np.uint8), np.ones((5, 5), np.uint8))
            remove |= (alpha > 0) & (support == 0) & ~protected_body
    cleaned = pixels.copy()
    cleaned[:, :, 3][remove] = 0
    assert np.array_equal(cleaned[:, :, :3], pixels[:, :, :3])
    assert np.array_equal(cleaned[:, :, 3][protected_body], alpha[protected_body])
    return Image.fromarray(cleaned), remove, {
        'changed': int(remove.sum()),
        'visibleChanged': int((remove & (alpha >= 16)).sum()),
        'protectedBodyChanged': 0,
        'opaqueMatteChanged': int((remove & (alpha >= 240)).sum()),
    }


def detached_pixels(frame):
    alpha = np.asarray(frame)[:, :, 3]
    binary = (alpha >= 16).astype(np.uint8)
    count, _, stats, _ = cv2.connectedComponentsWithStats(binary, 8)
    return 0 if count <= 1 else int(stats[1:, 4].sum() - stats[1:, 4].max())


def render_preview(item, index):
    with Image.open(SOURCE / item['file']) as image:
        image.seek(index)
        frame = image.convert('RGBA')
    repaired, removed, stats = clean_frame(frame, item['id'])
    result = Image.new('RGB', (1440, 756), '#101927')
    for column, im in enumerate((frame, repaired)):
        bg = Image.new('RGBA', im.size, '#101927')
        bg.alpha_composite(im)
        result.paste(bg.convert('RGB'), (column * 720, 36))
    draw = ImageDraw.Draw(result)
    draw.text((12, 10), f"Original {item['id']} frame {index}", fill='white')
    draw.text((732, 10), f"Alpha-only cleanup | removed {stats['changed']} pixels", fill='white')
    name = f"compare-{item['id']}-{index}.png"
    result.save(BASE / name)
    stats.update(id=item['id'], frame=index, detachedBefore=detached_pixels(frame), detachedAfter=detached_pixels(repaired))
    if item['id'] == 'idle2' and index == 60:
        stats['knownNoiseAlphaBefore'] = int(np.asarray(frame)[680:686, 440:448, 3].max())
        stats['knownNoiseAlphaAfter'] = int(np.asarray(repaired)[680:686, 440:448, 3].max())
    return stats


def process_animation(item, overwrite=False):
    cv2.setNumThreads(1)
    start = time.monotonic()
    source_path = SOURCE / item['file']
    destination = (OUTPUT / item['file']).resolve()
    if not destination.is_relative_to(OUTPUT.resolve()):
        raise ValueError('Output path escapes the separate repair directory')
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
    changed, opaque_changed = [], 0
    durations = []
    with Image.open(source_path) as source:
        expected_frames = source.n_frames
        size = source.size
        with ExactWebPWriter(destination, size, loop=source.info.get('loop', 0),
                             background=source.info.get('background', (0, 0, 0, 0)),
                             method=1, overwrite=overwrite) as writer:
            for index in range(expected_frames):
                source.seek(index)
                original = source.convert('RGBA')
                duration = int(source.info['duration'])
                repaired, _, stats = clean_frame(original, item['id'])
                writer.append(repaired, duration)
                durations.append(duration)
                changed.append(stats['changed'])
                opaque_changed += stats.get('opaqueMatteChanged', 0)
    # Decode every encoded frame: exact RGB, alpha-only deletion and timing.
    with Image.open(source_path) as source, Image.open(destination) as target:
        assert target.n_frames == expected_frames and target.size == size
        for index, duration in enumerate(durations):
            source.seek(index)
            a = np.asarray(source.convert('RGBA'))
            target.seek(index)
            b = np.asarray(target.convert('RGBA'))
            assert target.info['duration'] == duration
            assert np.array_equal(a[:, :, :3], b[:, :, :3])
            assert np.all((b[:, :, 3] == a[:, :, 3]) | (b[:, :, 3] == 0))
    assert hashlib.sha256(source_path.read_bytes()).hexdigest() == source_hash
    report = {'id': item['id'], 'file': item['file'], 'sourceSha256': source_hash,
              'outputSha256': hashlib.sha256(destination.read_bytes()).hexdigest(),
              'frames': expected_frames, 'durations': durations, 'durationMs': sum(durations),
              'size': list(size), 'alphaOnly': True, 'unchangedSource': True,
              'rgbExactAfterEncoding': True, 'protectedBodyPixelsChanged': 0,
              'removedPixelsTotal': sum(changed), 'removedPixelsPerFrame': changed,
              'opaqueMattePixelsRemoved': opaque_changed,
              'bytes': destination.stat().st_size, 'seconds': round(time.monotonic() - start, 2)}
    (BASE / 'reports').mkdir(exist_ok=True)
    (BASE / 'reports' / f"{item['id']}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--preview', action='store_true')
    parser.add_argument('--build', action='store_true')
    parser.add_argument('--jobs', type=int, default=4)
    parser.add_argument('--ids', nargs='*')
    parser.add_argument('--overwrite', action='store_true')
    args = parser.parse_args()
    cv2.setNumThreads(1)
    manifest = SOURCE / '素材清单.json'
    items = animation_items(json.loads(manifest.read_text('utf-8-sig')))
    if args.preview:
        chosen = {'idle': 10, 'idle2': 60, '04': 30, '18': 60, '24': 72, '25': 48, 'work01': 60}
        report = [render_preview(item, chosen[item['id']]) for item in items if item['id'] in chosen]
        (BASE / 'preview-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(json.dumps(report, ensure_ascii=True), flush=True)
    if args.build:
        OUTPUT.mkdir(parents=True, exist_ok=True)
        selected_items = items if not args.ids else [item for item in items if item['id'] in args.ids]
        if args.ids and set(args.ids) - {item['id'] for item in selected_items}:
            raise ValueError('Unknown animation ID')
        reports = []
        with ProcessPoolExecutor(max_workers=args.jobs) as pool:
            work = {pool.submit(process_animation, item, args.overwrite): item['id'] for item in selected_items}
            for future in as_completed(work):
                report = future.result()
                reports.append(report)
                print(json.dumps({k: report[k] for k in ('id', 'frames', 'bytes', 'seconds')}) + f" completed={len(reports)}/{len(selected_items)}", flush=True)
        if len(selected_items) != len(items):
            reports = [json.loads((BASE / 'reports' / f"{item['id']}.json").read_text('utf-8')) for item in items]
        shutil.copy2(manifest, OUTPUT / manifest.name)
        summary = {'source': str(SOURCE), 'output': str(OUTPUT), 'animations': len(reports),
                   'frames': sum(r['frames'] for r in reports), 'alphaOnly': True,
                   'rgbExactAfterEncoding': True, 'originalFilesUnchanged': True,
                   'manifestSha256': hashlib.sha256(manifest.read_bytes()).hexdigest(),
                   'outputBytes': sum(r['bytes'] for r in reports),
                   'removedPixels': sum(r['removedPixelsTotal'] for r in reports),
                   'reports': sorted(reports, key=lambda r: r['id'])}
        (BASE / 'repair-report.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        (OUTPUT / 'repair-validation.json').write_text(json.dumps({k: v for k, v in summary.items() if k != 'reports'}, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
