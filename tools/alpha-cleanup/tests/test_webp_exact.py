"""Small offline round trips for exact animated WebP output; no live pet."""
from io import BytesIO
from pathlib import Path
import struct
import tempfile
import unittest

from PIL import Image

from webp_exact import ExactWebPWriter, _extract_lossless_frame, write_webp_frames


class ExactWebPTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="bailong-webp-test-")
        self.addCleanup(temporary.cleanup)
        self.destination = Path(temporary.name) / "clean.webp"

    def test_repeated_frames_alpha_rgb_loop_background_and_duration_round_trip(self):
        first = Image.new("RGBA", (13, 17), (203, 117, 59, 0))
        first.putpixel((3, 4), (100, 120, 140, 117))
        first.putpixel((6, 8), (44, 55, 66, 255))
        second = first.copy()
        second.putpixel((6, 8), (91, 81, 71, 0))
        transparent = Image.new("RGBA", first.size, (77, 88, 99, 0))
        frames = [first, first.copy(), second, transparent, transparent.copy()]
        durations = [41, 42, 43, 84, 123]
        background = (23, 45, 67, 89)
        with ExactWebPWriter(self.destination, first.size, loop=7, background=background) as writer:
            for frame, duration in zip(frames, durations):
                writer.append(frame, duration)
            self.assertEqual(writer.frame_count, 5)
            self.assertEqual(writer.duration_ms, sum(durations))
            self.assertFalse(self.destination.exists())
            self.assertTrue(writer.partial_path.exists())
        self.assertTrue(self.destination.exists())
        self.assertFalse(writer.partial_path.exists())
        with Image.open(self.destination) as decoded:
            self.assertEqual(decoded.n_frames, len(frames))
            self.assertEqual(decoded.info["loop"], 7)
            self.assertEqual(decoded.info["background"], background)
            for index, (expected, duration) in enumerate(zip(frames, durations)):
                decoded.seek(index)
                actual = decoded.convert("RGBA")
                self.assertEqual(decoded.info["duration"], duration)
                self.assertEqual(actual.tobytes(), expected.tobytes(), f"frame {index}")
        self.assertEqual(writer.finish(), self.destination)

    def test_odd_riff_chunk_padding_is_accepted_and_preserved(self):
        source = Image.new("RGBA", (3, 5), (10, 20, 30, 40))
        encoded = BytesIO()
        source.save(encoded, format="WEBP", lossless=True, exact=True)
        riff = encoded.getvalue()
        chunk = _extract_lossless_frame(riff, source.size)
        # A valid unknown RIFF chunk has one payload byte and one pad byte.
        body = b"WEBP" + b"JUNK" + struct.pack("<I", 1) + b"X\0" + riff[12:]
        padded = b"RIFF" + struct.pack("<I", len(body)) + body
        self.assertEqual(_extract_lossless_frame(padded, source.size), chunk)
        broken = bytearray(padded)
        broken[21] = 1
        with self.assertRaisesRegex(ValueError, "padding"):
            _extract_lossless_frame(bytes(broken), source.size)

    def test_invalid_static_payloads_are_rejected(self):
        source = Image.new("RGBA", (3, 5), (10, 20, 30, 40))
        encoded = BytesIO()
        source.save(encoded, format="WEBP", lossless=True, exact=True)
        riff = encoded.getvalue()
        for malformed in (b"not an image", riff[:-1], riff + b"unexpected"):
            with self.assertRaises(ValueError):
                _extract_lossless_frame(malformed, source.size)
        with self.assertRaisesRegex(ValueError, "dimensions"):
            _extract_lossless_frame(riff, (4, 5))
        frame = _extract_lossless_frame(riff, source.size)
        body = b"WEBP" + frame + frame
        with self.assertRaisesRegex(ValueError, "duplicate"):
            _extract_lossless_frame(b"RIFF" + struct.pack("<I", len(body)) + body, source.size)

    def test_failed_context_preserves_existing_destination(self):
        self.destination.write_bytes(b"previous validated asset")
        with self.assertRaisesRegex(RuntimeError, "processing failed"):
            with ExactWebPWriter(self.destination, (4, 4), overwrite=True) as writer:
                writer.append(Image.new("RGBA", (4, 4), (1, 2, 3, 4)), 42)
                raise RuntimeError("processing failed")
        self.assertEqual(self.destination.read_bytes(), b"previous validated asset")
        self.assertFalse(writer.partial_path.exists())

    def test_empty_or_mismatched_sequences_are_not_published(self):
        with self.assertRaisesRegex(ValueError, "at least one"):
            write_webp_frames(self.destination, [], [], size=(4, 4))
        self.assertFalse(self.destination.exists())
        with self.assertRaises(ValueError):
            write_webp_frames(self.destination, [Image.new("RGBA", (4, 4))], [], size=(4, 4))
        self.assertFalse(self.destination.exists())
        self.assertFalse(self.destination.with_name("clean.webp.partial").exists())

    def test_existing_files_and_partial_files_are_protected(self):
        self.destination.write_bytes(b"existing")
        with self.assertRaises(FileExistsError):
            ExactWebPWriter(self.destination, (4, 4))
        partial = self.destination.with_name("clean.webp.partial")
        partial.write_bytes(b"someone else's in-progress file")
        with self.assertRaises(FileExistsError):
            ExactWebPWriter(self.destination, (4, 4), overwrite=True)
        self.assertEqual(self.destination.read_bytes(), b"existing")
        self.assertEqual(partial.read_bytes(), b"someone else's in-progress file")

    def test_invalid_frame_size_and_noninteger_duration(self):
        with ExactWebPWriter(self.destination, (4, 4)) as writer:
            with self.assertRaises(ValueError):
                writer.append(Image.new("RGBA", (5, 4)), 42)
            for duration in (-1, 2**24, 41.5, True):
                with self.assertRaises(ValueError):
                    writer.append(Image.new("RGBA", (4, 4)), duration)
            writer.append(Image.new("RGBA", (4, 4)), 42)


if __name__ == "__main__":
    unittest.main()
