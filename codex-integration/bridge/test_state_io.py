"""Bounded file-sharing-failure regressions; entirely isolated metadata fixtures."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import bridge_pet
import bridge_supervisor
import native_visibility as visibility


class StateIOTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="bailong-transient-state-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.path = self.base / "codex-state.json"
        self.state = {"running": True, "overlayOpen": True, "pid": 123,
                      "selectionKnown": True, "selectedAvatarId": "custom:bailong-full",
                      "nativePetVisible": True, "overlayHwnd": 456, "appStartedAt": 9000}
        self.reader = visibility.BoundedStateFile(validator=visibility.valid_observer_state)
        self.write(self.path, self.state)
        self.gen = {"generation": "first", "minimumMtime": 9990.0}
        self.write(self.base / visibility.GENERATION_NAME, self.gen)
        self.patches = [
            mock.patch.object(visibility.time, "time", return_value=10000.0),
            mock.patch.object(visibility.time, "monotonic", return_value=50.0),
            mock.patch.object(visibility.time, "sleep"),
            mock.patch.object(visibility, "_GENERATION_READER", visibility.BoundedStateFile(max_bytes=1024, max_age=None)),
            mock.patch.object(visibility, "_MASK_READER", visibility.BoundedStateFile(max_bytes=16384, max_age=6)),
            mock.patch.object(bridge_pet, "DIRECTORY", self.base),
            mock.patch.object(bridge_pet, "STATE_FILE", self.path),
            mock.patch.object(bridge_pet, "_STATE_READER", visibility.BoundedStateFile(validator=visibility.valid_observer_state)),
            mock.patch.object(bridge_pet, "_LAST_SELECTION", None),
            mock.patch.object(bridge_pet, "_LAST_SELECTION_AT", None),
            mock.patch.object(bridge_pet, "_LAST_SELECTION_CONTEXT", None),
            mock.patch.object(bridge_supervisor, "_OBSERVATION_READER", visibility.BoundedStateFile(validator=visibility.valid_observer_state)),
        ]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)

    def write(self, path, value, stamp=10000.0):
        path.write_text(json.dumps(value), encoding="utf-8")
        os.utime(path, (stamp, stamp))

    def lock(self, path):
        original = Path.open
        def locked(candidate, *args, **kwargs):
            if candidate == path:
                raise PermissionError("simulated atomic replace sharing violation")
            return original(candidate, *args, **kwargs)
        return mock.patch.object(Path, "open", locked)

    def test_permission_error_retries_and_recovers_without_fallback(self):
        original = Path.open
        attempts = []
        def transient(path, *args, **kwargs):
            attempts.append(1)
            if len(attempts) < 3:
                raise PermissionError("sharing violation")
            return original(path, *args, **kwargs)
        with mock.patch.object(Path, "open", transient):
            result = self.reader.read(self.path, now=10000, monotonic_now=50)
        self.assertEqual(result, self.state)
        self.assertEqual(len(attempts), 3)
        self.assertFalse(self.reader.reused)

    def test_continuous_io_failure_reuses_only_one_second_and_recovers(self):
        self.reader.read(self.path, now=10000, monotonic_now=50)
        with self.lock(self.path):
            for elapsed in (.1, .4, .99):
                self.assertEqual(self.reader.read(self.path, now=10000 + elapsed,
                                 monotonic_now=50 + elapsed), self.state)
                self.assertTrue(self.reader.reused)
            self.assertIsNone(self.reader.read(self.path, now=10001.01, monotonic_now=51.01))
        self.assertEqual(self.reader.read(self.path, now=10001.1, monotonic_now=51.1), self.state)

    def test_stale_file_is_never_extended_by_read_cache(self):
        self.write(self.path, self.state, stamp=9985.1)
        self.reader.read(self.path, now=10000, monotonic_now=50)
        with self.lock(self.path):
            self.assertIsNone(self.reader.read(self.path, now=10000.2, monotonic_now=50.2))

    def test_generation_and_minimum_time_invalidate_even_during_io_failure(self):
        self.reader.read(self.path, now=10000, monotonic_now=50, generation="old")
        with self.lock(self.path):
            self.assertIsNone(self.reader.read(self.path, now=10000.1, monotonic_now=50.1, generation="new"))
        self.reader.read(self.path, now=10000, monotonic_now=50, generation="new")
        with self.lock(self.path):
            self.assertIsNone(self.reader.read(self.path, now=10000.1, monotonic_now=50.1,
                                             generation="new", minimum_mtime=10000.05))

    def test_explicit_false_selection_change_and_invalid_data_never_reuse_old_value(self):
        for change in ({"overlayOpen": False}, {"selectedAvatarId": "custom:other"},
                       {"running": False}, {"nativePetVisible": False}):
            self.reader.read(self.path, now=10000, monotonic_now=50)
            updated = dict(self.state, **change)
            self.write(self.path, updated)
            self.assertEqual(self.reader.read(self.path, now=10000.1, monotonic_now=50.1), updated)
        self.path.write_text("{truncated", encoding="utf-8")
        os.utime(self.path, (10000, 10000))
        self.assertIsNone(self.reader.read(self.path, now=10000.2, monotonic_now=50.2))
        self.assertEqual(self.reader.reason, "invalid")

    def test_supervisor_io_cache_preserves_original_verification_time(self):
        first = bridge_supervisor.read_observation(self.path, 10000, generation="one", monotonic_now=50)
        with self.lock(self.path):
            second = bridge_supervisor.read_observation(self.path, 10000.2, generation="one", monotonic_now=50.2)
            self.assertEqual(first, second)
            self.assertEqual(second.verified_at, 50)
            self.assertIsNone(bridge_supervisor.read_observation(self.path, 10000.3, generation="two", monotonic_now=50.3))

    def test_worker_io_cache_and_explicit_hide(self):
        first = bridge_pet.read_state(now=10000, monotonic_now=50)
        self.assertEqual(first["_observerGeneration"], "first")
        with self.lock(self.path):
            self.assertEqual(bridge_pet.read_state(now=10000.2, monotonic_now=50.2), first)
            self.assertIsNone(bridge_pet.read_state(now=10001.01, monotonic_now=51.01))
        self.write(self.path, dict(self.state, overlayOpen=False))
        self.assertFalse(bridge_pet.read_state(now=10001.1, monotonic_now=51.1)["overlayOpen"])

    def test_worker_unknown_selection_is_bounded_but_false_or_new_id_wins(self):
        first = bridge_pet.read_state(now=10000, monotonic_now=50)
        unknown = dict(self.state, selectionKnown=False, selectedAvatarId=None, nativePetVisible=None)
        self.write(self.path, unknown)
        self.assertTrue(bridge_pet.read_state(now=10000.2, monotonic_now=50.2)["selectionKnown"])
        self.assertFalse(bridge_pet.read_state(now=10001.01, monotonic_now=51.01)["selectionKnown"])
        for change in ({"overlayOpen": False}, {"selectedAvatarId": "custom:other"}, {"nativePetVisible": False}):
            self.write(self.path, self.state)
            bridge_pet.read_state(now=10000, monotonic_now=50)
            self.write(self.path, dict(unknown, **change))
            value = bridge_pet.read_state(now=10000.1, monotonic_now=50.1)
            self.assertFalse(value["selectionKnown"])

    def test_worker_new_generation_never_reuses_previous_state(self):
        bridge_pet.read_state(now=10000, monotonic_now=50)
        self.write(self.base / visibility.GENERATION_NAME, {"generation": "second", "minimumMtime": 10000.1})
        with self.lock(self.path):
            self.assertIsNone(bridge_pet.read_state(now=10000.2, monotonic_now=50.2))

    def test_mask_gate_io_cache_false_and_generation_guards(self):
        state = dict(self.state, _observerGeneration="first")
        path = self.base / visibility.STATE_NAME
        report = {"at": 10000.0, "observerGeneration": "first",
                  "mask": {"pid": 123, "hwnd": 456, "masked": True}}
        self.write(path, report)
        self.assertTrue(visibility.mask_ready(self.base, state))
        with self.lock(path):
            with mock.patch.object(visibility.time, "monotonic", return_value=50.2):
                self.assertTrue(visibility.mask_ready(self.base, state))
            with mock.patch.object(visibility.time, "monotonic", return_value=51.01):
                self.assertFalse(visibility.mask_ready(self.base, state))
        self.write(path, report)
        self.assertTrue(visibility.mask_ready(self.base, state))
        with self.lock(path):
            self.assertFalse(visibility.mask_ready(self.base, dict(state, _observerGeneration="second")))
        self.write(path, {**report, "mask": {"pid": 123, "hwnd": 456, "masked": False}})
        self.assertFalse(visibility.mask_ready(self.base, state))


if __name__ == "__main__":
    unittest.main()
