"""Replace only WebM alpha packets, preserving encoded color and timestamps.

Container layout: https://wiki.webmproject.org/alpha-channel (BlockAdditional).
This adapter accepts the single-track, unlaced v051 release assets only.
"""
from __future__ import annotations
from pathlib import Path

SEGMENT = 0x18538067
CLUSTER = 0x1F43B675
GROUP = 0xA0
BLOCK = 0xA1
ADDITIONS = 0x75A1
MORE = 0xA6
ADDITIONAL = 0xA5


def vint(data, offset, identifier=False):
    if offset >= len(data) or data[offset] == 0:
        raise ValueError('Invalid EBML integer')
    count = 1
    while not data[offset] & (1 << (8 - count)):
        count += 1
        if count > 8:
            raise ValueError('Oversized EBML integer')
    if offset + count > len(data):
        raise ValueError('Truncated EBML integer')
    value = int.from_bytes(data[offset:offset + count], 'big')
    return (value if identifier else value & ((1 << (7 * count)) - 1)), count


def elements(data):
    cursor = 0
    while cursor < len(data):
        start = cursor
        identifier, count = vint(data, cursor, True)
        cursor += count
        size, count = vint(data, cursor)
        cursor += count
        if size == (1 << (7 * count)) - 1:
            size = len(data) - cursor
        end = cursor + size
        if end > len(data):
            raise ValueError('Truncated EBML element')
        yield identifier, data[cursor:end], data[start:end]
        cursor = end


def element(identifier, payload):
    for count in range(1, 9):
        if len(payload) < (1 << (7 * count)) - 1:
            size = ((1 << (7 * count)) | len(payload)).to_bytes(count, 'big')
            return identifier.to_bytes((identifier.bit_length() + 7) // 8, 'big') + size + payload
    raise ValueError('Element too large')


def groups(path):
    result = []
    segment = next(payload for kind, payload, _ in elements(Path(path).read_bytes()) if kind == SEGMENT)
    for kind, cluster, _ in elements(segment):
        if kind != CLUSTER:
            continue
        for kind, group, _ in elements(cluster):
            if kind == GROUP:
                result.append(group)
            elif kind == 0xA3:
                raise ValueError('Alpha asset has unexpected SimpleBlock')
    return result


def alpha_packets(path):
    packets = []
    for group in groups(path):
        additions = next(payload for kind, payload, _ in elements(group) if kind == ADDITIONS)
        more = next(payload for kind, payload, _ in elements(additions) if kind == MORE)
        packets.append(next(payload for kind, payload, _ in elements(more) if kind == ADDITIONAL))
    return packets


def video_packets(path):
    packets = []
    segment = next(payload for kind, payload, _ in elements(Path(path).read_bytes()) if kind == SEGMENT)
    for kind, cluster, _ in elements(segment):
        if kind != CLUSTER:
            continue
        for kind, payload, _ in elements(cluster):
            if kind == GROUP:
                payload = next(data for child, data, _ in elements(payload) if child == BLOCK)
            elif kind != 0xA3:
                continue
            track, count = vint(payload, 0)
            if track != 1 or payload[count + 2] & 0x06:
                raise ValueError('Unsupported alpha packet layout')
            packets.append(payload[count + 3:])
    return packets


def replace_alpha(source, alpha_source, output):
    packets = video_packets(alpha_source)
    group_count = 0
    result = []
    source_blocks = []
    for kind, payload, raw in elements(Path(source).read_bytes()):
        if kind != SEGMENT:
            result.append(raw)
            continue
        segment_parts = []
        for kind, payload, raw in elements(payload):
            if kind in (0x114D9B74, 0x1C53BB6B, 0xBF):
                continue  # Offset indexes and CRCs change when alpha sizes change.
            if kind != CLUSTER:
                segment_parts.append(raw)
                continue
            cluster_parts = []
            for kind, payload, raw in elements(payload):
                if kind in (0xA7, 0xAB, 0xBF):
                    continue  # Position, PrevSize and CRC.
                if kind != GROUP:
                    cluster_parts.append(raw)
                    continue
                if group_count >= len(packets):
                    raise ValueError('Missing alpha frame')
                group_parts = []
                block_seen = False
                alpha_seen = False
                for kind, payload, raw in elements(payload):
                    if kind == 0xBF:
                        continue
                    if kind == BLOCK:
                        track, track_bytes = vint(payload, 0)
                        if track != 1 or payload[track_bytes + 2] & 0x06:
                            raise ValueError('Only single-track unlaced v051 video is supported')
                        source_blocks.append(payload)
                        block_seen = True
                    if kind == ADDITIONS:
                        packet = element(0xEE, b'\x01') + element(ADDITIONAL, packets[group_count])
                        raw = element(ADDITIONS, element(MORE, packet))
                        alpha_seen = True
                    group_parts.append(raw)
                if not block_seen or not alpha_seen:
                    raise ValueError('Incomplete WebM alpha BlockGroup')
                cluster_parts.append(element(GROUP, b''.join(group_parts)))
                group_count += 1
            segment_parts.append(element(CLUSTER, b''.join(cluster_parts)))
        result.append(element(SEGMENT, b''.join(segment_parts)))
    if group_count != len(packets):
        raise ValueError('Source and repaired frame counts differ')
    Path(output).write_bytes(b''.join(result))
    output_blocks = [payload for group in groups(output) for kind, payload, _ in elements(group) if kind == BLOCK]
    if output_blocks != source_blocks:
        raise AssertionError('Original encoded colors or timestamps changed')
    return group_count
