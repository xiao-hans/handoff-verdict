"""Offline tests for kanban_handoff_sync.py and the router hook.

Everything runs against fixture ledgers in a temp dir and a fake board layer that
models the verified Hermes kanban semantics. The real board database, the real
cron jobs and the pi-handoff ledger are never touched.
"""

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import kanban_handoff_sync as sync  # noqa: E402

NOW = 1_800_000_000          # fixed clock: every activity assertion is relative
STALE = NOW - 6 * 3600       # older than the 15 minute activity window
ARTIFACTS = "artifacts"


# --------------------------------------------------------------------------
# Fixture builders
# --------------------------------------------------------------------------


def write_json(path, payload):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)


def make_ledger(root, tasks, events=(), acks=(), artifacts=None):
    """Build a pi-handoff shaped ledger under ``root``.

    ``tasks``: registry entries keyed by task id. ``events``: outbox events.
    ``acks``: verify-acks.jsonl records. ``artifacts``: {abs path: payload|None}
    written outside the ledger root (exit receipts and reports live in workdirs;
    ``None`` payload means "write a report file").
    """
    os.makedirs(os.path.join(root, "outbox", "events"), exist_ok=True)
    os.makedirs(os.path.join(root, "tasks"), exist_ok=True)
    write_json(os.path.join(root, "registry.json"), {"tasks": tasks})
    for item in events:
        write_json(os.path.join(root, "outbox", "events",
                                f"{item['event_id']}.json"), item)
    lines = "".join(json.dumps(ack, ensure_ascii=False) + "\n" for ack in acks)
    with open(os.path.join(root, "outbox", "verify-acks.jsonl"), "w",
              encoding="utf-8") as handle:
        handle.write(lines)
    for task_id, item in tasks.items():
        state = dict(item.get("_state") or {})
        state.setdefault("last_activity_at", item.get("last_activity_at"))
        write_json(os.path.join(root, "tasks", task_id, "state.json"), state)
    for path, payload in (artifacts or {}).items():
        if payload is None:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("# report\n" + "x" * 400)
        else:
            write_json(path, payload)
    return root


def task(task_id, **kwargs):
    base = {
        "task_id": task_id,
        "goal": f"goal of {task_id}",
        "agent_name": kwargs.pop("agent", "Pi"),
        "source": "exit",
        "workdir": "<WORKSPACE>/demo",
        "report_path": None,
        "registered_at": STALE,
        "terminal_event_id": None,
        "pid": None,
        "exit_code_path": None,
        "exit_code_success": 0,   # the config default, never evidence
    }
    base.update(kwargs)
    return base


def event(event_id, task_id, status="report_ready", verdict="accepted",
          reason="exitcode receipt exit_code=0", created_at=STALE, **extra):
    payload = {
        "event_id": event_id,
        "task_id": task_id,
        "status": status,
        "verdict": verdict,
        "reason": reason,
        "created_at": created_at,
        "receipt_path": f"<WORKSPACE>/demo/receipts/{event_id}.json",
    }
    payload.update(extra)
    return payload


def ack(event_id, verdict=None, note="独立核验通过：产物与收据一致", at=STALE):
    record = {"at": at, "claimer": "hermes-verify", "event_id": event_id, "note": note}
    if verdict is not None:
        record["verdict"] = verdict
    return record


def tree_fingerprint(root):
    digest = hashlib.sha256()
    for base, dirs, files in os.walk(root):
        dirs.sort()
        for name in sorted(files):
            path = os.path.join(base, name)
            digest.update(os.path.relpath(path, root).encode("utf-8"))
            with open(path, "rb") as handle:
                digest.update(handle.read())
    return digest.hexdigest()


# --------------------------------------------------------------------------
# Fake board layer: models the kanban semantics verified against hermes_cli
# --------------------------------------------------------------------------


class FakeCard:
    def __init__(self, cid, title, body, status, idempotency_key, created_by):
        self.id = cid
        self.title = title
        self.body = body
        self.status = status
        self.idempotency_key = idempotency_key
        self.created_by = created_by


class FakeKB:
    """In-memory stand-in for ``hermes_cli.kanban_db``.

    Semantics copied from the verified production probe:
      * ``initial_status="running"`` with no parents lands ``ready``;
      * an idempotency-key hit returns the old id and does NOT update title/body;
      * ``review`` refuses block, ``done`` refuses everything;
      * ``review`` -> ``ready`` only through ``reopen_review_task``.
    """

    def __init__(self):
        self.cards = {}
        self.comments = {}
        self.calls = []
        self._next = 0

    def new_id(self):
        self._next += 1
        return f"t{self._next:04d}"

    # -- reads -----------------------------------------------------------
    def list_tasks(self, conn, include_archived=False, **kwargs):
        return [card for card in self.cards.values()
                if include_archived or card.status != "archived"]

    def get_task(self, conn, task_id):
        return self.cards.get(task_id)

    def list_comments(self, conn, task_id):
        return [types.SimpleNamespace(body=text)
                for text in self.comments.get(task_id, [])]

    # -- writes ----------------------------------------------------------
    def create_task(self, conn, *, title, body=None, assignee=None, created_by=None,
                    idempotency_key=None, initial_status="running", parents=(),
                    **kwargs):
        self.calls.append(("create_task", idempotency_key, initial_status))
        if idempotency_key:
            for card in self.cards.values():
                if card.idempotency_key == idempotency_key:
                    return card.id  # dedup: title/body are deliberately NOT updated
        status = "blocked" if initial_status == "blocked" else "ready"
        cid = self.new_id()
        self.cards[cid] = FakeCard(cid, title, body or "", status, idempotency_key,
                                   created_by)
        return cid

    def edit_task(self, conn, task_id, *, title=None, body=None, **kwargs):
        self.calls.append(("edit_task", task_id))
        card = self.cards[task_id]
        if title is not None:
            card.title = title
        if body is not None:
            card.body = body
        return True

    def add_comment(self, conn, task_id, author, body):
        self.calls.append(("add_comment", task_id))
        self.comments.setdefault(task_id, []).append(body)
        return True

    def complete_task(self, conn, task_id, *, result=None, summary=None, **kwargs):
        self.calls.append(("complete_task", task_id))
        card = self.cards[task_id]
        if card.status == "done":
            return False
        card.status = "done"
        return True

    def request_review(self, conn, task_id, *, summary=None, **kwargs):
        self.calls.append(("request_review", task_id))
        card = self.cards[task_id]
        if card.status not in ("ready", "running"):
            return False
        card.status = "review"
        return True

    def block_task(self, conn, task_id, *, reason=None, **kwargs):
        self.calls.append(("block_task", task_id))
        card = self.cards[task_id]
        if card.status in ("review", "done"):
            return False
        card.status = "blocked"
        return True

    def unblock_task(self, conn, task_id):
        self.calls.append(("unblock_task", task_id))
        card = self.cards[task_id]
        if card.status != "blocked":
            return False
        card.status = "ready"
        return True

    def reopen_review_task(self, conn, task_id):
        self.calls.append(("reopen_review_task", task_id))
        card = self.cards[task_id]
        if card.status != "review":
            return False
        card.status = "ready"
        return True

    # -- forbidden for a projector (any call fails the test) -------------
    def archive_task(self, *args, **kwargs):
        raise AssertionError("the projector must never archive a card")

    def delete_archived_task(self, *args, **kwargs):
        raise AssertionError("the projector must never delete a card")

    def claim_task(self, *args, **kwargs):
        raise AssertionError("the projector must never claim a card")

    def names(self):
        return [entry[0] for entry in self.calls]


