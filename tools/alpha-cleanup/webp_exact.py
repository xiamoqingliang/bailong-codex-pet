"""Stream lossless animated WebP without coalescing identical frames.

Pillow's animation encoder combines consecutive identical frames. Bailong's
player uses the frame count to compute its playback position, so this module
encodes each complete RGBA frame separately and wraps the resulting VP8L data
in one ANMF chunk per input frame. It never rewrites an input animation.

Only a completed, flushed animation is published at ``destination``. Until
then the data lives at ``destination.name + '.partial'``. An exception inside
the context manager removes only that writer's incomplete file.
"""
from __future__ import annotations

from io import BytesIO
import os
from pathlib import Path
import struct
from typing import Iterable

from PIL import Image


MAX_FRAME_DURATION = (1 << 24) - 1
MAX_IMAGE_DIMENSION = 16383


def _integer(value: int, minimum: int, maximum: int, name: str) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer in [{minimum}, {maximum}]")
    return value


def _u24(value: int) -> bytes:
    return value.to_bytes(3, "little")


def _chunk(tag: bytes, payload: bytes) -> bytes:
    if len(tag) != 4:
        raise ValueError("A RIFF chunk tag must contain four bytes")
    return tag + struct.pack("<I", len(payload)) + payload + (b"\0" if len(payload) & 1 else b"")


def _extract_lossless_frame(encoded: bytes, expected_size: tuple[int, int]) -> bytes:
    """Validate a standalone WebP and return its padded VP8L chunk.

    Ancillary metadata is not placed inside an animation frame. Reject lossy
    or animated inputs, truncated chunks, invalid padding, and duplicate
    image payloads instead of emitting an ambiguous animation.
    """
    if len(encoded) < 12 or encoded[:4] != b"RIFF" or encoded[8:12] != b"WEBP":
        raise ValueError("The static encoder did not produce a WebP RIFF file")
    if struct.unpack_from("<I", encoded, 4)[0] != len(encoded) - 8:
        raise ValueError("The static WebP RIFF size is inconsistent")
    cursor = 12
    frame = None
    while cursor < len(encoded):
        if len(encoded) - cursor < 8:
            raise ValueError("The static WebP has a truncated chunk header")
        tag = encoded[cursor:cursor + 4]
        length = struct.unpack_from("<I", encoded, cursor + 4)[0]
        payload_start = cursor + 8
        payload_end = payload_start + length
        chunk_end = payload_end + (length & 1)
        if chunk_end > len(encoded):
            raise ValueError("The static WebP has a truncated chunk payload")
        if length & 1 and encoded[payload_end] != 0:
            raise ValueError("The static WebP has invalid chunk padding")
        payload = encoded[payload_start:payload_end]
        if tag in (b"ANIM", b"ANMF", b"VP8 ", b"ALPH"):
            raise ValueError("Only a static lossless VP8L image can be used as a frame")
        if tag == b"VP8X":
            if length != 10 or payload[0] & 0x02:
                raise ValueError("The static WebP has an invalid or animated VP8X header")
        if tag == b"VP8L":
            if frame is not None or length < 5 or payload[0] != 0x2F:
                raise ValueError("The static WebP has a duplicate or invalid VP8L image")
            properties = int.from_bytes(payload[1:5], "little")
            size = ((properties & 0x3FFF) + 1, ((properties >> 14) & 0x3FFF) + 1)
            if properties >> 29 or size != expected_size:
                raise ValueError("The static VP8L dimensions or version are invalid")
            frame = encoded[cursor:chunk_end]
        cursor = chunk_end
    if frame is None:
        raise ValueError("The static WebP is missing its VP8L image")
    return frame


