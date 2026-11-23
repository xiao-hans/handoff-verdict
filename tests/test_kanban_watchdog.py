import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path

HERMES_HOME = Path(os.environ.get("HERMES_HOME", str(Path.home() / "AppData" / "Local" / "hermes")))
WATCHDOG = HERMES_HOME / "scripts" / "kanban_watchdog.py"


def load_watchdog(root: Path):
    spec = importlib.util.spec_from_file_location("kanban_watchdog_under_test", WATCHDOG)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.STATE_FILE = root / "state.json"
    module.LOG_FILE = root / "watchdog.log"
    return module


class WatchdogFilterTest(unittest.TestCase):
    def test_handoff_tracker_cards_are_not_alerts(self):
        with tempfile.TemporaryDirectory() as raw:
            watchdog = load_watchdog(Path(raw))
            watchdog.check_tasks([
                {"id": "mirror", "title": "[handoff] Pi", "assignee": "handoff-tracker",
                 "status": "blocked"},
                {"id": "real", "title": "Real task", "assignee": "worker",
                 "status": "blocked"},
            ])
            log = watchdog.LOG_FILE.read_text(encoding="utf-8")
            state = json.loads(watchdog.STATE_FILE.read_text(encoding="utf-8"))
            self.assertNotIn("mirror", log)
            self.assertIn("real", log)
            self.assertNotIn("mirror", state["alerts"])
            self.assertIn("real", state["alerts"])


if __name__ == "__main__":
    unittest.main()
