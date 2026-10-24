"""Portable CLI regressions with small synthetic animations; no real assets."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from PIL import Image, ImageDraw
from webp_exact import ExactWebPWriter


TOOL = Path(__file__).resolve().parents[1] / "repair_assets.py"


class PortableCliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="bailong-portable-cli-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.source = self.base / "原始 素材"
        self.output = self.base / "处理 结果"
        self.source.mkdir()
        self.frames = []
        for index in range(3):
            frame = Image.new("RGBA", (720, 720), (83, 67, 121, 0))
            ImageDraw.Draw(frame).rectangle((200, 150, 520, 650), fill=(245, 231, 253, 255))
            frame.putpixel((10 + index, 10), (100, 101, 99, 120))
            self.frames.append(frame)
        for name in ("idle.webp", "idle2.webp"):
            with ExactWebPWriter(self.source / name, (720, 720), loop=3) as writer:
                for frame, duration in zip(self.frames, (41, 42, 84)):
                    writer.append(frame, duration)
        self.manifest = {"idle": {"id": "idle", "file": "idle.webp"},
                         "additionalIdleAnimations": [{"id": "idle2", "file": "idle2.webp"}], "variants": []}
        self.write_manifest()
        self.before = self.hashes()

    def write_manifest(self):
        (self.source / "素材清单.json").write_text(json.dumps(self.manifest), encoding="utf-8")

    def hashes(self):
        return {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in self.source.iterdir() if path.is_file()}

    def run_cli(self, *args, output=None):
        return subprocess.run([sys.executable, str(TOOL), "--source", str(self.source),
            "--output", str(output or self.output), *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
            cwd=self.base)

    def test_first_partial_build_preview_and_duplicate_frame_timing(self):
        result = self.run_cli("--preview", "--build", "--ids", "idle2", "--jobs", "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.hashes(), self.before)
        self.assertTrue((self.output / "idle2.webp").is_file())
        self.assertFalse((self.output / "idle.webp").exists())
        previews = list(self.output.rglob("compare-idle2-*.png"))
        self.assertEqual(len(previews), 1)
        with Image.open(self.output / "idle2.webp") as cleaned:
            self.assertEqual(cleaned.n_frames, 3)
            self.assertEqual(cleaned.info["loop"], 3)
            for index, duration in enumerate((41, 42, 84)):
                cleaned.seek(index)
                frame = cleaned.convert("RGBA")
                self.assertEqual(cleaned.info["duration"], duration)
                self.assertEqual(frame.convert("RGB").tobytes(), self.frames[index].convert("RGB").tobytes())
                self.assertEqual(frame.getpixel((10 + index, 10))[3], 0)
                self.assertEqual(frame.getpixel((300, 300)), (245, 231, 253, 255))
        for name in ("素材清单.json", "repair-validation.json"):
            if (self.output / name).exists():
                value = json.loads((self.output / name).read_text(encoding="utf-8"))
                self.assertIs(value.get("complete"), False)

    def test_full_build_windows_workers_use_explicit_paths(self):
        result = self.run_cli("--build", "--jobs", "2")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.hashes(), self.before)
        self.assertEqual((self.output / "素材清单.json").read_bytes(), (self.source / "素材清单.json").read_bytes())
        report = json.loads((self.output / "repair-validation.json").read_text(encoding="utf-8"))
        self.assertEqual(report["animations"], 2)
        self.assertEqual(report["frames"], 6)
        self.assertTrue(report["alphaOnly"] and report["rgbExactAfterEncoding"] and report["originalFilesUnchanged"])
        self.assertIs(report.get("complete"), True)

    def test_rejects_source_overlap_without_changing_source(self):
        for destination in (self.source, self.source / "nested-output", self.base):
            with self.subTest(destination=destination):
                result = self.run_cli("--build", output=destination)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.hashes(), self.before)

    def test_invalid_selection_or_jobs_produces_no_output(self):
        for args in (("--build", "--ids", "unknown"), ("--build", "--jobs", "0")):
            result = self.run_cli(*args)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(self.output.exists())
            self.assertEqual(self.hashes(), self.before)

    def test_rejects_manifest_path_escape(self):
        self.manifest["idle"]["file"] = "../outside.webp"
        self.write_manifest()
        result = self.run_cli("--build")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