@contextlib.contextmanager
def fake_hermes_modules(kb):
    """Install ``kb`` as ``hermes_cli.kanban_db`` for the duration of a block."""
    connection = object()
    package = types.ModuleType("hermes_cli")
    db_module = types.ModuleType("hermes_cli.kanban_db")
    connect_module = types.ModuleType("hermes_cli.kanban_db_connect")
    for name in dir(kb):
        if not name.startswith("__"):
            setattr(db_module, name, getattr(kb, name))

    @contextlib.contextmanager
    def connect_closing(db_path=None, *, board=None):
        yield connection

    connect_module.__dict__["connect_closing"] = connect_closing
    package.__dict__["kanban_db"] = db_module
    package.__dict__["kanban_db_connect"] = connect_module
    names = ("hermes_cli", "hermes_cli.kanban_db", "hermes_cli.kanban_db_connect")
    saved = {name: sys.modules.get(name) for name in names}
    sys.modules["hermes_cli"] = package
    sys.modules["hermes_cli.kanban_db"] = db_module
    sys.modules["hermes_cli.kanban_db_connect"] = connect_module
    try:
        yield kb
    finally:
        for name, value in saved.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


class SyncTestCase(unittest.TestCase):
    """Shared temp-dir plumbing."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="handoff-sync-test-")
        self.addCleanup(self.tmp.cleanup)
        base = self.tmp.name
        self.root = os.path.join(base, "ledger")
        self.state_dir = os.path.join(base, "state")
        self.log_path = os.path.join(base, "logs", "sync.log")
        self.board_path = os.path.join(base, "board.db")

    def artifact_path(self, name, code=0):
        """Absolute path of a fixture exit receipt (written by ``artifacts=``)."""
        return os.path.join(self.tmp.name, ARTIFACTS, f"{name}-{code}.json")

    def artifact(self, name, code=0):
        return {self.artifact_path(name, code): {"exit_code": code}}

    def board(self, kb):
        """HermesBoard wired to the fake module layer (closed at teardown)."""
        board = sync.HermesBoard(self.tmp.name, self.board_path, sync.NullLogger())
        context = fake_hermes_modules(kb)
        context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        board.open()
        self.addCleanup(board.close)
        return board

    def apply_plan(self, kb, root, now=NOW):
        board = self.board(kb)
        ledger = sync.load_ledger(root)
        cards, comments = board.snapshot()
        plan, _skipped = sync.build_plan(ledger, cards, comments, now, root)
        owned = {card.id for card in cards.values()}
        tally = {"created": 0, "transitioned": 0, "refreshed": 0, "commented": 0,
                 "skipped": 0}
        for action in plan:
            tag = board.apply(action, owned)
            if tag in tally:
                tally[tag] += 1
            else:
                tally["skipped"] += 1
        return tally, board


# --------------------------------------------------------------------------
# Evidence resolution
# --------------------------------------------------------------------------


class EvidenceTest(SyncTestCase):

    def test_pointer_is_preferred_over_the_newest_event(self):
        tasks = {"a": task("a", terminal_event_id="evt-old")}
        events = [event("evt-old", "a", status="report_ready"),
                  event("evt-new", "a", status="failed", created_at=STALE + 10)]
        root = make_ledger(self.root, tasks, events)
        ledger = sync.load_ledger(root)
        got = sync.resolve_terminal_event(ledger["tasks"]["a"], ledger)
        assert got is not None
        self.assertEqual(got["event_id"], "evt-old")

    def test_null_pointer_falls_back_to_the_newest_event(self):
        tasks = {"a": task("a", terminal_event_id=None)}
        events = [event("evt-1", "a", status="failed", created_at=STALE),
                  event("evt-2", "a", status="report_ready", created_at=STALE + 60)]
        root = make_ledger(self.root, tasks, events)
        ledger = sync.load_ledger(root)
        got = sync.resolve_terminal_event(ledger["tasks"]["a"], ledger)
        assert got is not None
        self.assertEqual(got["event_id"], "evt-2")

    def test_dangling_pointer_is_reported_as_missing(self):
        tasks = {"a": task("a", terminal_event_id="evt-gone")}
        root = make_ledger(self.root, tasks)
        ledger = sync.load_ledger(root)
        got = sync.resolve_terminal_event(ledger["tasks"]["a"], ledger)
        assert got is not None
        self.assertTrue(got["missing"])
        decision = sync.classify("a", ledger["tasks"]["a"], ledger, NOW, root)
        self.assertEqual(decision["rule"], "R8")

    def test_registry_exit_code_success_is_not_evidence(self):
        """exit_code_success=0 with a missing artifact stays non-terminal."""
        tasks = {"a": task("a", exit_code_path=self.artifact_path("absent", 0),
                           exit_code_success=0)}
        root = make_ledger(self.root, tasks)
        ledger = sync.load_ledger(root)
        decision = sync.classify("a", ledger["tasks"]["a"], ledger, NOW, root)
        self.assertEqual((decision["state"], decision["rule"]), ("ready", "R8"))
        self.assertIn("missing", decision["exit_receipt"]["error"])

    def test_exit_artifact_beats_the_registry_claim(self):
        tasks = {"a": task("a", exit_code_path=self.artifact_path("bad", 1),
                           exit_code_success=0)}
        root = make_ledger(self.root, tasks, artifacts=self.artifact("bad", 1))
        ledger = sync.load_ledger(root)
        decision = sync.classify("a", ledger["tasks"]["a"], ledger, NOW, root)
        self.assertEqual((decision["state"], decision["rule"]), ("blocked", "R3"))


# --------------------------------------------------------------------------
# The state mapping
# --------------------------------------------------------------------------


class MappingTest(SyncTestCase):

    def classify(self, tasks, events=(), acks=(), artifacts=None, key="a", now=NOW):
        root = make_ledger(self.root, tasks, events, acks, artifacts)
        ledger = sync.load_ledger(root)
        return sync.classify(key, ledger["tasks"][key], ledger, now, root)

    def test_r1_accepted_ack_is_done(self):
        decision = self.classify(
            {"a": task("a", terminal_event_id="evt-1")},
            [event("evt-1", "a")], [ack("evt-1", "accepted")])
        self.assertEqual((decision["state"], decision["rule"], decision["board_state"]),
                         ("done", "R1", "done"))

    def test_r1_accepts_a_note_only_ack(self):
        """Older acks carry no verdict field: the note wording decides."""
        decision = self.classify(
            {"a": task("a", terminal_event_id="evt-1")},
            [event("evt-1", "a")], [ack("evt-1", None, "产物核验通过：docs/spec/12 已落盘")])
        self.assertEqual(decision["rule"], "R1")

    def test_r2_rejecting_note_blocks(self):
        decision = self.classify(
            {"a": task("a", terminal_event_id="evt-1")},
            [event("evt-1", "a")], [ack("evt-1", None, "核验不通过：报告与收据不一致")])
        self.assertEqual((decision["state"], decision["rule"]), ("blocked", "R2"))

    def test_r2_rejected_verdict_beats_a_zero_exit(self):
        decision = self.classify(
            {"a": task("a", terminal_event_id="evt-1",
                       exit_code_path=self.artifact_path("ok", 0))},
            [event("evt-1", "a")], [ack("evt-1", "blocked", "产物缺失，验收不通过")],
            artifacts=self.artifact("ok", 0))
        self.assertEqual(decision["rule"], "R2")

    def test_r3_nonzero_exit_blocks(self):
        decision = self.classify(
            {"a": task("a", exit_code_path=self.artifact_path("nine", 9))},
            artifacts=self.artifact("nine", 9))
        self.assertEqual((decision["state"], decision["rule"]), ("blocked", "R3"))
        self.assertIn("exit_code=9", decision["reason"])

    def test_r4_failed_event_blocks_without_any_exit_artifact(self):
        decision = self.classify(
            {"a": task("a", terminal_event_id="evt-1")},
            [event("evt-1", "a", status="stalled", verdict="accepted",
                   reason="no session records observed")])
        self.assertEqual((decision["state"], decision["rule"]), ("blocked", "R4"))

    def test_r4_covers_stopped_unfinished(self):
        decision = self.classify(
            {"a": task("a", terminal_event_id="evt-1")},
            [event("evt-1", "a", status="stopped_unfinished")])
        self.assertEqual(decision["rule"], "R4")

    def test_r5_zero_exit_without_acceptance_is_review(self):
        decision = self.classify(
            {"a": task("a", terminal_event_id="evt-1",
                       exit_code_path=self.artifact_path("ok", 0))},
            [event("evt-1", "a")], artifacts=self.artifact("ok", 0))
        self.assertEqual((decision["state"], decision["rule"], decision["board_state"]),
                         ("review", "R5", "review"))

    def test_r6_report_ready_without_exit_artifact_is_review(self):
        decision = self.classify(
            {"a": task("a", terminal_event_id="evt-1", source="claude")},
            [event("evt-1", "a", reason="claude result subtype=success")])
        self.assertEqual((decision["state"], decision["rule"]), ("review", "R6"))

    def test_r7_recent_activity_is_running(self):
        decision = self.classify({"a": task("a", last_activity_at=NOW - 60)})
        self.assertEqual((decision["state"], decision["rule"]), ("running", "R7"))

    def test_r7_live_pid_is_running_even_when_the_log_is_stale(self):
        decision = self.classify({"a": task("a", pid=os.getpid())},
                                 now=NOW)
        self.assertEqual(decision["rule"], "R7")

    def test_r8_idle_without_evidence_is_ready(self):
        decision = self.classify({"a": task("a", last_activity_at=STALE)})
        self.assertEqual((decision["state"], decision["rule"]), ("ready", "R8"))

    def test_scanner_bookkeeping_is_not_worker_activity(self):
        """last_tick_at / last_scan_at / the state.json mtime move every tick.

        They measure the ledger scanner, not the worker, so they must never
        promote a card to running.
        """
        root = make_ledger(self.root, {"a": task(
            "a", _state={"last_tick_at": NOW - 5, "last_scan_at": NOW - 5})})
        state_path = os.path.join(root, "tasks", "a", "state.json")
        os.utime(state_path, (NOW - 5, NOW - 5))   # touched seconds ago
        ledger = sync.load_ledger(root)
        signal = sync.activity_signal(ledger["tasks"]["a"],
                                      ledger["states"].get("a"), NOW, root)
        self.assertIsNone(signal["last_activity_at"])
        self.assertFalse(signal["active"])
        decision = sync.classify("a", ledger["tasks"]["a"], ledger, NOW, root)
        self.assertEqual(decision["rule"], "R8")
        self.assertIn("no stream activity stamp", decision["reason"])

    def test_activity_window_boundary(self):
        inside = self.classify({"a": task("a", last_activity_at=NOW - 900)}, now=NOW)
        outside = self.classify({"a": task("a", last_activity_at=NOW - 901)}, now=NOW)
        self.assertEqual(inside["rule"], "R7")
        self.assertEqual(outside["rule"], "R8")

    def test_running_never_reaches_the_board(self):
        """Hermes' dispatcher requeues claimless running cards every tick."""
        root = make_ledger(self.root, {"a": task("a", last_activity_at=NOW - 30)})
        plan, _ = sync.build_plan(sync.load_ledger(root), {}, {}, NOW, root)
        create = next(item for item in plan if item["kind"] == "create")
        self.assertEqual(create["ledger_state"], "running")
        self.assertEqual(create["state"], "ready")
        self.assertIn("ACTIVE", create["comment"])

    def test_acceptance_outranks_a_nonzero_exit(self):
        decision = self.classify(
            {"a": task("a", terminal_event_id="evt-1",
                       exit_code_path=self.artifact_path("seven", 7))},
            [event("evt-1", "a")], [ack("evt-1", "accepted")],
            artifacts=self.artifact("seven", 7))
        self.assertEqual(decision["rule"], "R1")


