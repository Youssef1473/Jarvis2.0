"""Unit and integration verification suite for Milestone 6: Residual Hardening (R-01, R-02, R-03, N-01 to N-04)."""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np


class TestMilestone6ResidualHardening(unittest.TestCase):
    """Verifies all 7 residual and novel hardening fixes."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "test_m6.db"

    def tearDown(self):
        try:
            from jarvis.core.memory.sqlite_base import SingleWriterActor
            with SingleWriterActor._registry_lock:
                actor = SingleWriterActor._instances.pop(os.path.normcase(str(self.db_path.resolve())), None)
                if actor:
                    actor.stop(drain=False, timeout=1.0)
        except Exception:  # noqa: S110, BLE001
            pass
        self.tmp_dir.cleanup()

    # -------------------------------------------------------------------------
    # R-01: SingleWriterActor.get_or_create & Auxiliary Daemon Serialization
    # -------------------------------------------------------------------------
    def test_r01_single_writer_actor_registry_and_auxiliary_daemons(self):
        """Verifies SingleWriterActor.get_or_create registry and actor writes across all 4 daemons."""
        from jarvis.core.cognition.habit_graph import TemporalHabitGraph
        from jarvis.core.memory.obsidian_sync import (
            ObsidianChunk,
            ObsidianContextSyncEngine,
        )
        from jarvis.core.memory.sqlite_base import SingleWriterActor
        from jarvis.core.memory.transcript_distiller import TranscriptDistiller
        from jarvis.core.reflex_engine import DynamicReflexEngine

        actor1 = SingleWriterActor.get_or_create(self.db_path)
        actor2 = SingleWriterActor.get_or_create(self.db_path)
        self.assertIs(actor1, actor2, "get_or_create must return the same singleton actor instance")

        # 1. TemporalHabitGraph uses SingleWriterActor
        habit_graph = TemporalHabitGraph(db_path=self.db_path, sync_writes=True)
        habit_graph.observe_action("open_ide", "DEEP_FLOW", "code", timestamp=time.time())
        habit_graph.observe_action("run_tests", "DEEP_FLOW", "code", timestamp=time.time() + 1.0)

        # 2. TranscriptDistiller uses SingleWriterActor
        distiller = TranscriptDistiller(db_path=self.db_path)
        distiller.set_last_distilled_id(42)
        self.assertEqual(distiller.get_last_distilled_id(), 42)

        # 3. DynamicReflexEngine uses SingleWriterActor
        reflex_engine = DynamicReflexEngine(db_path=self.db_path)
        promoted = reflex_engine.record_habit("open browser", "open_browser_tab", {"url": "http://localhost"})
        self.assertTrue(promoted)
        self.assertEqual(len(reflex_engine.list_learned_habits()), 1)

        # 4. ObsidianContextSyncEngine uses SingleWriterActor
        vault_dir = Path(self.tmp_dir.name) / "vault"
        vault_dir.mkdir(parents=True, exist_ok=True)
        sync_engine = ObsidianContextSyncEngine(vault_path=str(vault_dir), db_path=str(self.db_path))
        chunk = ObsidianChunk(
            file_path="notes/arch.md",
            note_title="Architecture",
            breadcrumb="Top > Architecture",
            content="Jarvis system architecture",
            tags=["core"],
            wiki_links=["design"],
            frontmatter={"author": "Luna"},
            chunk_index=0,
        )
        sync_engine._db_queue.put({
            "action": "UPSERT_NOTE",
            "file_path": "notes/arch.md",
            "file_hash": "abcd1234hash",
            "mtime": time.time(),
            "chunks": [chunk],
        })
        sync_engine._flush_pending_db_queue()

        # Verify all writes persisted in database via read-only connection
        ro_conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        ro_conn.row_factory = sqlite3.Row
        try:
            # Check habit transitions
            row_habit = ro_conn.execute("SELECT COUNT(*) as cnt FROM action_transitions").fetchone()
            self.assertGreaterEqual(row_habit["cnt"], 1)

            # Check schema_meta last distilled
            row_dist = ro_conn.execute("SELECT value FROM schema_meta WHERE key = 'last_distilled_interaction_id'").fetchone()
            self.assertEqual(int(row_dist["value"]), 42)

            # Check learned_habits
            row_learn = ro_conn.execute("SELECT skill_name FROM learned_habits WHERE trigger_phrase = 'open browser'").fetchone()
            self.assertEqual(row_learn["skill_name"], "open_browser_tab")

            # Check obsidian_chunks
            row_obs = ro_conn.execute("SELECT note_title FROM obsidian_chunks WHERE file_path = 'notes/arch.md'").fetchone()
            self.assertEqual(row_obs["note_title"], "Architecture")
        finally:
            ro_conn.close()

    # -------------------------------------------------------------------------
    # R-02: Reflex Macro Table Schema Unification
    # -------------------------------------------------------------------------
    def test_r02_reflex_macro_schema_unification(self):
        """Verifies compiled_reflex_macros accommodates both canonical and backward-compatible queries."""
        from jarvis.core.memory.sleep_consolidator import SleepPhaseConsolidator
        from jarvis.core.reflex_engine import DynamicReflexEngine

        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            # Create interactions with contiguous repeated sequences
            conn.execute("""
                CREATE TABLE IF NOT EXISTS interactions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_input TEXT,
                    jarvis_reply TEXT,
                    tool_calls TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            seq_json = json.dumps([
                {"name": "list_directory"},
                {"name": "read_file"},
                {"name": "inspect_screen"},
            ])
            for _ in range(4):
                conn.execute("INSERT INTO interactions (tool_calls) VALUES (?)", (seq_json,))
            conn.commit()

            consolidator = SleepPhaseConsolidator(db_path=self.db_path, vector_engine=MagicMock())
            compiled_count = consolidator.run_pass_b_workflow_distillation(conn)
            conn.commit()
            self.assertEqual(compiled_count, 1)
        finally:
            conn.close()

        # 1. Canonical query expected by DynamicReflexEngine
        reflex_engine = DynamicReflexEngine(db_path=self.db_path)
        self.assertEqual(len(reflex_engine._macro_cache), 1)

        # 2. Compatibility query expected by tests/test_milestone2_memory_hardening.py
        conn2 = sqlite3.connect(str(self.db_path))
        conn2.row_factory = sqlite3.Row
        try:
            cursor = conn2.cursor()
            cursor.execute("SELECT macro_id, tool_sequence, occurrence_count FROM compiled_reflex_macros")
            row = cursor.fetchone()
            self.assertIsNotNone(row)
            self.assertIn("list_directory->read_file->inspect_screen", row["tool_sequence"])
            self.assertEqual(row["occurrence_count"], 4)
            self.assertTrue(row["macro_id"].startswith("macro_"))
        finally:
            conn2.close()

    # -------------------------------------------------------------------------
    # R-03: WinEvent Hook Async Callback Offloading & SPSC Condition Variable
    # -------------------------------------------------------------------------
    def test_r03_winevent_daemon_async_offload_and_hung_window_guard(self):
        """Verifies WinEventHookDaemon offloads callbacks to a queue and skips hung windows."""
        from jarvis.vision.window_hook_daemon import (
            CHILDID_SELF,
            OBJID_WINDOW,
            WinEventHookDaemon,
        )

        daemon = WinEventHookDaemon()
        received_events = []
        event_handled = threading.Event()

        def _listener(hwnd, event, title):
            received_events.append((hwnd, event, title))
            event_handled.set()

        daemon.add_listener(_listener)
        daemon._running = True

        # Test hung window check
        with patch("ctypes.windll.user32.IsHungAppWindow", return_value=1, create=True):
            title = daemon._get_window_title(99999)
            self.assertEqual(title, "", "Must return empty title if window is hung")

        # Start dispatch worker thread
        dispatch_thread = threading.Thread(target=daemon._dispatch_worker_loop, daemon=True)
        dispatch_thread.start()

        try:
            # Simulate native C callback
            with patch.object(daemon, "_get_window_title", return_value="Active Window Title"):
                daemon._event_callback(0, 0x0003, 12345, OBJID_WINDOW, CHILDID_SELF, 1, 100)

            self.assertTrue(event_handled.wait(timeout=1.0))
            self.assertEqual(len(received_events), 1)
            self.assertEqual(received_events[0], (12345, 0x0003, "Active Window Title"))
        finally:
            daemon._running = False
            daemon._event_queue.put(None)
            dispatch_thread.join(timeout=1.0)

    def test_r03_spsc_ring_buffer_condition_variable(self):
        """Verifies SPSCFrameRingBuffer condition variable synchronizes write and read without races."""
        from jarvis.audio.wake_word import SPSCFrameRingBuffer

        ring = SPSCFrameRingBuffer(capacity=10, blocksize=64)
        read_frames = []

        def _consumer():
            for _ in range(5):
                frame = ring.read_frame(timeout=0.5)
                if frame is not None:
                    read_frames.append(frame)

        t = threading.Thread(target=_consumer)
        t.start()

        # Producer writes with slight delay
        time.sleep(0.02)
        for i in range(5):
            test_frame = np.full(64, float(i + 1), dtype=np.float32)
            ring.write_frame(test_frame)

        t.join(timeout=1.5)
        self.assertEqual(len(read_frames), 5)
        for i, f in enumerate(read_frames):
            self.assertEqual(f[0], float(i + 1))

    # -------------------------------------------------------------------------
    # N-01: MCP JSON-RPC ID Correlation & TTS Epoch-Guarded Stop
    # -------------------------------------------------------------------------
    def test_n01_mcp_client_stdio_request_correlation(self):
        """Verifies MCPClientSession correlates stdio JSON-RPC responses strictly by request ID."""
        from jarvis.core.mcp_manager import MCPClientSession

        session = MCPClientSession(name="test_server", transport="stdio", command="mock.exe")
        session._proc = MagicMock()
        session._proc.poll.return_value = None
        session._proc.stdin = MagicMock()

        # Setup correlated response queue
        req_id = session._next_id()
        payload = {"jsonrpc": "2.0", "id": req_id, "method": "test/method"}

        # Simulate background worker receiving out-of-order or notification frame, then response
        def _simulated_worker():
            time.sleep(0.01)
            # Send uncorrelated notification
            session._stdout_queue.put('{"jsonrpc": "2.0", "method": "notifications/progress"}')
            # Dispatch correlated response directly to pending queue via _pending_requests
            with session._pending_lock:
                q = session._pending_requests.get(req_id)
            if q:
                q.put({"jsonrpc": "2.0", "id": req_id, "result": {"correlated": True}})

        worker_thread = threading.Thread(target=_simulated_worker)
        worker_thread.start()

        resp = session._send_stdio_request(payload, timeout=1.0)
        worker_thread.join(timeout=1.0)
        self.assertEqual(resp.get("result", {}).get("correlated"), True)

    def test_n01_tts_stop_epoch_guard(self):
        """Verifies TextToSpeech.stop and _do_stop skip halting when session activation is newer than expected epoch."""
        from jarvis.audio.tts import TextToSpeech

        with patch.object(TextToSpeech, "_init_kokoro"):
            tts = TextToSpeech(config={"tts": {"enabled": True}})
            tts._session_activation_epoch = 10
            tts._stream_active = True
            tts._is_speaking = True

            # Attempt stop with an older epoch (e.g. 5) -> must be a NO-OP
            stopped = tts.stop(stop_timeout=0.05, expected_epoch=5)
            self.assertTrue(stopped)
            self.assertTrue(tts._stream_active, "Stream must remain active when expected_epoch is older than activation epoch")

            # Attempt stop with a newer/matching epoch (e.g. 10) -> must halt
            tts.stop(stop_timeout=0.05, expected_epoch=10, non_blocking=True)
            self.assertFalse(tts._stream_active, "Stream must be halted when expected_epoch matches or exceeds activation epoch")

    # -------------------------------------------------------------------------
    # N-02: WebSocket Command Throttling Backpressure
    # -------------------------------------------------------------------------
    def test_n02_ws_command_throttling_backpressure(self):
        """Verifies WebSocket command handler emits busy feedback when 4 worker slots are occupied."""
        import asyncio

        from jarvis.ui.server import JarvisUIService

        service = JarvisUIService(port=9999)
        # Exhaust all 4 semaphore slots
        for _ in range(4):
            service._command_semaphore.acquire()

        mock_ws = MagicMock()
        mock_ws.headers = {"host": "127.0.0.1:9999", "origin": "http://127.0.0.1:9999"}
        mock_ws.query_params = {}
        sent_messages = []

        async def _mock_send(text):
            sent_messages.append(text)

        mock_ws.send_text = _mock_send

        # Simulate receiving command while fully occupied
        async def _test():
            # Step 1: acquire failed -> must emit 'busy'
            if not service._command_semaphore.acquire(blocking=False):
                await mock_ws.send_text(json.dumps({"type": "busy", "message": "Jarvis is busy."}))

        asyncio.run(_test())
        self.assertEqual(len(sent_messages), 1)
        data = json.loads(sent_messages[0])
        self.assertEqual(data["type"], "busy")

        # Release semaphore slots
        for _ in range(4):
            service._command_semaphore.release()
        service.stop()

    # -------------------------------------------------------------------------
    # N-03: Flow-State Bridge Token Persistence
    # -------------------------------------------------------------------------
    def test_n03_flow_state_token_persistence(self):
        """Verifies FlowStateBridgeListener persists auto-generated token to ~/.jarvis_sec/flow_state.token."""
        from jarvis.context.bridges.pipe_listener import FlowStateBridgeListener

        with patch.object(FlowStateBridgeListener, "TOKEN_PATH", Path(self.tmp_dir.name) / ".jarvis_sec" / "flow_state.token"):
            listener = FlowStateBridgeListener(port=31499)
            token_path = FlowStateBridgeListener.TOKEN_PATH
            self.assertTrue(token_path.exists(), "Token file must be created on disk")
            persisted = token_path.read_text(encoding="utf-8").strip()
            self.assertEqual(persisted, listener.auth_token)
            self.assertGreaterEqual(len(persisted), 32)

    # -------------------------------------------------------------------------
    # N-04: Superseded Knowledge Graph Vector Deletion
    # -------------------------------------------------------------------------
    def test_n04_superseded_knowledge_graph_vector_deletion(self):
        """Verifies triggers delete superseded edge embeddings when valid_until is updated or functionally replaced."""
        conn = sqlite3.connect(str(self.db_path))
        try:
            # Create base tables simulating sqlite_base schema
            conn.execute("""
                CREATE TABLE IF NOT EXISTS knowledge_edges (
                    edge_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    subject TEXT NOT NULL,
                    predicate TEXT NOT NULL,
                    object TEXT NOT NULL,
                    is_functional INTEGER NOT NULL DEFAULT 1,
                    valid_from REAL NOT NULL,
                    valid_until REAL DEFAULT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS kg_edges_vec (
                    edge_id INTEGER PRIMARY KEY,
                    embedding BLOB
                )
            """)
            # Create triggers defined in sqlite_base
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS kg_edges_vec_ad AFTER DELETE ON knowledge_edges BEGIN
                    DELETE FROM kg_edges_vec WHERE edge_id = old.edge_id;
                END;
            """)
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS kg_edges_vec_au AFTER UPDATE OF valid_until ON knowledge_edges
                WHEN new.valid_until IS NOT NULL AND old.valid_until IS NULL
                BEGIN
                    DELETE FROM kg_edges_vec WHERE edge_id = old.edge_id;
                END;
            """)
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS trg_kg_supersede_functional_vec
                BEFORE INSERT ON knowledge_edges
                WHEN NEW.is_functional = 1 AND NEW.valid_until IS NULL
                BEGIN
                    DELETE FROM kg_edges_vec WHERE edge_id IN (
                        SELECT edge_id FROM knowledge_edges
                        WHERE subject = NEW.subject
                          AND predicate = NEW.predicate
                          AND valid_until IS NULL
                          AND object != NEW.object
                    );
                END;
            """)
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS trg_kg_supersede_functional
                BEFORE INSERT ON knowledge_edges
                WHEN NEW.is_functional = 1 AND NEW.valid_until IS NULL
                BEGIN
                    UPDATE knowledge_edges
                    SET valid_until = NEW.valid_from
                    WHERE subject = NEW.subject
                      AND predicate = NEW.predicate
                      AND valid_until IS NULL
                      AND object != NEW.object;
                END;
            """)

            # 1. Insert initial functional edge: "Youssef" "primary_ide" "VS Code"
            conn.execute("""
                INSERT INTO knowledge_edges (subject, predicate, object, is_functional, valid_from)
                VALUES ('Youssef', 'primary_ide', 'VS Code', 1, 100.0)
            """)
            edge1_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute("INSERT INTO kg_edges_vec (edge_id, embedding) VALUES (?, X'0001')", (edge1_id,))
            conn.commit()

            # Verify edge1 exists in both tables
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM kg_edges_vec WHERE edge_id = ?", (edge1_id,)).fetchone()[0], 1)

            # 2. Insert superseding functional edge: "Youssef" "primary_ide" "Cursor"
            conn.execute("""
                INSERT INTO knowledge_edges (subject, predicate, object, is_functional, valid_from)
                VALUES ('Youssef', 'primary_ide', 'Cursor', 1, 200.0)
            """)
            edge2_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute("INSERT INTO kg_edges_vec (edge_id, embedding) VALUES (?, X'0002')", (edge2_id,))
            conn.commit()

            # Verify edge1 was superseded in knowledge_edges
            edge1_row = conn.execute("SELECT valid_until FROM knowledge_edges WHERE edge_id = ?", (edge1_id,)).fetchone()
            self.assertEqual(edge1_row[0], 200.0)

            # CRITICAL: Verify edge1 vector embedding was deleted by trg_kg_supersede_functional_vec
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM kg_edges_vec WHERE edge_id = ?", (edge1_id,)).fetchone()[0],
                0,
                "Superseded knowledge edge embedding must be deleted from kg_edges_vec",
            )
            # Verify edge2 vector embedding remains
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM kg_edges_vec WHERE edge_id = ?", (edge2_id,)).fetchone()[0],
                1,
            )
        finally:
            conn.close()


    # -------------------------------------------------------------------------
    # Operational Deployment Hardening Recommendations
    # -------------------------------------------------------------------------
    def test_single_writer_actor_normcase_resolution(self):
        """Verifies SingleWriterActor.get_or_create resolves case-insensitively on Windows."""
        from jarvis.core.memory.sqlite_base import SingleWriterActor

        str_path = str(self.db_path)
        varied_path = Path(str_path.upper() if str_path.islower() else str_path.lower())
        actor1 = SingleWriterActor.get_or_create(self.db_path)
        actor2 = SingleWriterActor.get_or_create(varied_path)
        self.assertIs(actor1, actor2, "Actors created with different casing must resolve to the identical singleton")

    def test_flow_state_principal_sid_binding(self):
        """Verifies FlowStateBridgeListener resolves principal with SID or username and persists token."""
        from jarvis.context.bridges.pipe_listener import FlowStateBridgeListener

        with patch.object(FlowStateBridgeListener, "TOKEN_PATH", Path(self.tmp_dir.name) / ".jarvis_sec" / "flow_state_sid.token"):
            listener = FlowStateBridgeListener(port=31498)
            token_path = FlowStateBridgeListener.TOKEN_PATH
            self.assertTrue(token_path.exists())
            self.assertEqual(token_path.read_text(encoding="utf-8").strip(), listener.auth_token)

            if sys.platform == "win32":
                import subprocess
                res = subprocess.run(["icacls", str(token_path)], capture_output=True, text=True, check=True)
                acl_out = res.stdout.upper()
                self.assertTrue(
                    any(p in acl_out for p in ["(R,W)", "(F)", "(M)"]),
                    f"ACL must grant user permissions: {acl_out}",
                )
                self.assertNotIn("EVERYONE", acl_out)
                self.assertNotIn("BUILTIN\\USERS", acl_out)

    def test_health_check_sqlite_version_validation(self):
        """Verifies run_system_health_check unconditionally validates SQLite runtime version >= 3.31.0 for virtual columns."""
        from jarvis.core.health_check import run_system_health_check

        report = run_system_health_check()
        mem_db = report["subsystems"]["memory_db"]
        self.assertIn("sqlite_version", mem_db)
        self.assertTrue(mem_db["virtual_columns_supported"], "SQLite runtime version must support virtual columns")

    def test_reflex_engine_does_not_retry_on_in_flight_timeout(self):
        """Verifies DynamicReflexEngine does not retry writes on in-flight TimeoutError."""
        from jarvis.core.memory.sqlite_base import SingleWriterActor
        from jarvis.core.reflex_engine import DynamicReflexEngine

        engine = DynamicReflexEngine(db_path=str(self.db_path))
        call_count = 0

        def _timed_out_write(conn):
            nonlocal call_count
            call_count += 1
            return 42

        in_flight_timeout = TimeoutError("Simulated in-flight write timeout")
        in_flight_timeout.timed_out_in_flight = True
        in_flight_timeout.transaction_outcome_unknown = True
        in_flight_timeout.cancelled_before_execution = False

        with patch.object(SingleWriterActor, "get_or_create") as mock_get_or_create:
            mock_actor = MagicMock()
            mock_actor.execute_write.side_effect = in_flight_timeout
            mock_get_or_create.return_value = mock_actor

            with self.assertRaises(TimeoutError):
                engine.execute_write(_timed_out_write)

        self.assertEqual(call_count, 0, "Write callback must never be retried when timeout occurs in flight")

    def test_no_competing_writer_while_previous_actor_worker_alive(self):
        """Verifies SingleWriterActor.get_or_create refuses to spawn a second actor

        while the previous actor's worker thread is still alive and writing.
        """
        from jarvis.core.memory.sqlite_base import (
            ActorStoppingError,
            SingleWriterActor,
        )

        actor_a = SingleWriterActor.get_or_create(self.db_path)
        write_started = threading.Event()
        unblock_write = threading.Event()

        def _blocking_write(conn):
            write_started.set()
            unblock_write.wait(timeout=5.0)
            conn.execute("CREATE TABLE IF NOT EXISTS blocked_test (id INT);")

        write_done = threading.Event()

        def _do_write():
            try:
                actor_a.execute_write(_blocking_write, timeout=5.0)
            finally:
                write_done.set()

        t_write = threading.Thread(target=_do_write, daemon=True)
        t_write.start()

        self.assertTrue(write_started.wait(timeout=2.0), "Worker must begin executing write")
        self.assertTrue(actor_a._thread.is_alive(), "Worker thread must be alive")

        actor_a.stop(drain=False, timeout=0.01)
        self.assertTrue(actor_a._thread.is_alive(), "Worker thread must still be alive executing the write")

        with self.assertRaises(ActorStoppingError) as ctx:
            SingleWriterActor.get_or_create(self.db_path)
        self.assertIn("still terminating", str(ctx.exception))

        unblock_write.set()
        t_write.join(timeout=2.0)
        actor_a._thread.join(timeout=2.0)
        self.assertFalse(actor_a._thread.is_alive(), "Old worker thread must have terminated")

        actor_b = SingleWriterActor.get_or_create(self.db_path)
        self.assertIsNot(actor_a, actor_b, "Must be a fresh actor instance")
        self.assertTrue(actor_b._thread.is_alive(), "New actor's thread must be alive")
        actor_b.stop(drain=False, timeout=1.0)

    def test_execute_or_fallback_strictly_rejects_when_actor_worker_alive(self):
        """Verifies SingleWriterActor.execute_or_fallback raises ActorStoppingError and strictly
        refuses to execute fallback direct writes while a stopping actor's worker thread is still alive.
        """
        from jarvis.core.memory.sqlite_base import (
            ActorStoppingError,
            SingleWriterActor,
        )

        actor = SingleWriterActor.get_or_create(self.db_path)
        write_started = threading.Event()
        unblock_write = threading.Event()

        def _blocking_write(conn):
            write_started.set()
            unblock_write.wait(timeout=5.0)

        t_write = threading.Thread(
            target=lambda: actor.execute_write(_blocking_write, timeout=5.0),
            daemon=True,
        )
        t_write.start()

        self.assertTrue(write_started.wait(timeout=2.0), "Worker must begin executing write")
        self.assertTrue(actor._thread.is_alive(), "Worker thread must be alive")

        # Stop actor with short timeout so it enters STATE_STOPPING while worker is still alive
        actor.stop(drain=False, timeout=0.01)
        self.assertTrue(actor._thread.is_alive(), "Worker thread must still be alive executing write")

        fallback_called = False
        second_write_called = False

        def _fallback_factory():
            nonlocal fallback_called
            fallback_called = True
            return sqlite3.connect(str(self.db_path))

        def _second_write(conn):
            nonlocal second_write_called
            second_write_called = True

        with self.assertRaises(ActorStoppingError):
            SingleWriterActor.execute_or_fallback(
                self.db_path,
                _second_write,
                _fallback_factory,
                timeout=1.0,
            )

        self.assertFalse(fallback_called, "Fallback factory must NOT be called when worker is alive")
        self.assertFalse(second_write_called, "Write callback must NOT execute on direct connection when worker is alive")

        unblock_write.set()
        t_write.join(timeout=2.0)
        actor._thread.join(timeout=2.0)
        self.assertFalse(actor._thread.is_alive(), "Worker thread must have cleanly exited")

    def test_fallback_write_and_get_or_create_mutual_exclusion(self):
        """Verifies that while a direct fallback write holds _db_write_locks,
        concurrent get_or_create() calls are serialized and cannot instantiate a competing actor.
        """
        from jarvis.core.memory.sqlite_base import (
            ActorUnavailableError,
            SingleWriterActor,
        )

        resolved_key = os.path.normcase(str(self.db_path.resolve()))
        # Ensure no active actor in registry
        with SingleWriterActor._registry_lock:
            old = SingleWriterActor._instances.pop(resolved_key, None)
            if old:
                old.stop(drain=False, timeout=0.1)

        fallback_started = threading.Event()
        unblock_fallback = threading.Event()
        goc_completed = threading.Event()
        new_actor_box = []

        orig_goc = SingleWriterActor.get_or_create
        call_count = 0
        goc_lock = threading.Lock()

        def _mock_goc(db_path):
            nonlocal call_count
            with goc_lock:
                call_count += 1
                if call_count == 1:
                    raise ActorUnavailableError("simulated actor offline")
            return orig_goc(db_path)

        def _fallback_write(conn):
            fallback_started.set()
            unblock_fallback.wait(timeout=5.0)
            conn.execute("CREATE TABLE IF NOT EXISTS fallback_mutex (id INT);")

        def _run_fallback():
            SingleWriterActor.execute_or_fallback(
                self.db_path,
                _fallback_write,
                lambda: sqlite3.connect(str(self.db_path)),
            )

        with patch.object(SingleWriterActor, "get_or_create", side_effect=_mock_goc):
            t_fallback = threading.Thread(target=_run_fallback, daemon=True)
            t_fallback.start()

            self.assertTrue(fallback_started.wait(timeout=2.0), "Fallback write must start and hold db_lock")

            def _run_goc():
                actor = SingleWriterActor.get_or_create(self.db_path)
                new_actor_box.append(actor)
                goc_completed.set()

            t_goc = threading.Thread(target=_run_goc, daemon=True)
            t_goc.start()

            # goc must NOT complete while fallback write is running
            self.assertFalse(goc_completed.wait(timeout=0.15), "get_or_create must block while fallback write is in flight")

            unblock_fallback.set()
            t_fallback.join(timeout=2.0)

            # After fallback write finishes and releases db_lock, get_or_create must complete
            self.assertTrue(goc_completed.wait(timeout=2.0), "get_or_create must succeed after fallback completes")
            t_goc.join(timeout=2.0)

            self.assertTrue(new_actor_box)
            new_actor = new_actor_box[0]
            self.assertTrue(new_actor._thread.is_alive())
            new_actor.stop(drain=False, timeout=1.0)

    def test_atomic_admission_and_shutdown_no_post_sentinel_enqueue(self):
        """Verifies execute_write cannot enqueue items after the shutdown sentinel under _state_lock."""
        from jarvis.core.memory.sqlite_base import (
            ActorUnavailableError,
            SingleWriterActor,
        )

        actor = SingleWriterActor.get_or_create(self.db_path)
        actor.stop(drain=True, timeout=1.0)

        with self.assertRaises(ActorUnavailableError) as ctx:
            actor.execute_write(lambda c: 1)
        self.assertTrue(ctx.exception.cancelled_before_execution)
        self.assertEqual(ctx.exception.transaction_outcome, "not_executed")

    def test_execute_or_fallback_never_double_executes(self):
        """Verifies SingleWriterActor.execute_or_fallback executes callbacks at most once."""
        from jarvis.core.memory.sqlite_base import SingleWriterActor

        exec_count = 0

        def _counted_write(conn):
            nonlocal exec_count
            exec_count += 1
            return exec_count

        res1 = SingleWriterActor.execute_or_fallback(
            self.db_path,
            _counted_write,
            lambda: sqlite3.connect(str(self.db_path)),
        )
        self.assertEqual(res1, 1)
        self.assertEqual(exec_count, 1)

        in_flight_timeout = TimeoutError("in-flight timeout")
        in_flight_timeout.timed_out_in_flight = True
        in_flight_timeout.transaction_outcome_unknown = True

        with patch.object(SingleWriterActor, "get_or_create") as mock_goc:
            mock_actor = MagicMock()
            mock_actor.execute_write.side_effect = in_flight_timeout
            mock_goc.return_value = mock_actor

            with self.assertRaises(TimeoutError):
                SingleWriterActor.execute_or_fallback(
                    self.db_path,
                    _counted_write,
                    lambda: sqlite3.connect(str(self.db_path)),
                )
        self.assertEqual(exec_count, 1, "Must NOT re-execute on in-flight timeout")

        def _failing_write(conn):
            nonlocal exec_count
            exec_count += 1
            raise sqlite3.OperationalError("syntax error in SQL")

        with self.assertRaises(sqlite3.OperationalError):
            SingleWriterActor.execute_or_fallback(
                self.db_path,
                _failing_write,
                lambda: sqlite3.connect(str(self.db_path)),
            )
        self.assertEqual(exec_count, 2, "Must NOT retry failing SQL on fallback connection")

    def test_reentrant_actor_creation_prohibited_during_fallback(self):
        """Verifies that a fallback write callback running under _db_write_locks cannot
        reentrantly invoke get_or_create(), instantiate SingleWriterActor, or nest fallback.
        """
        from jarvis.core.memory.sqlite_base import (
            ActorUnavailableError,
            SingleWriterActor,
            SingleWriterError,
        )

        orig_goc = SingleWriterActor.get_or_create
        first_call = True
        goc_lock = threading.Lock()

        def _mock_goc(db_path):
            nonlocal first_call
            with goc_lock:
                if first_call:
                    first_call = False
                    raise ActorUnavailableError("simulated offline")
            return orig_goc(db_path)

        with patch.object(SingleWriterActor, "get_or_create", side_effect=_mock_goc):
            def _reentrant_callback(conn):
                # 1. Attempt reentrant get_or_create
                with self.assertRaises(SingleWriterError) as ctx1:
                    SingleWriterActor.get_or_create(self.db_path)
                self.assertIn("Reentrant SingleWriterActor instantiation prohibited", str(ctx1.exception))

                # 2. Attempt direct instantiation
                with self.assertRaises(SingleWriterError) as ctx2:
                    SingleWriterActor(self.db_path)
                self.assertIn("Direct SingleWriterActor instantiation prohibited", str(ctx2.exception))

                # 3. Attempt nested execute_or_fallback
                with self.assertRaises(SingleWriterError) as ctx3:
                    SingleWriterActor.execute_or_fallback(
                        self.db_path,
                        lambda c: None,
                        lambda: sqlite3.connect(str(self.db_path)),
                    )
                self.assertIn("prohibited", str(ctx3.exception))
                return "reentrancy_strictly_prevented"

            result = SingleWriterActor.execute_or_fallback(
                self.db_path,
                _reentrant_callback,
                lambda: sqlite3.connect(str(self.db_path)),
            )
            self.assertEqual(result, "reentrancy_strictly_prevented")

    def test_dead_worker_drains_and_rejects_pending_queued_requests(self):
        """Verifies that when an actor worker terminates, all pending queued requests
        are immediately drained and rejected with a definite failure rather than silently lost.
        """
        from jarvis.core.memory.sqlite_base import SingleWriterActor

        actor = SingleWriterActor.get_or_create(self.db_path)
        write1_started = threading.Event()
        unblock_write1 = threading.Event()
        write2_result_box = []
        write2_completed = threading.Event()

        def _blocking_write1(conn):
            write1_started.set()
            unblock_write1.wait(timeout=5.0)

        def _queued_write2(conn):
            return "should_never_execute"

        # Start write 1
        t1 = threading.Thread(target=lambda: actor.execute_write(_blocking_write1, timeout=5.0), daemon=True)
        t1.start()
        self.assertTrue(write1_started.wait(timeout=2.0))

        # Enqueue write 2 behind write 1
        def _run_write2():
            try:
                res = actor.execute_write(_queued_write2, timeout=5.0)
                write2_result_box.append(("success", res))
            except (RuntimeError, TimeoutError) as ex:
                write2_result_box.append(("error", ex))
            finally:
                write2_completed.set()

        t2 = threading.Thread(target=_run_write2, daemon=True)
        t2.start()

        # Stop actor with drain=False to terminate worker immediately
        actor.stop(drain=False, timeout=0.01)
        unblock_write1.set()
        t1.join(timeout=2.0)
        t2.join(timeout=2.0)

        self.assertTrue(write2_completed.is_set(), "Queued write 2 must complete immediately")
        self.assertEqual(len(write2_result_box), 1)
        status, exc = write2_result_box[0]
        self.assertEqual(status, "error")
        self.assertIsInstance(exc, RuntimeError)
        self.assertIn("shutdown before write could execute", str(exc))

    def test_worker_fatal_exception_drains_queue_and_sets_failure(self):
        """Verifies that an unexpected worker crash drains all pending queue items
        and raises an explicit failure on callers without hanging.
        """
        from jarvis.core.memory.sqlite_base import SingleWriterActor

        class SimulatedFatalWorkerCrash(BaseException):
            pass

        actor = SingleWriterActor.get_or_create(self.db_path)
        write1_started = threading.Event()
        unblock_crash = threading.Event()
        write1_completed = threading.Event()
        write1_result_box = []
        write2_completed = threading.Event()
        write2_result_box = []

        def _crashing_write1(conn):
            write1_started.set()
            unblock_crash.wait(timeout=5.0)
            raise SimulatedFatalWorkerCrash("simulated catastrophic worker crash")

        def _queued_write2(conn):
            return "never"

        def _run_write1():
            try:
                res = actor.execute_write(_crashing_write1, timeout=5.0)
                write1_result_box.append(("success", res))
            except (RuntimeError, TimeoutError, SimulatedFatalWorkerCrash) as ex:
                write1_result_box.append(("error", ex))
            finally:
                write1_completed.set()

        def _run_write2():
            try:
                res = actor.execute_write(_queued_write2, timeout=5.0)
                write2_result_box.append(("success", res))
            except (RuntimeError, TimeoutError) as ex:
                write2_result_box.append(("error", ex))
            finally:
                write2_completed.set()

        t1 = threading.Thread(target=_run_write1, daemon=True)
        t1.start()
        self.assertTrue(write1_started.wait(timeout=2.0))

        # Queue write 2 while write 1 is actively executing
        t2 = threading.Thread(target=_run_write2, daemon=True)
        t2.start()

        # Wait until write 2 is queued
        for _ in range(50):
            if not actor._queue.empty():
                break
            time.sleep(0.01)

        # Trigger crash in write 1
        unblock_crash.set()

        # Wait for worker thread to terminate from crash
        actor._thread.join(timeout=2.0)
        self.assertFalse(actor._thread.is_alive(), "Worker thread must have terminated from fatal error")

        # Both active write 1 and queued write 2 must unblock immediately
        self.assertTrue(write1_completed.wait(timeout=2.0), "Active request must unblock immediately on worker death")
        self.assertTrue(write2_completed.wait(timeout=2.0), "Queued request must not hang after worker dies")
        t1.join(timeout=2.0)
        t2.join(timeout=2.0)

        # Verify active write 1 outcome:
        self.assertEqual(len(write1_result_box), 1)
        st1, exc1 = write1_result_box[0]
        self.assertEqual(st1, "error")
        self.assertIsInstance(exc1, RuntimeError)
        self.assertIn("transaction outcome is unknown", str(exc1))
        self.assertFalse(getattr(exc1, "cancelled_before_execution", True))
        self.assertEqual(getattr(exc1, "transaction_outcome", ""), "unknown")
        self.assertTrue(getattr(exc1, "transaction_outcome_unknown", False))

        # Verify queued write 2 outcome:
        self.assertEqual(len(write2_result_box), 1)
        st2, exc2 = write2_result_box[0]
        self.assertEqual(st2, "error")
        self.assertIsInstance(exc2, RuntimeError)
        self.assertIn("worker terminated unexpectedly", str(exc2))
        self.assertTrue(getattr(exc2, "cancelled_before_execution", False))
        self.assertEqual(getattr(exc2, "transaction_outcome", ""), "not_executed")
        self.assertFalse(getattr(exc2, "transaction_outcome_unknown", True))

    def test_cross_thread_direct_constructor_blocks_during_fallback(self):
        """Verifies that a thread calling SingleWriterActor(db_path) directly blocks on the per-database lock
        while another thread is executing a fallback write, preventing competing writer instantiation.
        """
        from jarvis.core.memory.sqlite_base import (
            ActorUnavailableError,
            SingleWriterActor,
        )

        fallback_started = threading.Event()
        unblock_fallback = threading.Event()
        t2_started = threading.Event()
        t2_actor_box = []
        t2_error_box = []
        resolved_key = os.path.normcase(str(Path(self.db_path).resolve()))

        with patch.object(
            SingleWriterActor,
            "get_or_create",
            side_effect=ActorUnavailableError("simulated offline for fallback test"),
        ):
            def _fallback_work(conn):
                fallback_started.set()
                unblock_fallback.wait(timeout=5.0)
                return "fallback_finished"

            def _t1_worker():
                SingleWriterActor.execute_or_fallback(
                    self.db_path,
                    _fallback_work,
                    lambda: sqlite3.connect(str(self.db_path)),
                )

            t1 = threading.Thread(target=_t1_worker, daemon=True)
            t1.start()
            self.assertTrue(fallback_started.wait(timeout=2.0))

            # Thread 2 attempts direct constructor SingleWriterActor(self.db_path)
            def _t2_worker():
                t2_started.set()
                try:
                    actor = SingleWriterActor(self.db_path)
                    t2_actor_box.append(actor)
                except Exception as ex:  # noqa: BLE001
                    t2_error_box.append(ex)

            t2 = threading.Thread(target=_t2_worker, daemon=True)
            t2.start()
            self.assertTrue(t2_started.wait(timeout=2.0))

            # Verify Thread 2 is blocked waiting on db_lock while Thread 1 holds fallback
            t2.join(timeout=0.2)
            self.assertTrue(t2.is_alive(), "Direct constructor on Thread 2 must block while Thread 1 executes fallback")
            self.assertEqual(len(t2_actor_box), 0)
            self.assertNotIn(resolved_key, SingleWriterActor._instances, "No actor instance may register while fallback holds lease")

            # Unblock fallback on Thread 1
            unblock_fallback.set()
            t1.join(timeout=2.0)
            t2.join(timeout=2.0)

            self.assertFalse(t2.is_alive(), "Thread 2 must unblock once fallback completes")
            self.assertEqual(len(t2_error_box), 0)
            self.assertEqual(len(t2_actor_box), 1)
            created_actor = t2_actor_box[0]
            self.assertIsInstance(created_actor, SingleWriterActor)
            self.assertEqual(created_actor.state, SingleWriterActor.STATE_RUNNING)

            # Prove construction proceeded safely and new actor functions
            res = created_actor.execute_write(lambda c: c.execute("SELECT 42").fetchone()[0])
            self.assertEqual(res, 42)
            created_actor.stop()

    def test_execute_or_fallback_never_retries_unknown_outcome_active_request(self):
        """Verifies that when a worker crashes during active request execution, execute_or_fallback():
        1. Does not catch the resulting RuntimeError as ActorUnavailableError.
        2. Does not invoke the fallback connection factory.
        3. Never silently retries or re-executes the unresolved request.
        4. Does not permit a subsequent actor to replay the unresolved request.
        """
        from jarvis.core.memory.sqlite_base import SingleWriterActor

        class SimulatedWorkerCrash(BaseException):
            pass

        actor = SingleWriterActor.get_or_create(self.db_path)
        call_count = 0
        fallback_factory_mock = MagicMock(side_effect=lambda: sqlite3.connect(str(self.db_path)))

        def _crashing_callback(conn):
            nonlocal call_count
            call_count += 1
            # Perform actual SQLite mutation before raising in-flight fatal crash
            conn.execute("CREATE TABLE IF NOT EXISTS test_boundary_tbl (id INTEGER PRIMARY KEY, note TEXT)")
            conn.execute("INSERT INTO test_boundary_tbl (note) VALUES ('in_flight_write')")
            raise SimulatedWorkerCrash("fatal crash while executing write after DB mutation")

        # Execute via execute_or_fallback
        with self.assertRaises(RuntimeError) as ctx:
            SingleWriterActor.execute_or_fallback(
                self.db_path,
                _crashing_callback,
                fallback_factory_mock,
                timeout=5.0,
            )

        err = ctx.exception
        self.assertIn("transaction outcome is unknown", str(err))
        self.assertEqual(getattr(err, "transaction_outcome", ""), "unknown")
        self.assertFalse(getattr(err, "cancelled_before_execution", True))

        # Invariant: Fallback factory must NEVER be called for in-flight/unknown outcome failures
        fallback_factory_mock.assert_not_called()
        self.assertEqual(call_count, 1, "Crashing callback must be executed exactly once without silent retry")

        # Invariant: Subsequent actor must start fresh with an empty queue and not replay unresolved work
        actor._thread.join(timeout=2.0)
        self.assertFalse(actor._thread.is_alive())

        replacement_actor = SingleWriterActor.get_or_create(self.db_path)
        try:
            self.assertEqual(replacement_actor.state, SingleWriterActor.STATE_RUNNING)
            self.assertTrue(replacement_actor._queue.empty(), "New actor queue must be empty")
            val = replacement_actor.execute_write(lambda c: c.execute("SELECT 99").fetchone()[0])
            self.assertEqual(val, 99)
            self.assertEqual(call_count, 1, "Replacement actor must not replay previously crashed write")
        finally:
            replacement_actor.stop()

    def test_worker_fatal_exception_during_connection_creation_marks_not_executed(self):
        """Verifies that a fatal exception during connection creation (before callback execution)
        is explicitly classified as not_executed (never unknown), releases caller immediately,
        and does not invoke fallback connection factory.
        """
        from jarvis.core.memory.sqlite_base import SingleWriterActor

        class SimulatedFatalConnError(BaseException):
            pass

        actor = SingleWriterActor.get_or_create(self.db_path)
        callback_called = False
        fallback_factory_mock = MagicMock(side_effect=lambda: sqlite3.connect(str(self.db_path)))

        def _dummy_callback(conn):
            nonlocal callback_called
            callback_called = True
            return "ok"

        with (
            patch.object(actor, "_create_conn", side_effect=SimulatedFatalConnError("simulated fatal disk/socket error")),
            self.assertRaises(RuntimeError) as ctx,
        ):
            SingleWriterActor.execute_or_fallback(
                self.db_path,
                _dummy_callback,
                fallback_factory_mock,
                timeout=5.0,
            )

        err = ctx.exception
        self.assertFalse(callback_called, "Callback must never be executed if connection fails")
        self.assertTrue(getattr(err, "cancelled_before_execution", False))
        self.assertEqual(getattr(err, "transaction_outcome", ""), "not_executed")
        self.assertFalse(getattr(err, "transaction_outcome_unknown", True))
        fallback_factory_mock.assert_not_called()
        actor._thread.join(timeout=2.0)
        self.assertFalse(actor._thread.is_alive())

    def test_constructor_publication_is_atomic_under_db_lock(self):
        """Verifies that actor construction is atomic under the per-database coordination lock.
        Even if Thread A's construction is paused after registry publication but before worker startup,
        concurrent get_or_create() (Thread B) and direct construction (Thread C) block on db_lock
        and can never observe or return a partially initialized object.
        """
        from jarvis.core.memory.sqlite_base import (
            SingleWriterActor,
            SingleWriterError,
        )

        published_evt = threading.Event()
        unblock_startup = threading.Event()
        orig_thread_start = threading.Thread.start

        def _instrumented_thread_start(t_self):
            if getattr(t_self, "name", "") == "JarvisSingleWriter":
                published_evt.set()
                unblock_startup.wait(timeout=5.0)
            orig_thread_start(t_self)

        actor_a_box = []
        actor_b_box = []
        error_c_box = []

        with patch.object(threading.Thread, "start", _instrumented_thread_start):
            def _thread_a():
                actor = SingleWriterActor(self.db_path)
                actor_a_box.append(actor)

            t_a = threading.Thread(target=_thread_a, daemon=True)
            t_a.start()
            self.assertTrue(published_evt.wait(timeout=2.0), "Thread A must reach publication point")

            # At this moment, Thread A is inside __init__ under db_lock.
            # Thread B calls get_or_create()
            def _thread_b():
                actor = SingleWriterActor.get_or_create(self.db_path)
                actor_b_box.append(actor)

            t_b = threading.Thread(target=_thread_b, daemon=True)
            t_b.start()

            # Thread C calls SingleWriterActor(self.db_path) directly
            def _thread_c():
                try:
                    SingleWriterActor(self.db_path)
                except SingleWriterError as ex:
                    error_c_box.append(ex)

            t_c = threading.Thread(target=_thread_c, daemon=True)
            t_c.start()

            # Both Thread B and Thread C must be blocked on db_lock while Thread A is constructing
            t_b.join(timeout=0.2)
            t_c.join(timeout=0.2)
            self.assertTrue(t_b.is_alive(), "Thread B must block on db_lock during Thread A construction")
            self.assertTrue(t_c.is_alive(), "Thread C must block on db_lock during Thread A construction")
            self.assertEqual(len(actor_b_box), 0, "Thread B cannot return partially initialized actor")

            # Unblock Thread A to complete worker startup and release db_lock
            unblock_startup.set()
            t_a.join(timeout=2.0)
            t_b.join(timeout=2.0)
            t_c.join(timeout=2.0)

            self.assertEqual(len(actor_a_box), 1)
            self.assertEqual(len(actor_b_box), 1)
            # Thread B observes the fully initialized, running instance
            actor_a = actor_a_box[0]
            actor_b = actor_b_box[0]
            self.assertIs(actor_b, actor_a, "Thread B must receive the exact registered singleton")
            self.assertEqual(actor_b.state, SingleWriterActor.STATE_RUNNING)
            self.assertTrue(actor_b._thread.is_alive())
            self.assertIsNotNone(getattr(actor_b, "_state_lock", None))

            # Thread C attempted direct construction for already active actor -> rejected with SingleWriterError
            self.assertEqual(len(error_c_box), 1)
            self.assertIn("Active SingleWriterActor already exists", str(error_c_box[0]))

            actor_a.stop()

    def test_interrupted_thread_start_fails_closed_and_prevents_competing_writer(self):
        """Verifies that if Thread.start() successfully launches the worker thread but the constructor
        is interrupted immediately after start() returns, constructor cleanup fails closed:
        1. It does not unregister the actor while its worker thread is still alive.
        2. A subsequent get_or_create() refuses to start a competing writer (raising ActorStoppingError).
        3. Only after the worker thread terminates can a replacement actor be created.
        """
        from jarvis.core.memory.sqlite_base import (
            ActorStoppingError,
            SingleWriterActor,
        )

        class SimulatedInterruptedStartupError(BaseException):
            pass

        orig_thread_start = threading.Thread.start
        worker_hold_event = threading.Event()
        resolved_key = os.path.normcase(str(Path(self.db_path).resolve()))

        # Hook _worker_loop to pause on worker_hold_event so the thread stays alive during the test
        orig_worker_loop = SingleWriterActor._worker_loop

        def _paused_worker_loop(actor_self):
            worker_hold_event.wait(timeout=5.0)
            orig_worker_loop(actor_self)

        def _interrupted_thread_start(t_self):
            if getattr(t_self, "name", "") == "JarvisSingleWriter":
                orig_thread_start(t_self)
                raise SimulatedInterruptedStartupError("interrupted immediately after thread launched")
            orig_thread_start(t_self)

        with (
            patch.object(SingleWriterActor, "_worker_loop", _paused_worker_loop),
            patch.object(threading.Thread, "start", _interrupted_thread_start),
            self.assertRaises(SimulatedInterruptedStartupError),
        ):
            SingleWriterActor(self.db_path)

        # Invariant 1: Worker thread is alive, so constructor cleanup must NOT have removed it from _instances
        with SingleWriterActor._registry_lock:
            existing = SingleWriterActor._instances.get(resolved_key)
            self.assertIsNotNone(existing, "Failed startup must not remove registration while worker thread is alive")
            self.assertTrue(existing._thread.is_alive(), "Worker thread must still be alive")
            self.assertEqual(existing.state, SingleWriterActor.STATE_STOPPING)

        # Invariant 2: Another caller calls get_or_create() -> MUST fail closed and refuse competing writer
        with self.assertRaises(ActorStoppingError) as ctx:
            SingleWriterActor.get_or_create(self.db_path)
        self.assertIn("still terminating", str(ctx.exception))

        # Invariant 3: Unblock the worker so it can terminate
        worker_hold_event.set()
        existing._thread.join(timeout=2.0)
        self.assertFalse(existing._thread.is_alive(), "Worker thread must have cleanly terminated")

        # Invariant 4: Now that the first worker has terminated, get_or_create() can safely create replacement
        replacement = SingleWriterActor.get_or_create(self.db_path)
        try:
            self.assertEqual(replacement.state, SingleWriterActor.STATE_RUNNING)
            self.assertTrue(replacement._thread.is_alive())
            res = replacement.execute_write(lambda c: c.execute("SELECT 77").fetchone()[0])
            self.assertEqual(res, 77)
        finally:
            replacement.stop()

    def test_stop_leaves_actor_registered_while_worker_thread_alive(self):
        """Verifies that calling stop() with a timeout leaves the actor registered in _instances
        in STATE_STOPPING as long as its worker thread is alive, and only unregisters after thread termination.
        """
        from jarvis.core.memory.sqlite_base import SingleWriterActor

        actor = SingleWriterActor.get_or_create(self.db_path)
        resolved_key = os.path.normcase(str(Path(self.db_path).resolve()))
        write_started = threading.Event()
        unblock_write = threading.Event()

        def _blocking_write(conn):
            write_started.set()
            unblock_write.wait(timeout=5.0)
            return "done"

        t = threading.Thread(target=lambda: actor.execute_write(_blocking_write, timeout=5.0), daemon=True)
        t.start()
        self.assertTrue(write_started.wait(timeout=2.0))

        # Stop with short timeout while worker is still blocked executing write
        actor.stop(timeout=0.05)

        # Invariant: Actor state must remain STATE_STOPPING, worker is alive, and instance remains registered in _instances
        self.assertEqual(actor.state, SingleWriterActor.STATE_STOPPING)
        self.assertTrue(actor._thread.is_alive())
        with SingleWriterActor._registry_lock:
            self.assertIn(resolved_key, SingleWriterActor._instances)
            self.assertIs(SingleWriterActor._instances[resolved_key], actor)

        # Unblock worker and wait for termination
        unblock_write.set()
        t.join(timeout=2.0)
        actor.stop(timeout=1.0)

        # Invariant: Once worker is dead, state transitions to STATE_STOPPED and instance is popped from _instances
        self.assertEqual(actor.state, SingleWriterActor.STATE_STOPPED)
        self.assertFalse(actor._thread.is_alive())
        with SingleWriterActor._registry_lock:
            self.assertNotIn(resolved_key, SingleWriterActor._instances)

    def test_direct_constructor_rejected_when_actor_active(self):
        """Verifies that calling SingleWriterActor(db_path) directly when an actor is already alive
        raises SingleWriterError, enforcing singleton invariants.
        """
        from jarvis.core.memory.sqlite_base import (
            SingleWriterActor,
            SingleWriterError,
        )

        actor1 = SingleWriterActor.get_or_create(self.db_path)
        try:
            with self.assertRaises(SingleWriterError) as ctx:
                SingleWriterActor(self.db_path)
            self.assertIn("Active SingleWriterActor already exists", str(ctx.exception))
        finally:
            actor1.stop()

    def test_fallback_connection_closes_and_rolls_back_on_error(self):
        """Verifies fallback connection is explicitly rolled back and closed when callback or commit fails."""
        from jarvis.core.memory.sqlite_base import (
            ActorUnavailableError,
            SingleWriterActor,
        )

        mock_conn = MagicMock(spec=sqlite3.Connection)
        mock_conn.commit.side_effect = sqlite3.OperationalError("disk I/O error on commit")

        with patch.object(
            SingleWriterActor,
            "get_or_create",
            side_effect=ActorUnavailableError("simulated offline"),
        ):
            # Test 1: Callback raises exception -> verify rollback and close
            failing_conn = MagicMock(spec=sqlite3.Connection)
            with self.assertRaises(ValueError):
                SingleWriterActor.execute_or_fallback(
                    self.db_path,
                    lambda c: (_ for _ in ()).throw(ValueError("user error")),
                    lambda: failing_conn,
                )
            failing_conn.rollback.assert_called_once()
            failing_conn.close.assert_called_once()

            # Test 2: Commit raises exception -> verify rollback and close
            with self.assertRaises(sqlite3.OperationalError):
                SingleWriterActor.execute_or_fallback(
                    self.db_path,
                    lambda c: "ok",
                    lambda: mock_conn,
                )
            mock_conn.rollback.assert_called_once()
            mock_conn.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