class ExactWebPWriter:
    """Write one full-canvas lossless frame for every ``append`` call.

    ``background`` is an RGBA tuple and ``loop=0`` means indefinite playback.
    ``append(image, duration_ms)`` preserves all RGBA bytes, including RGB
    values behind zero alpha, and accepts exact integer millisecond durations.
    Use the context manager or call ``finish()`` to publish the destination.
    A pre-existing destination is protected unless ``overwrite=True``.
    """

    def __init__(
        self,
        destination: str | Path,
        size: tuple[int, int],
        *,
        loop: int = 0,
        background: tuple[int, int, int, int] = (0, 0, 0, 0),
        method: int = 1,
        overwrite: bool = False,
    ):
        if len(size) != 2:
            raise ValueError("size must contain the canvas width and height")
        self.size = tuple(_integer(v, 1, MAX_IMAGE_DIMENSION, "canvas dimension") for v in size)
        self.loop = _integer(loop, 0, 65535, "loop")
        if len(background) != 4:
            raise ValueError("background must contain four RGBA channels")
        self.background = tuple(_integer(v, 0, 255, "background channel") for v in background)
        self.method = _integer(method, 0, 6, "method")
        self.destination = Path(destination).resolve()
        self.partial_path = self.destination.with_name(self.destination.name + ".partial")
        self.overwrite = bool(overwrite)
        self.frame_count = 0
        self.duration_ms = 0
        self.finished = False
        self._stream = None
        self._owns_partial = False
        if self.destination.exists() and not self.overwrite:
            raise FileExistsError(self.destination)
        self.destination.parent.mkdir(parents=True, exist_ok=True)
        self._stream = self.partial_path.open("xb")
        self._owns_partial = True
        try:
            width, height = self.size
            self._stream.write(b"RIFF\0\0\0\0WEBP")
            # VP8X: alpha and animation present, with no metadata flags.
            self._stream.write(_chunk(b"VP8X", b"\x12\0\0\0" + _u24(width - 1) + _u24(height - 1)))
            red, green, blue, alpha = self.background
            self._stream.write(_chunk(b"ANIM", bytes((blue, green, red, alpha)) + struct.pack("<H", loop)))
        except BaseException:
            self.abort()
            raise

    def append(self, image: Image.Image, duration_ms: int) -> None:
        if self._stream is None:
            raise RuntimeError("This WebP writer is closed")
        duration_ms = _integer(duration_ms, 0, MAX_FRAME_DURATION, "duration_ms")
        if image.size != self.size:
            raise ValueError(f"Frame size {image.size} does not match canvas {self.size}")
        frame = image if image.mode == "RGBA" else image.convert("RGBA")
        buffer = BytesIO()
        frame.save(buffer, format="WEBP", lossless=True, exact=True, method=self.method)
        encoded_frame = _extract_lossless_frame(buffer.getvalue(), self.size)
        width, height = self.size
        # Full canvas at (0, 0), no blending, no disposal. Each input frame
        # replaces all preceding pixels, including fully transparent pixels.
        header = _u24(0) + _u24(0) + _u24(width - 1) + _u24(height - 1) + _u24(duration_ms) + b"\x02"
        encoded_chunk = _chunk(b"ANMF", header + encoded_frame)
        if self._stream.tell() + len(encoded_chunk) - 8 > 0xFFFFFFFF:
            raise ValueError("The animation exceeds the WebP RIFF size limit")
        self._stream.write(encoded_chunk)
        self.frame_count += 1
        self.duration_ms += duration_ms

    def finish(self) -> Path:
        if self.finished:
            return self.destination
        if self._stream is None:
            raise RuntimeError("This WebP writer was aborted")
        try:
            if self.frame_count == 0:
                raise ValueError("An animated WebP needs at least one frame")
            file_length = self._stream.tell()
            self._stream.seek(4)
            self._stream.write(struct.pack("<I", file_length - 8))
            self._stream.flush()
            os.fsync(self._stream.fileno())
            self._stream.close()
            self._stream = None
            if self.destination.exists() and not self.overwrite:
                raise FileExistsError(self.destination)
            os.replace(self.partial_path, self.destination)
            self._owns_partial = False
            self.finished = True
            return self.destination
        except BaseException:
            self.abort()
            raise

    def abort(self) -> None:
        if self._stream is not None:
            self._stream.close()
            self._stream = None
        if self._owns_partial:
            self.partial_path.unlink(missing_ok=True)
            self._owns_partial = False

    def __enter__(self) -> ExactWebPWriter:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        if exc_type is None:
            self.finish()
        else:
            self.abort()
        return False


def write_webp_frames(
    destination: str | Path,
    frames: Iterable[Image.Image],
    durations_ms: Iterable[int],
    *,
    size: tuple[int, int],
    loop: int = 0,
    background: tuple[int, int, int, int] = (0, 0, 0, 0),
    method: int = 1,
    overwrite: bool = False,
) -> Path:
    """Convenience wrapper for equally sized frame and duration iterables."""
    with ExactWebPWriter(destination, size, loop=loop, background=background,
                         method=method, overwrite=overwrite) as writer:
        for frame, duration in zip(frames, durations_ms, strict=True):
            writer.append(frame, duration)
    return writer.destination