# --------------------------------------------------------------------------
# Planning
# --------------------------------------------------------------------------


class PlanTest(SyncTestCase):

    def test_first_run_backfills_every_registry_task(self):
        root = make_ledger(self.root, {"a": task("a", terminal_event_id="evt-a"),
                                       "b": task("b")},
                           [event("evt-a", "a")])
        plan, _ = sync.build_plan(sync.load_ledger(root), {}, {}, NOW, root)
        self.assertEqual(sorted(item["task_id"] for item in plan if
                                item["kind"] == "create"), ["a", "b"])
        for item in plan:
            self.assertTrue(item["key"].startswith("handoff:"))

    def test_card_content_requirements(self):
        root = make_ledger(self.root, {"a": task(
            "a", agent="Cursor Agent CLI", terminal_event_id="evt-a",
            report_path="D:/demo/REPORT.md", workdir="D:/demo/wd")},
            [event("evt-a", "a")])
        plan, _ = sync.build_plan(sync.load_ledger(root), {}, {}, NOW, root)
        create = next(item for item in plan if item["kind"] == "create")
        self.assertIn("Cursor Agent CLI", create["title"])
        self.assertIn("goal of a", create["title"])
        for needle in ("workdir: D:/demo/wd", "report_path: D:/demo/REPORT.md",
                       f"registered_at: {STALE}", "terminal_event_id: evt-a"):
            self.assertIn(needle, create["body"], needle)

    def test_title_falls_back_to_source_when_agent_is_absent(self):
        root = make_ledger(self.root, {"a": {"task_id": "a", "goal": "g",
                                             "source": "pi"}})
        plan, _ = sync.build_plan(sync.load_ledger(root), {}, {}, NOW, root)
        self.assertIn("[handoff] pi", next(item for item in plan
                                           if item["kind"] == "create")["title"])

    def test_transition_only_when_the_column_differs(self):
        root = make_ledger(self.root, {"a": task("a", terminal_event_id="evt-a")},
                           [event("evt-a", "a")])
        ledger = sync.load_ledger(root)
        card = sync.Card("t1", "handoff:a", "ready")
        plan, _ = sync.build_plan(ledger, {"handoff:a": card}, {"t1": []}, NOW, root)
        self.assertEqual({item["kind"] for item in plan}, {"transition", "comment"})
        transition = next(item for item in plan if item["kind"] == "transition")
        self.assertEqual((transition["from"], transition["to"]), ("ready", "review"))

    def test_no_action_when_the_column_already_matches(self):
        root = make_ledger(self.root, {"a": task("a", terminal_event_id="evt-a")},
                           [event("evt-a", "a")])
        ledger = sync.load_ledger(root)
        decision = sync.classify("a", ledger["tasks"]["a"], ledger, NOW, root)
        card = sync.Card("t1", "handoff:a", decision["board_state"],
                         sync.card_title(ledger["tasks"]["a"]),
                         sync.card_body(decision, root))
        token = sync.evidence_token(decision)
        plan, _ = sync.build_plan(ledger, {"handoff:a": card},
                                  {"t1": [f"{sync.COMMENT_MARKER}{token}] done"]},
                                  NOW, root)
        self.assertEqual(plan, [])

    def test_archived_card_is_skipped_and_not_recreated(self):
        root = make_ledger(self.root, {"a": task("a")})
        ledger = sync.load_ledger(root)
        card = sync.Card("t1", "handoff:a", "archived")
        plan, skipped = sync.build_plan(ledger, {"handoff:a": card}, {"t1": []}, NOW,
                                        root)
        self.assertEqual(plan, [])
        self.assertEqual(skipped[0][0], "a")
        self.assertIn("archived", skipped[0][1])

    def test_card_in_a_foreign_column_is_skipped_not_forced(self):
        root = make_ledger(self.root, {"a": task("a")})
        ledger = sync.load_ledger(root)
        card = sync.Card("t1", "handoff:a", "todo")
        plan, skipped = sync.build_plan(ledger, {"handoff:a": card}, {"t1": []}, NOW,
                                        root)
        self.assertEqual([item["kind"] for item in plan], ["comment"])
        self.assertEqual(skipped[0][0], "a")
        self.assertIn("todo", skipped[0][1])

    def test_body_drift_is_a_refresh_not_a_transition(self):
        root = make_ledger(self.root, {"a": task("a")})
        ledger = sync.load_ledger(root)
        card = sync.Card("t1", "handoff:a", "ready", title="[handoff] Pi · old",
                         body="stale body")
        plan, _ = sync.build_plan(ledger, {"handoff:a": card}, {"t1": []}, NOW, root)
        self.assertEqual({item["kind"] for item in plan}, {"refresh", "comment"})

    def test_comment_token_needs_no_new_comment_for_the_same_evidence(self):
        root = make_ledger(self.root, {"a": task("a")})
        ledger = sync.load_ledger(root)
        decision = sync.classify("a", ledger["tasks"]["a"], ledger, NOW, root)
        token = sync.evidence_token(decision)
        card = sync.Card("t1", "handoff:a", "ready", sync.card_title({"task_id": "a"}),
                         sync.card_body(decision, root))
        plan, _ = sync.build_plan(ledger, {"handoff:a": card},
                                  {"t1": [f"… {sync.COMMENT_MARKER}{token}] …"]}, NOW,
                                  root)
        self.assertEqual([item["kind"] for item in plan], ["refresh"])


