"""Corner and multi-monitor placement regressions from user screenshots."""
import unittest
from companion_layout import place_window, place_vertical_window, vertical_group_layout, intersects
from companion_layout import is_task_notification, place_side_window
from types import SimpleNamespace


class PlacementTests(unittest.TestCase):
    def test_centered_side_panels_clamp_only_at_screen_edges(self):
        for sx, sy in ((0, 0), (-1600, -900)):
            area = (sx, sy, 1600, 900)
            for y in (sy, sy + 300, sy + 644):
                owner = (sx + 600, y, 256, 256)
                for side in ('left', 'right'):
                    point = place_side_window((350, 430), owner, area, gap=12,
                                              preferred_side=side, allowed_sides=(side,), centered=True)
                    self.assertIsNotNone(point)
                    self.assertEqual(point[1], max(sy, min(y + (256 - 430) // 2, sy + 900 - 430)))
                    self.assertEqual(point[0], owner[0] - 350 - 12 if side == 'left' else owner[0] + 256 + 12)

    def test_centered_side_panels_never_shift_diagonally_to_avoid_obstacles(self):
        owner = (600, 300, 256, 256)
        area = (0, 0, 1600, 900)
        # A wide task card blocks the upper part of a tall settings panel.
        obstacle = (500, 170, 500, 110)
        for side in ('left', 'right'):
            options = dict(gap=12, preferred_side=side, allowed_sides=(side,))
            legacy = place_side_window((350, 430), owner, area, (obstacle,), **options)
            self.assertIsNotNone(legacy)
            self.assertNotEqual(legacy[1], 213)
            centered = place_side_window((350, 430), owner, area, (obstacle,), centered=True, **options)
            self.assertEqual(centered[1], 213)
            self.assertFalse(intersects((*centered, 350, 430), obstacle, 12))
            if side == 'left':
                self.assertEqual(centered[0] + 350 + 12, obstacle[0])
            else:
                self.assertEqual(centered[0], obstacle[0] + obstacle[2] + 12)
            self.assertEqual(place_side_window((320, 280), owner, area, (obstacle,), centered=True, **options)[1], 288)

    def test_centered_side_panel_ignores_obstacles_outside_its_vertical_range(self):
        owner = (600, 300, 256, 256)
        point = place_side_window((350, 430), owner, (0, 0, 1600, 900),
                                  ((400, 50, 800, 100),), gap=12,
                                  preferred_side='right', centered=True)
        self.assertEqual(point, (868, 213))

    def test_preferred_side_is_retained_across_midpoint_until_it_cannot_fit(self):
        for sx in (0, -1500):
            area = (sx, 0, 1500, 900)
            owner = (sx + 700, 300, 256, 256)
            # Left has more space, but a previously opened right panel fits.
            point = place_side_window((280, 180), owner, area, preferred_side='right')
            self.assertEqual(point[0], owner[0] + owner[2] + 8)
            near_edge = (sx + 1200, 300, 256, 256)
            self.assertIsNone(place_side_window((280, 180), near_edge, area,
                                               preferred_side='right', allowed_sides=('right',)))
            fallback = place_side_window((280, 180), near_edge, area)
            self.assertEqual(fallback[0] + 280 + 8, near_edge[0])

    def test_speech_stays_beside_pet_for_all_text_heights(self):
        owner = (400, 350, 256, 256)
        area = (0, 0, 1200, 900)
        for height in (52, 70, 36, 52):
            positions = vertical_group_layout(owner, area, (256, height), (280, 46), (304, 110), task_notification=True)
            speech = positions['bubble']
            card = positions['activity_cards']
            self.assertTrue(speech[0] + 256 < owner[0] or speech[0] > owner[0] + owner[2])
            self.assertFalse(intersects((*speech, 256, height), (*card, 304, 110)))
            self.assertEqual(card[1] + 110 + 8, owner[1])
            self.assertEqual(positions['toolbar'][1], owner[1] + owner[3] + 8)

    def test_speech_near_top_edge_still_uses_a_side(self):
        owner = (400, 20, 256, 256)
        positions = vertical_group_layout(owner, (0, 0, 1200, 900), (256, 70), (280, 46), (304, 110), task_notification=True)
        self.assertTrue(positions['bubble'][0] + 256 < owner[0] or positions['bubble'][0] > owner[0] + owner[2])
        self.assertEqual(positions['toolbar'][1], owner[1] + owner[3] + 8)

    def test_large_messages_at_all_monitor_corners_only_use_left_or_right(self):
        for area in ((0, 0, 800, 700), (-1920, -1080, 1920, 1080)):
            sx, sy, sw, sh = area
            for x in (sx, sx + sw - 256):
                for y in (sy, sy + sh - 256):
                    owner = (x, y, 256, 256)
                    for height in (52, 225, 450):
                        positions = vertical_group_layout(owner, area, (256, height), (280, 46), (304, 110), task_notification=True)
                        speech = positions['bubble']
                        self.assertIsNotNone(speech)
                        self.assertTrue(speech[0] + 256 < x or speech[0] > x + 256)
                        self.assertGreaterEqual(speech[1], sy)
                        self.assertLessEqual(speech[1] + height, sy + sh)

    def test_interaction_speech_stays_between_task_card_and_pet(self):
        owner = (400, 350, 256, 256)
        for height in (52, 70, 36, 52):
            positions = vertical_group_layout(owner, (0, 0, 1200, 900), (256, height), (280, 46), (304, 110))
            speech = positions['bubble']
            self.assertEqual(speech[1] + height + 8, owner[1])
            self.assertEqual(positions['activity_cards'][1] + 110 + 8, speech[1])
            self.assertEqual(positions['toolbar'][1], owner[1] + owner[3] + 8)

    def test_interaction_speech_at_top_attaches_below_pet(self):
        owner = (400, 20, 256, 256)
        positions = vertical_group_layout(owner, (0, 0, 1200, 900), (256, 70), (280, 46), (304, 110))
        self.assertEqual(positions['bubble'][1], owner[1] + owner[3] + 8)
        self.assertEqual(positions['toolbar'][1], positions['bubble'][1] + 70 + 8)

    def test_content_flags_distinguish_task_notices_from_interactions_and_quota(self):
        pet = SimpleNamespace(task_active=True, bubble=SimpleNamespace(is_task_bubble=False, is_task_progress=False))
        self.assertFalse(is_task_notification(pet))
        pet.bubble.is_task_bubble = True
        self.assertTrue(is_task_notification(pet))
        pet._quota_speech_active = True
        self.assertFalse(is_task_notification(pet))
        pet._quota_speech_active = False
        pet.bubble.is_task_bubble = False
        pet.bubble.is_task_progress = True
        self.assertTrue(is_task_notification(pet))

    def test_all_corners_keep_toolbar_cards_and_character_separate(self):
        for area in ((0, 0, 1024, 768), (-1920, -1080, 1920, 1080), (0, 0, 800, 700)):
            x, y, w, h = area
            for px in (x, x + w - 256):
                for py in (y, y + h - 256):
                    owner = (px, py, 256, 256)
                    bar = place_vertical_window((280, 46), owner, area)
                    self.assertIsNotNone(bar)
                    bar = (*bar, 280, 46)
                    card = place_vertical_window((320, 126), owner, area, (bar,), preferred_side='above')
                    self.assertIsNotNone(card)
                    card = (*card, 320, 126)
                    self.assertFalse(intersects(bar, card))
                    self.assertFalse(intersects(bar, owner))
                    self.assertFalse(intersects(card, owner))
                    for rect in (bar, card):
                        self.assertTrue(rect[1] + rect[3] <= py or rect[1] >= py + 256)

    def test_obstacle_edges_provide_more_than_four_anchored_candidates(self):
        owner = (60, 0, 256, 256)
        bar = (48, 262, 280, 46)
        point = place_window((320, 126), owner, (0, 0, 400, 700), (bar,))
        self.assertIsNotNone(point)
        self.assertFalse(intersects((*point, 320, 126), bar))

    def test_impossible_window_is_reported_instead_of_covering_character(self):
        self.assertIsNone(place_window((300, 300), (0, 0, 300, 300), (0, 0, 300, 300)))
