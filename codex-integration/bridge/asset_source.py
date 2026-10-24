"""Use the only animation set shipped in the compact release.

The original engine's ROOT still owns sounds and runtime caches. Only ASSETS
changes, before any Pet instance is constructed. No originals, fallback source,
user configuration, GUI, or account data are needed by this module.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import struct


DIRECTORY = Path(__file__).resolve().parent
DEFAULT_ROOT = DIRECTORY.parents[1]
CLEAN_RELATIVE_DIRECTORY = Path("codex-integration") / "clean-assets"
MANIFEST_NAME = "素材清单.json"
VALIDATION_NAME = "repair-validation.json"
EXPECTED_ANIMATIONS = 25
EXPECTED_FRAMES = 2529
MAX_METADATA_BYTES = 1024 * 1024


class AssetValidationError(ValueError):
    """The compact package must be extracted again from an intact archive."""


def _contained_path(base: Path, value: str | Path, label: str) -> Path:
    if not isinstance(value, (str, Path)) or not str(value).strip():
        raise AssetValidationError(f"{label} must name a nonempty relative path")
    candidate = Path(value)
    if candidate.is_absolute() or candidate.drive or candidate.root or ".." in candidate.parts:
        raise AssetValidationError(f"{label} must remain inside its asset directory")
    resolved = (base / candidate).resolve()
    if not resolved.is_relative_to(base.resolve()):
        raise AssetValidationError(f"{label} escapes its permitted directory")
    return resolved


def _read_json(path: Path, label: str) -> tuple[bytes, dict]:
    if not path.is_file() or not 0 < path.stat().st_size <= MAX_METADATA_BYTES:
        raise AssetValidationError(f"{label} is missing or has an invalid size")
    raw = path.read_bytes()
    try:
        value = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeError, ValueError) as error:
        raise AssetValidationError(f"{label} is not valid UTF-8 JSON") from error
    if not isinstance(value, dict):
        raise AssetValidationError(f"{label} must be a JSON object")
    return raw, value


def _animation_records(value):
    if isinstance(value, dict):
        if "file" in value or "playbackFile" in value:
            yield value
        for child in value.values():
            yield from _animation_records(child)
    elif isinstance(value, list):
        for child in value:
            yield from _animation_records(child)


def _webp_frame_count(path: Path) -> int:
    """Count ANMF chunks using seeks, without decoding or reading image payloads."""
    size = path.stat().st_size
    if size < 12:
        raise AssetValidationError("An animation has an incomplete WebP header")
    with path.open("rb") as stream:
        header = stream.read(12)
        if (header[:4] != b"RIFF" or header[8:] != b"WEBP"
                or struct.unpack_from("<I", header, 4)[0] + 8 != size):
            raise AssetValidationError("An animation has an invalid WebP header or length")
        count = 0
        while stream.tell() < size:
            chunk = stream.read(8)
            if len(chunk) != 8:
                raise AssetValidationError("An animation has a truncated WebP chunk")
            length = struct.unpack_from("<I", chunk, 4)[0]
            end = stream.tell() + length + (length & 1)
            if end > size:
                raise AssetValidationError("An animation has a truncated WebP payload")
            if chunk[:4] == b"ANMF":
                if length < 16:
                    raise AssetValidationError("An animation has a truncated frame header")
                count += 1
                if count > EXPECTED_FRAMES:
                    raise AssetValidationError("An animation exceeds the package frame limit")
            stream.seek(end)
        if count == 0:
            raise AssetValidationError("An animation has no animated WebP frames")
        return count


def validate_asset_directory(root: Path, original_directory: Path | None = None,
                             configured_directory: str | None = None) -> dict:
    """Validate this release's clean set; legacy original_directory is ignored.

    The optional legacy arguments keep offline packaging probes compatible.
    They cannot redirect the compact release to another source.
    """
    root = Path(root).resolve()
    if (configured_directory is not None
            and Path(configured_directory) != CLEAN_RELATIVE_DIRECTORY):
        raise AssetValidationError("The compact release only contains clean animations")
    target = _contained_path(root, CLEAN_RELATIVE_DIRECTORY, "Animation directory")
    if not target.is_dir():
        raise AssetValidationError("Clean animations are missing; extract the complete package again")
    manifest_path = _contained_path(target, MANIFEST_NAME, "Asset manifest")
    validation_path = _contained_path(target, VALIDATION_NAME, "Repair validation")
    manifest_bytes, manifest = _read_json(manifest_path, "Asset manifest")
    _, validation = _read_json(validation_path, "Repair validation")
    for name, expected in (("animations", EXPECTED_ANIMATIONS), ("frames", EXPECTED_FRAMES)):
        if type(validation.get(name)) is not int or validation[name] != expected:
            raise AssetValidationError(f"Repair validation requires {name}={expected}")
    for name in ("alphaOnly", "rgbExactAfterEncoding", "originalFilesUnchanged"):
        if validation.get(name) is not True:
            raise AssetValidationError(f"Repair validation requires {name}=true")
    manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
    if validation.get("manifestSha256") != manifest_sha:
        raise AssetValidationError("The repair manifest checksum does not match")
    records = list(_animation_records(manifest))
    if len(records) != EXPECTED_ANIMATIONS:
        raise AssetValidationError(f"The manifest must reference exactly {EXPECTED_ANIMATIONS} animations")
    playback_paths = set()
    for item in records:
        if "file" not in item:
            raise AssetValidationError("An animation is missing its file field")
        for key in ("file", "playbackFile"):
            if key not in item:
                continue
            if not isinstance(item[key], str):
                raise AssetValidationError(f"Animation {key} must be a string")
            asset = _contained_path(target, item[key], f"Animation {key}")
            if asset.suffix.lower() != ".webp" or not asset.is_file():
                raise AssetValidationError("A required WebP animation is missing")
        playback = item.get("playbackFile", item["file"])
        playback_paths.add(_contained_path(target, playback, "Playback file"))
    if len(playback_paths) != EXPECTED_ANIMATIONS:
        raise AssetValidationError(f"The manifest must reference {EXPECTED_ANIMATIONS} distinct playback files")
    frames = sum(_webp_frame_count(path) for path in playback_paths)
    if frames != EXPECTED_FRAMES:
        raise AssetValidationError(f"The clean animation files must contain {EXPECTED_FRAMES} frames")
    return {
        "assetSource": "clean",
        "assetsDirectory": str(target),
        "assetSourceError": None,
        "assetAnimations": EXPECTED_ANIMATIONS,
        "assetFrames": frames,
        "assetManifestSha256": manifest_sha,
    }


def configure_asset_source(original_module) -> dict:
    """Validate first, then redirect the original engine's ASSETS only."""
    result = validate_asset_directory(original_module.ROOT)
    original_module.ASSETS = Path(result["assetsDirectory"])
    return result