# --------------------------------------------------------------------------
# Applying to the board
# --------------------------------------------------------------------------


class ApplyTest(SyncTestCase):

    def test_idempotent_rerun_produces_no_second_change(self):
        root = make_ledger(self.root, {
            "a": task("a", terminal_event_id="evt-a",
                      exit_code_path=self.artifact_path("ok", 0)),
            "b": task("b", last_activity_at=STALE)},
            [event("evt-a", "a")], artifacts=self.artifact("ok", 0))
        kb = FakeKB()
        first, _ = self.apply_plan(kb, root)
        self.assertEqual(first["created"], 2)
        self.assertEqual(sorted(len(v) for v in kb.comments.values()), [1, 1])
        second, board = self.apply_plan(kb, root)
        self.assertEqual(sum(second.values()), 0, second)
        self.assertEqual(len(board.snapshot()[0]), 2)

    def test_ten_identical_reruns_write_one_comment_per_card(self):
        root = make_ledger(self.root, {"a": task("a"), "b": task("b")})
        kb = FakeKB()
        for _ in range(10):
            self.apply_plan(kb, root)
        self.assertEqual([len(v) for v in kb.comments.values()], [1, 1])
        self.assertEqual(kb.names().count("create_task"), 2)

    def test_evidence_change_adds_exactly_one_new_comment(self):
        """No evidence -> exit artifact: the card gains one comment, not two."""
        root = make_ledger(self.root, {"a": task("a")})
        kb = FakeKB()
        self.apply_plan(kb, root)
        card_id = next(iter(kb.cards))
        make_ledger(root, {"a": task("a", exit_code_path=self.artifact_path("ok", 0))},
                    artifacts=self.artifact("ok", 0))
        self.apply_plan(kb, root)
        self.assertEqual(len(kb.comments[card_id]), 2)
        self.assertIn("exit receipt", kb.comments[card_id][1])

    def test_projection_moves_a_card_to_done_after_acceptance(self):
        tasks = {"a": task("a", terminal_event_id="evt-a",
                           exit_code_path=self.artifact_path("ok", 0))}
        root = make_ledger(self.root, tasks, [event("evt-a", "a")],
                           artifacts=self.artifact("ok", 0))
        kb = FakeKB()
        self.apply_plan(kb, root)
        card = next(iter(kb.cards.values()))
        self.assertEqual(card.status, "review")
        make_ledger(root, tasks, [event("evt-a", "a")], [ack("evt-a", "accepted")],
                    artifacts=self.artifact("ok", 0))
        tally, _ = self.apply_plan(kb, root)
        self.assertEqual(tally["transitioned"], 1)
        self.assertEqual(card.status, "done")

    def test_blocked_from_review_reopens_first(self):
        root = make_ledger(self.root, {"a": task("a")})
        kb = FakeKB()
        self.apply_plan(kb, root)
        card = next(iter(kb.cards.values()))
        card.status = "review"
        kb.calls.clear()
        make_ledger(root, {"a": task("a", exit_code_path=self.artifact_path("nine", 9))},
                    artifacts=self.artifact("nine", 9))
        tally, _ = self.apply_plan(kb, root)
        self.assertEqual(card.status, "blocked")
        self.assertEqual(tally["transitioned"], 1)
        self.assertEqual(kb.names(), ["reopen_review_task", "block_task",
                                      "add_comment"])

    def test_review_from_blocked_unblocks_first(self):
        root = make_ledger(self.root, {"a": task("a")})
        kb = FakeKB()
        self.apply_plan(kb, root)
        card = next(iter(kb.cards.values()))
        card.status = "blocked"
        kb.calls.clear()
        make_ledger(root, {"a": task("a", exit_code_path=self.artifact_path("ok", 0))},
                    artifacts=self.artifact("ok", 0))
        self.apply_plan(kb, root)
        self.assertEqual(card.status, "review")
        self.assertEqual(kb.names()[:2], ["unblock_task", "request_review"])

    def test_blocked_is_created_directly_when_the_card_is_new(self):
        root = make_ledger(self.root, {"a": task("a", terminal_event_id="evt-1")},
                           [event("evt-1", "a", status="failed")])
        kb = FakeKB()
        self.apply_plan(kb, root)
        self.assertEqual(next(iter(kb.cards.values())).status, "blocked")
        self.assertEqual(kb.calls[0], ("create_task", "handoff:a", "blocked"))

    def test_done_card_is_never_downgraded(self):
        root = make_ledger(self.root, {"a": task("a")})
        kb = FakeKB()
        self.apply_plan(kb, root)
        card = next(iter(kb.cards.values()))
        card.status = "done"
        card.body = "stale"
        tally, _ = self.apply_plan(kb, root)
        self.assertEqual(card.status, "done")
        self.assertEqual(tally["transitioned"], 0)
        self.assertEqual(tally["refreshed"], 0)
        self.assertEqual(tally["skipped"], 1)

    def test_plan_built_on_a_stale_status_is_dropped(self):
        """A human moved the card between plan and apply: leave it alone."""
        root = make_ledger(self.root, {"a": task("a")})
        kb = FakeKB()
        self.apply_plan(kb, root)
        card = next(iter(kb.cards.values()))
        ledger = sync.load_ledger(root)
        stale = sync.Card(card.id, card.idempotency_key, "blocked", card.title,
                          card.body, card.created_by)
        plan, _ = sync.build_plan(ledger, {"handoff:a": stale}, {card.id: []}, NOW,
                                  root)
        transition = next(item for item in plan if item["kind"] == "transition")
        board = self.board(kb)
        self.assertEqual(board.apply(transition, {card.id}), "skipped_moved")
        self.assertEqual(card.status, "ready")

    def test_single_writer_boundary(self):
        """Cards without the handoff: prefix are invisible and untouchable."""
        root = make_ledger(self.root, {"a": task("a")})
        kb = FakeKB()
        kb.cards["human1"] = FakeCard("human1", "human card", "body", "ready", None,
                                      "someone")
        kb.cards["other1"] = FakeCard("other1", "watchdog card", "body", "blocked",
                                      "watchdog:t1", "watchdog")
        kb.calls.clear()
        self.apply_plan(kb, root)
        self.assertEqual(kb.cards["human1"].status, "ready")
        self.assertEqual(kb.cards["other1"].status, "blocked")
        self.assertNotIn("human1", kb.comments)
        self.assertNotIn("other1", kb.comments)
        for _name, target in ((e[0], e[1]) for e in kb.calls if len(e) > 1):
            self.assertNotIn(target, ("human1", "other1"), kb.calls)

    def test_apply_refuses_an_unowned_card_id(self):
        root = make_ledger(self.root, {"a": task("a")})
        kb = FakeKB()
        self.apply_plan(kb, root)
        card = next(iter(kb.cards.values()))
        board = self.board(kb)
        forged = {"kind": "transition", "task_id": "a", "key": "handoff:a",
                  "card_id": "human1", "from": "ready", "to": "done", "reason": "x"}
        with self.assertRaises(sync.SyncError):
            board.apply(forged, {card.id})
        foreign = {"kind": "create", "task_id": "x", "key": "watchdog:x",
                   "title": "t", "body": "b", "state": "ready", "reason": "x",
                   "comment": None}
        with self.assertRaises(sync.SyncError):
            board.apply(foreign, set())

    def test_no_delete_archive_or_claim_is_ever_reachable(self):
        root = make_ledger(self.root, {"a": task("a", terminal_event_id="evt-a")},
                           [event("evt-a", "a")])
        kb = FakeKB()
        for _ in range(3):
            self.apply_plan(kb, root)
        for forbidden in ("archive_task", "delete_archived_task", "claim_task"):
            self.assertNotIn(forbidden, kb.names())
        self.assertNotIn("archived", [card.status for card in kb.cards.values()])

    def test_ledger_is_read_only(self):
        root = make_ledger(self.root, {"a": task("a", terminal_event_id="evt-a",
                                                 exit_code_path=self.artifact_path(
                                                     "ok", 0))},
                           [event("evt-a", "a")], artifacts=self.artifact("ok", 0))
        before = tree_fingerprint(root)
        self.apply_plan(FakeKB(), root)
        self.assertEqual(tree_fingerprint(root), before)

    def test_archived_handoff_card_is_not_recreated(self):
        kb = FakeKB()
        kb.cards["gone"] = FakeCard("gone", "[handoff] Pi · old", "body", "archived",
                                    "handoff:old", sync.CARD_AUTHOR)
        root = make_ledger(self.root, {"old": task("old")})
        board = self.board(kb)
        cards, _ = board.snapshot()
        self.assertEqual(cards["handoff:old"].status, "archived")
        tally, _board = self.apply_plan(kb, root)
        self.assertEqual(tally["created"], 0)
        self.assertEqual(len(kb.cards), 1)


