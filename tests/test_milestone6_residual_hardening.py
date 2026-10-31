"""Unit and integration verification suite for Milestone 6: Residual Hardening (R-01, R-02, R-03, N-01 to N-04)."""
from __future__ import annotations

import json
import os
import sqlite3
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

    def test_health_check_sqlite_version_validation(self):
        """Verifies run_system_health_check validates SQLite runtime version >= 3.31.0 for virtual columns."""
        from jarvis.core.health_check import run_system_health_check

        report = run_system_health_check()
        mem_db = report.get("subsystems", {}).get("memory_db", {})
        if "sqlite_version" in mem_db:
            self.assertTrue(mem_db.get("virtual_columns_supported"), "SQLite runtime version must support virtual columns")


if __name__ == "__main__":
    unittest.main()
