"""Image-content regressions for noise and legitimate fading effects."""
import json
import unittest
import os
from pathlib import Path

import numpy as np
from PIL import Image

import repair_assets as repair


PROFILE_SOURCE = Path(os.environ['BAILONG_SOURCE_DIR']).resolve() if os.environ.get('BAILONG_SOURCE_DIR') else None
PROFILE_AVAILABLE = PROFILE_SOURCE is not None and (PROFILE_SOURCE / '素材清单.json').is_file()
ITEMS = ({item['id']: item for item in repair.animation_items(
    json.loads((PROFILE_SOURCE / '素材清单.json').read_text('utf-8-sig')))} if PROFILE_AVAILABLE else {})


def load(animation_id, frame):
    with Image.open(PROFILE_SOURCE / ITEMS[animation_id]['file']) as source:
        source.seek(frame)
        return source.convert('RGBA')


@unittest.skipUnless(PROFILE_AVAILABLE, "Set BAILONG_SOURCE_DIR to the original v0.4.1 assets for profile regressions")
class RepairContentTests(unittest.TestCase):
    def test_known_blinking_foot_noise_is_removed_without_changing_colors(self):
        source = load('idle2', 60)
        target, _, _ = repair.clean_frame(source, 'idle2')
        a, b = np.asarray(source), np.asarray(target)
        self.assertEqual(int(a[680:686, 440:448, 3].max()), 225)
        self.assertEqual(int(b[680:686, 440:448, 3].max()), 0)
        self.assertTrue(np.array_equal(a[:, :, :3], b[:, :, :3]))
        self.assertTrue(np.array_equal(a[230:410, 250:470], b[230:410, 250:470]))

    def test_lamp_chest_and_fading_anger_regions_are_unchanged(self):
        cases = [('04', 30, (0, 360, 0, 230)),
                 ('24', 72, (340, 720, 0, 360)),
                 ('13', 63, (160, 380, 0, 190)),
                 ('13', 63, (160, 380, 520, 720))]
        for animation_id, frame, (y1, y2, x1, x2) in cases:
            with self.subTest(animation=animation_id, frame=frame):
                source = load(animation_id, frame)
                target, _, _ = repair.clean_frame(source, animation_id)
                self.assertTrue(np.array_equal(np.asarray(source)[y1:y2, x1:x2],
                                               np.asarray(target)[y1:y2, x1:x2]))

    def test_fading_effects_keep_their_opaque_pixels_and_low_alpha_shape(self):
        for animation_id, frame in [('18', 82), ('21', 66), ('work02', 44), ('work02', 116)]:
            with self.subTest(animation=animation_id, frame=frame):
                source = load(animation_id, frame)
                target, _, _ = repair.clean_frame(source, animation_id)
                a, b = np.asarray(source), np.asarray(target)
                solid = a[:, :, 3] >= 240
                self.assertTrue(np.array_equal(a[solid], b[solid]))
                if animation_id == 'work02' and frame == 116:
                    self.assertTrue(np.array_equal(a[472:485, 656:664], b[472:485, 656:664]))

    def test_very_faint_small_particles_are_preserved(self):
        cases = [('18', 60, (56, 145, 7, 7)), ('quota', 71, (165, 226, 9, 9)),
                 ('quota', 72, (164, 224, 10, 10)), ('25', 64, (165, 276, 7, 11))]
        for animation_id, frame, (x, y, width, height) in cases:
            with self.subTest(animation=animation_id, frame=frame):
                source = load(animation_id, frame)
                target, _, _ = repair.clean_frame(source, animation_id)
                a = np.asarray(source)[y:y+height, x:x+width]
                b = np.asarray(target)[y:y+height, x:x+width]
                particle = a[:, :, 3] >= 16
                self.assertTrue(particle.any())
                self.assertTrue(np.array_equal(a[particle], b[particle]))


if __name__ == '__main__':
    unittest.main()