# --------------------------------------------------------------------------
# CLI behaviour: dry-run, lock, stdin trigger, failure reporting
# --------------------------------------------------------------------------


class CliTest(SyncTestCase):

    def args(self, root, *extra):
        return ["--ledger", root, "--board-db", self.board_path,
                "--state-dir", self.state_dir, "--log-file", self.log_path,
                "--agent-dir", self.tmp.name, "--now", str(NOW), "--no-stdin",
                *extra]

    def test_dry_run_writes_nothing_and_lists_the_full_plan(self):
        root = make_ledger(self.root, {"a": task("a"), "b": task("b")})
        out = io.StringIO()
        code = sync.run(self.args(root, "--dry-run"), stdout=out,
                        stderr=io.StringIO())
        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue().count("PLAN  create"), 2)
        self.assertIn("DRY-RUN tasks=2 cards=0 create=2", out.getvalue())
        self.assertFalse(os.path.exists(self.board_path))

    def test_dry_run_json_carries_rules_and_targets(self):
        root = make_ledger(self.root, {"a": task("a", terminal_event_id="evt-1")},
                           [event("evt-1", "a", status="failed")])
        out = io.StringIO()
        sync.run(self.args(root, "--dry-run", "--json"), stdout=out,
                 stderr=io.StringIO())
        payload = json.loads(out.getvalue())
        create = next(item for item in payload["plan"] if item["kind"] == "create")
        self.assertEqual((create["rule"], create["state"]), ("R4", "blocked"))
        self.assertEqual(payload["tasks"], 1)

    def test_dry_run_reads_an_existing_board_in_a_foreign_column(self):
        root = make_ledger(self.root, {"a": task("a", terminal_event_id="evt-a")},
                           [event("evt-a", "a", status="failed")])
        make_board(self.board_path, [("handoff:a", "review", "a")])
        out = io.StringIO()
        sync.run(self.args(root, "--dry-run"), stdout=out, stderr=io.StringIO())
        self.assertIn("review -> blocked", out.getvalue())

    def test_lock_contention_exits_zero_silently(self):
        root = make_ledger(self.root, {"a": task("a")})
        os.makedirs(self.state_dir, exist_ok=True)
        lock = os.path.join(self.state_dir, sync.LOCK_NAME)
        with open(lock, "w", encoding="utf-8") as handle:
            handle.write(f"{os.getpid()}\n")
        out, err = io.StringIO(), io.StringIO()
        code = sync.run(self.args(root), stdout=out, stderr=err)
        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(err.getvalue(), "")
        self.assertTrue(os.path.exists(lock))

    def test_stale_lock_from_a_dead_owner_is_stolen(self):
        root = make_ledger(self.root, {"a": task("a")})
        os.makedirs(self.state_dir, exist_ok=True)
        lock = os.path.join(self.state_dir, sync.LOCK_NAME)
        with open(lock, "w", encoding="utf-8") as handle:
            handle.write("999999\n")
        old = time.time() - 4000
        os.utime(lock, (old, old))
        out = io.StringIO()
        kb = FakeKB()
        with fake_hermes_modules(kb):
            code = sync.run(self.args(root), stdout=out, stderr=io.StringIO())
        self.assertEqual(code, 0)
        self.assertIn("created=1", out.getvalue())
        self.assertFalse(os.path.exists(lock))

    def test_missing_registry_is_a_logged_error(self):
        empty = os.path.join(self.tmp.name, "nope")
        os.makedirs(empty, exist_ok=True)
        err = io.StringIO()
        code = sync.run(self.args(empty), stdout=io.StringIO(), stderr=err)
        self.assertEqual(code, 1)
        self.assertIn("registry.json", err.getvalue())
        with open(self.log_path, encoding="utf-8") as handle:
            self.assertIn("ERROR", handle.read())

    def test_unreadable_ledger_is_reported_not_raised(self):
        root = make_ledger(self.root, {"a": task("a")})
        with open(os.path.join(root, "registry.json"), "w", encoding="utf-8") as handle:
            handle.write("{not json")
        err = io.StringIO()
        self.assertEqual(sync.run(self.args(root), stdout=io.StringIO(), stderr=err), 1)
        self.assertIn("not valid JSON", err.getvalue())

    def test_missing_agent_dir_is_a_logged_error(self):
        root = make_ledger(self.root, {"a": task("a")})
        args = self.args(root)
        args[args.index("--agent-dir") + 1] = os.path.join(self.tmp.name, "absent")
        err = io.StringIO()
        self.assertEqual(sync.run(args, stdout=io.StringIO(), stderr=err), 1)
        self.assertIn("hermes-agent dir not found", err.getvalue())

    def test_silent_when_nothing_changed(self):
        root = make_ledger(self.root, {"a": task("a")})
        kb = FakeKB()
        with fake_hermes_modules(kb):
            first = io.StringIO()
            sync.run(self.args(root), stdout=first, stderr=io.StringIO())
            self.assertIn("created=1", first.getvalue())
            again = io.StringIO()
            sync.run(self.args(root), stdout=again, stderr=io.StringIO())
        self.assertEqual(again.getvalue(), "")

    def test_sweep_flag_matches_the_no_argument_mode(self):
        root = make_ledger(self.root, {"a": task("a")})
        plain = io.StringIO()
        sync.run(self.args(root, "--dry-run"), stdout=plain, stderr=io.StringIO())
        swept = io.StringIO()
        sync.run(self.args(root, "--dry-run", "--sweep"), stdout=swept,
                 stderr=io.StringIO())
        self.assertEqual(plain.getvalue(), swept.getvalue())

    def test_stdin_payload_is_only_a_trigger(self):
        root = make_ledger(self.root, {"a": task("a")})
        args = [item for item in self.args(root, "--dry-run") if item != "--no-stdin"]
        baseline = io.StringIO()
        sync.run(list(args), stdout=baseline, stderr=io.StringIO())
        out = io.StringIO()
        original = sys.stdin
        sys.stdin = io.StringIO(json.dumps({"task_id": "a", "action": "ping"}))
        try:
            code = sync.run(list(args), stdout=out, stderr=io.StringIO())
        finally:
            sys.stdin = original
        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue(), baseline.getvalue())
        with open(self.log_path, encoding="utf-8") as handle:
            self.assertIn("trigger payload", handle.read())

    def test_blocking_stdin_cannot_hold_the_run(self):
        class Stalled(io.StringIO):
            def read(self, *_args, **_kwargs):
                time.sleep(5)
                return ""

            def isatty(self):
                return False

        started = time.time()
        self.assertIsNone(sync.read_stdin_payload(budget=0.2, stream=Stalled()))
        self.assertLess(time.time() - started, 2.0)


