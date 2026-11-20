import unittest
from unittest.mock import patch

from scripts.screenshots import render


class CapturePreparationTests(unittest.TestCase):
    def test_live_widget_prefetches_snapshot_and_resets_readiness(self):
        payload = {"stats": {"providers": []}, "live": {"chats": []}}
        capture = {"payload": {"old": True}, "ready": True}
        with patch.object(render, "capture_payload", return_value=payload) as fetch:
            is_live_widget = render.prepare_capture(capture, "tree", "http://127.0.0.1:8765")

        self.assertTrue(is_live_widget)
        fetch.assert_called_once_with("http://127.0.0.1:8765")
        self.assertIs(capture["payload"], payload)
        self.assertFalse(capture["ready"])

    def test_fixtures_and_non_widget_modes_do_not_prefetch(self):
        for mode, live in (("tree", None), ("insights-30d", "http://127.0.0.1:8765")):
            with self.subTest(mode=mode):
                capture = {"payload": {"old": True}, "ready": True}
                with patch.object(render, "capture_payload") as fetch:
                    is_live_widget = render.prepare_capture(capture, mode, live)

                self.assertFalse(is_live_widget)
                fetch.assert_not_called()
                self.assertIsNone(capture["payload"])
                self.assertFalse(capture["ready"])


if __name__ == "__main__":
    unittest.main()