def make_board(path, rows):
    """Create a scratch board database shaped like hermes' kanban.db."""
    import sqlite3

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with contextlib.closing(sqlite3.connect(path)) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, "
                     "idempotency_key TEXT, status TEXT, title TEXT, body TEXT, "
                     "created_by TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS task_comments "
                     "(id INTEGER PRIMARY KEY, task_id TEXT, body TEXT)")
        for index, (key, status, _task_id) in enumerate(rows, start=1):
            conn.execute("INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?)",
                         (f"card{index}", key, status, f"title card{index}", "body",
                          sync.CARD_AUTHOR))
            conn.execute("INSERT INTO task_comments (task_id, body) VALUES (?, ?)",
                         (f"card{index}", "[handoff-sync evidence=state:ready] ok"))
        conn.commit()


def load_hooked_router():
    """Import the hooked copy with its agentwebhook_* helpers stubbed."""
    for name in ("agentwebhook_config", "agentwebhook_endpoint", "agentwebhook_lock"):
        if name in sys.modules:
            continue
        module = types.ModuleType(name)
        module.__dict__.update({
            "load_config": dict,
            "setting": lambda key, environ=None: None,
            "ConfigError": type("ConfigError", (Exception,), {}),
            "FileLock": object,
            "LOCK_STALE_SECONDS": 300,
            "verify_signature": lambda *a, **k: True,
            "take_pending": lambda *a, **k: [],
            "enqueue": lambda *a, **k: None,
            "validate": lambda *a, **k: None,
        })
        sys.modules[name] = module
    spec = importlib.util.spec_from_file_location("router_hooked", str(ROUTER_HOOKED))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


if False:  # Retired router-hook snapshot tests; projection now runs after verify.
 class RouterHookTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="router-hook-test-")
        self.addCleanup(self.tmp.cleanup)
        self.script = os.path.join(self.tmp.name, "kanban_handoff_sync.py")
        with open(self.script, "w", encoding="utf-8") as handle:
            handle.write("# fixture sync script\n")
        self.router = load_hooked_router()
        self.summary = {"ok": True, "result": "done", "dispatched": [], "deferred": [],
                        "blocked": [], "errors": [], "acks": [], "scanned": 1}
        self.router.__dict__["run_router"] = lambda **kwargs: dict(self.summary)
        # The router shares the global subprocess module: restore it afterwards.
        self.addCleanup(setattr, self.router.subprocess, "run",
                        self.router.subprocess.run)
        self.calls = []

    def stub_settings(self, script=None, python=None):
        values = {"AGENT_HANDOFF_KANBAN_SYNC": script or self.script,
                  self.router.HERMES_PYTHON_KEY: python or sys.executable}
        self.router.__dict__["setting"] = lambda key, environ=None: values.get(key)

    def stub_run(self, behaviour=None):
        def runner(*args, **kwargs):
            self.calls.append((args, kwargs))
            if behaviour is not None:
                raise behaviour
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        self.router.subprocess.__dict__["run"] = runner

    def call_main(self):
        out, err = io.StringIO(), io.StringIO()
        stdout, stderr = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = out, err
        try:
            code = self.router.main([])
        finally:
            sys.stdout, sys.stderr = stdout, stderr
        return code, out.getvalue(), err.getvalue()

    def test_hook_fires_after_the_summary_with_the_contract_arguments(self):
        self.stub_settings()
        self.stub_run()
        code, out, _err = self.call_main()
        self.assertEqual(code, 0)
        self.assertIn('"result": "done"', out)
        self.assertEqual(len(self.calls), 1)
        args, kwargs = self.calls[0]
        self.assertEqual(args[0], [sys.executable, self.script])
        self.assertEqual(kwargs["timeout"], 120)
        self.assertTrue(kwargs["capture_output"])

    def test_sync_script_crash_cannot_change_the_result(self):
        self.stub_settings()
        self.stub_run(RuntimeError("sync script exploded"))
        code, out, err = self.call_main()
        self.assertEqual(code, 0)
        self.assertIn('"result": "done"', out)
        self.assertIn("kanban sync skipped", err)

    def test_sync_script_timeout_cannot_change_the_result(self):
        self.stub_settings()
        self.stub_run(subprocess.TimeoutExpired(cmd=["python", "sync.py"], timeout=120))
        code, out, err = self.call_main()
        self.assertEqual(code, 0)
        self.assertIn('"result": "done"', out)
        self.assertIn("kanban sync skipped", err)

    def test_sync_script_broken_interpreter_is_only_logged(self):
        """A missing/broken venv python surfaces as OSError, not as a router bug."""
        self.stub_settings()
        self.stub_run(OSError(2, "The system cannot find the file specified"))
        code, out, err = self.call_main()
        self.assertEqual(code, 0)
        self.assertIn('"result": "done"', out)
        self.assertIn("kanban sync skipped", err)

    def test_sync_script_nonzero_exit_is_only_logged(self):
        self.stub_settings()
        self.router.subprocess.__dict__["run"] = lambda *a, **k: types.SimpleNamespace(
            returncode=1, stdout="", stderr="board unavailable")
        code, out, err = self.call_main()
        self.assertEqual(code, 0)
        self.assertIn('"result": "done"', out)
        self.assertIn("kanban sync failed rc=1", err)

    def test_missing_sync_script_is_skipped_without_subprocess(self):
        self.stub_settings(script=os.path.join(self.tmp.name, "absent.py"))
        self.stub_run()
        code, out, err = self.call_main()
        self.assertEqual(code, 0)
        self.assertIn('"result": "done"', out)
        self.assertEqual(self.calls, [])
        self.assertIn("kanban sync skipped", err)

    def test_router_failure_exit_code_survives_the_hook(self):
        self.summary = {"ok": False, "result": "error"}
        self.stub_settings()
        self.stub_run(RuntimeError("sync script exploded"))
        code, _out, _err = self.call_main()
        self.assertEqual(code, 1)

    def test_lock_busy_still_returns_zero(self):
        self.summary = {"ok": False, "result": "lock_busy"}
        self.stub_settings()
        self.stub_run(RuntimeError("sync script exploded"))
        code, _out, _err = self.call_main()
        self.assertEqual(code, 0)

    def test_diff_is_additive_and_within_twenty_lines(self):
        self.assertTrue(ROUTER_DIFF.is_file(), "router_hook.diff is missing")
        text = ROUTER_DIFF.read_text(encoding="utf-8")
        added = [line for line in text.splitlines()
                 if line.startswith("+") and not line.startswith("+++")]
        removed = [line for line in text.splitlines()
                   if line.startswith("-") and not line.startswith("---")]
        self.assertEqual(removed, [], "the hook must not remove router logic")
        self.assertLessEqual(len(added), 20, text)
        self.assertIn("timeout=120", text)
        self.assertIn("capture_output=True", text)
        self.assertEqual(len([line for line in text.splitlines()
                              if line.startswith("@@")]), 1, "one hunk only")

    def test_only_the_hook_differs_from_production(self):
        self.assertTrue(ROUTER_HOOKED.is_file(), "hooked router copy is missing")
        original = ROUTER_ORIGINAL.read_text(encoding="utf-8").splitlines()
        hooked = ROUTER_HOOKED.read_text(encoding="utf-8").splitlines()
        start = next(n for n, line in enumerate(hooked)
                     if "kanban-handoff sync hook" in line)
        end = next(n for n, line in enumerate(hooked)
                   if "end kanban-handoff sync hook" in line)
        self.assertLessEqual(end - start + 1, 20)
        self.assertEqual(hooked[:start], original[:start])
        self.assertEqual(hooked[end + 1:], original[start:])
        self.assertEqual(len(hooked) - len(original), end - start + 1)

    def test_production_router_is_untouched(self):
        text = ROUTER_ORIGINAL.read_text(encoding="utf-8")
        self.assertNotIn("kanban-handoff sync hook", text)
        self.assertNotIn("kanban_handoff_sync", text)


if __name__ == "__main__":
    unittest.main()
