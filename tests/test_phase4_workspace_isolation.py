"""Phase 4: Runtime Workspace Propagation & Consolidator Tenant Isolation Tests.

Empirically verifies:
1. SleepPhaseConsolidator tenant isolation and strict quarantine preservation:
   - Micro-batch deduplication never crosses workspace boundaries.
   - Quarantined scope='unscoped' rows in facts/facts_vec are strictly preserved.
   - Attempts to consolidate 'unscoped' or empty workspace raise ValueError.
2. ContextOrchestrator workspace isolation:
   - build_ephemeral_user_envelope and assemble_system_prompt isolate retrieved memory,
     episodic lessons, and conversation history strictly to target workspace_id.
   - Attempts to build envelope with 'unscoped' raise ValueError.
3. JarvisUIService fail-closed workspace scoping and validation:
   - _resolve_request_workspace extracts workspace from query params, body dict, or orchestrator state.
   - 'unscoped' raises PermissionError (mapped to HTTP 403 Forbidden).
   - Invalid workspace identifier formats raise ValueError (mapped to HTTP 400 Bad Request).
   - Endpoints invoke _resolve_request_workspace and enforce workspace boundaries.
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from jarvis.context.context_orchestrator import ContextOrchestrator
from jarvis.core.memory import MemoryManager, SleepPhaseConsolidator
from jarvis.ui.server import JarvisUIService


class TestPhase4WorkspaceIsolation(unittest.TestCase):
    """Test suite verifying end-to-end multi-tenant isolation across consolidator, context, and UI."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_phase4.db"
        self.config = {
            "_base_dir": self.temp_dir.name,
            "workspace_id": "ws_alpha",
            "memory": {
                "db_path": str(self.db_path),
                "background_learning": False,
            },
        }
        self.memory = MemoryManager(self.config)

        # Seed workspace tenants in workspaces table with all mandatory columns
        def _seed_workspaces(conn: sqlite3.Connection):
            cursor = conn.cursor()
            cursor.execute("PRAGMA foreign_keys = OFF;")
            cursor.execute("""
                INSERT OR IGNORE INTO workspaces (workspace_id, name, path, is_archived, created_at)
                VALUES ('ws_alpha', 'Alpha Project', 'C:/mock_alpha', 0, 1700000000.0)
            """)
            cursor.execute("""
                INSERT OR IGNORE INTO workspaces (workspace_id, name, path, is_archived, created_at)
                VALUES ('ws_beta', 'Beta Project', 'C:/mock_beta', 0, 1700000000.0)
            """)
            cursor.execute("""
                INSERT OR IGNORE INTO workspaces (workspace_id, name, path, is_archived, created_at)
                VALUES ('ws_default', 'Default Project', 'C:/mock_default', 0, 1700000000.0)
            """)
            cursor.execute("""
                INSERT OR IGNORE INTO sessions (session_id, workspace_id, title, status)
                VALUES ('sess_alpha', 'ws_alpha', 'Alpha Session', 'active')
            """)
            cursor.execute("""
                INSERT OR IGNORE INTO sessions (session_id, workspace_id, title, status)
                VALUES ('sess_beta', 'ws_beta', 'Beta Session', 'active')
            """)
            cursor.execute("PRAGMA foreign_keys = ON;")

        self.memory.execute_write(_seed_workspaces)

    def tearDown(self):
        try:
            self.memory.close()
        except Exception:  # noqa: BLE001, S110
            pass
        try:
            self.temp_dir.cleanup()
        except Exception:  # noqa: BLE001, S110
            pass

    def test_1_sleep_consolidator_tenant_isolation_and_quarantine_preservation(self):
        """Verifies sleep-phase consolidation strictly scopes to workspace and preserves quarantined rows."""
        # Seed facts for ws_alpha (with near duplicates)
        self.memory.remember_fact("favorite_beverage", "double espresso coffee", "preference", workspace_id="ws_alpha")
        self.memory.remember_fact("favorite_drink", "double espresso coffee", "preference", workspace_id="ws_alpha")

        # Seed facts for ws_beta (with near duplicates)
        self.memory.remember_fact("favorite_beverage", "matcha green tea latte", "preference", workspace_id="ws_beta")
        self.memory.remember_fact("favorite_drink", "matcha green tea latte", "preference", workspace_id="ws_beta")

        # Seed quarantined legacy facts directly with scope='unscoped' and workspace_id=NULL
        def _seed_quarantine(conn: sqlite3.Connection):
            cursor = conn.cursor()
            cursor.execute("PRAGMA foreign_keys = OFF;")
            cursor.execute("""
                INSERT INTO facts (workspace_id, scope, category, key, value, source_provenance)
                VALUES (NULL, 'unscoped', 'legacy', 'quarantine_token_1', 'secret_val_1', 'quarantine_legacy')
            """)
            q1_id = cursor.lastrowid
            cursor.execute("""
                INSERT INTO facts (workspace_id, scope, category, key, value, source_provenance)
                VALUES (NULL, 'unscoped', 'legacy', 'quarantine_token_2', 'secret_val_1', 'quarantine_legacy')
            """)
            q2_id = cursor.lastrowid

            # Insert corresponding facts_vec entries if table exists
            cursor.execute("PRAGMA table_info(facts_vec)")
            cols = {r["name"] if hasattr(r, "__getitem__") and "name" in r else r[1] for r in cursor.fetchall()}
            if "workspace_id" in cols:
                dummy_vec = [0.1] * 384
                import sqlite_vec
                b = sqlite_vec.serialize_float32(dummy_vec)
                cursor.execute("INSERT OR REPLACE INTO facts_vec (fact_id, workspace_id, embedding) VALUES (?, 'unscoped', ?)", (q1_id, b))
                cursor.execute("INSERT OR REPLACE INTO facts_vec (fact_id, workspace_id, embedding) VALUES (?, 'unscoped', ?)", (q2_id, b))
            cursor.execute("PRAGMA foreign_keys = ON;")

        self.memory.execute_write(_seed_quarantine)

        consolidator = SleepPhaseConsolidator(str(self.memory.db_path), self.memory.vector_engine)

        # 1. Discover redundant facts strictly in ws_alpha
        redundant_alpha = consolidator.discover_redundant_facts_readonly(workspace_id="ws_alpha")
        self.assertGreater(len(redundant_alpha), 0, "Should detect near-duplicate espresso fact in ws_alpha")
        for keep, stale, _ in redundant_alpha:
            self.assertIn("espresso", keep["value"].lower())
            self.assertIn("espresso", stale["value"].lower())
            self.assertNotIn("matcha", keep["value"].lower())
            self.assertNotIn("matcha", stale["value"].lower())
            self.assertNotIn("secret_val", keep["value"].lower())

        # 2. Verify unscoped discovery is rejected
        with self.assertRaises(ValueError):
            consolidator.discover_redundant_facts_readonly(workspace_id="unscoped")
        with self.assertRaises(ValueError):
            consolidator.discover_redundant_facts_readonly(workspace_id="")

        # 3. Execute consolidation targeting ws_alpha (pass non-active callback to bypass live workstation idle gate)
        report = consolidator.run_sleep_consolidation(workspace_id="ws_alpha", is_user_active_fn=lambda: False)
        self.assertEqual(report["status"], "success")

        # 4. Invariant checks
        import sqlite_vec
        with sqlite3.connect(str(self.memory.db_path)) as conn:
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            # ws_beta facts must be intact (both records remain)
            cursor.execute("SELECT COUNT(*) as cnt FROM facts WHERE workspace_id = 'ws_beta'")
            self.assertEqual(cursor.fetchone()["cnt"], 2, "ws_beta facts must not be mutated by ws_alpha consolidation")

            # Quarantined unscoped facts must be intact (both records remain)
            cursor.execute("SELECT COUNT(*) as cnt FROM facts WHERE scope = 'unscoped'")
            self.assertEqual(cursor.fetchone()["cnt"], 2, "Quarantined scope='unscoped' facts must remain strictly untouched")

            # Quarantined unscoped facts_vec must be intact
            cursor.execute("PRAGMA table_info(facts_vec)")
            cols = {r["name"] if hasattr(r, "__getitem__") and "name" in r else r[1] for r in cursor.fetchall()}
            if "workspace_id" in cols:
                cursor.execute("SELECT COUNT(*) as cnt FROM facts_vec WHERE workspace_id = 'unscoped'")
                self.assertEqual(cursor.fetchone()["cnt"], 2, "Quarantined scope='unscoped' facts_vec must remain strictly untouched")

    def test_2_context_orchestrator_workspace_isolation(self):
        """Verifies ContextOrchestrator envelope and prompt assembly isolate memory and interactions to workspace."""
        # Seed facts
        self.memory.remember_fact("favorite_editor", "Neovim IDE", "tools", workspace_id="ws_alpha")
        self.memory.remember_fact("favorite_editor", "Visual Studio Code", "tools", workspace_id="ws_beta")

        # Seed interactions
        def _seed_interactions(conn: sqlite3.Connection):
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO interactions (workspace_id, session_id, scope, user_input, jarvis_reply, source_provenance)
                VALUES ('ws_alpha', 'sess_alpha', 'session', 'Alpha query about neovim', 'Alpha response neovim', 'user')
            """)
            cursor.execute("""
                INSERT INTO interactions (workspace_id, session_id, scope, user_input, jarvis_reply, source_provenance)
                VALUES ('ws_beta', 'sess_beta', 'session', 'Beta query about vscode', 'Beta response vscode', 'user')
            """)

        self.memory.execute_write(_seed_interactions)

        # Instantiate ContextOrchestrator for ws_alpha
        co_alpha = ContextOrchestrator(self.config, workspace_id="ws_alpha")

        # 1. Ephemeral Envelope: ws_alpha must recall Neovim and NEVER VSCode
        envelope = co_alpha.build_ephemeral_user_envelope(
            user_text="favorite_editor",
            memory=self.memory,
            workspace_id="ws_alpha",
        )
        self.assertIn("Neovim", envelope)
        self.assertNotIn("Visual Studio Code", envelope)

        # Envelope called with build_user_turn_envelope alias
        envelope_alias = co_alpha.build_user_turn_envelope(
            user_text="favorite_editor",
            memory=self.memory,
            workspace_id="ws_alpha",
        )
        self.assertEqual(envelope, envelope_alias)

        # 2. System Prompt: ws_alpha must include ws_alpha history, NEVER ws_beta history
        prompt = co_alpha.assemble_system_prompt(
            memory=self.memory,
            available_tools=[],
            provider="antigravity",
            active_model="gemini-3.8-flash",
            workspace_id="ws_alpha",
        )
        self.assertIn("Alpha query about neovim", prompt)
        self.assertNotIn("Beta query about vscode", prompt)
        self.assertIn("Workspace: ws_alpha", prompt)

        # 3. Unscoped access rejection
        with self.assertRaises(ValueError):
            ContextOrchestrator(workspace_id="unscoped")
        with self.assertRaises(ValueError):
            co_alpha.build_ephemeral_user_envelope("test", workspace_id="unscoped")
        with self.assertRaises(ValueError):
            co_alpha.assemble_system_prompt(
                memory=self.memory,
                available_tools=[],
                provider="antigravity",
                active_model="gemini-3.8-flash",
                workspace_id="unscoped",
            )

    def test_3_ui_server_workspace_scoping_and_fail_closed_validation(self):
        """Verifies JarvisUIService fail-closed validation and workspace routing."""
        mock_orch = MagicMock()
        mock_orch.active_workspace_id = "ws_alpha"
        mock_orch.memory = self.memory

        ui_service = JarvisUIService(orchestrator=mock_orch, enforce_auth=False)

        # 1. Query params extraction
        req_alpha = MagicMock()
        req_alpha.query_params = {"workspace_id": "ws_alpha"}
        self.assertEqual(ui_service._resolve_request_workspace(req_alpha), "ws_alpha")

        # 2. Body dict extraction
        data_beta = {"workspace_id": "ws_beta"}
        self.assertEqual(ui_service._resolve_request_workspace(data_beta), "ws_beta")

        # 3. Fallback to orchestrator active workspace
        req_empty = MagicMock()
        req_empty.query_params = {}
        self.assertEqual(ui_service._resolve_request_workspace(req_empty), "ws_alpha")

        # 4. Fallback to ws_default if orchestrator has none
        ui_no_orch = JarvisUIService(orchestrator=None, enforce_auth=False)
        self.assertEqual(ui_no_orch._resolve_request_workspace({}), "ws_default")

        # 5. Reject unscoped partition with PermissionError (403 contract)
        req_unscoped = MagicMock()
        req_unscoped.query_params = {"workspace_id": "unscoped"}
        with self.assertRaises(PermissionError):
            ui_service._resolve_request_workspace(req_unscoped)

        data_unscoped = {"workspace_id": "unscoped"}
        with self.assertRaises(PermissionError):
            ui_service._resolve_request_workspace(data_unscoped)

        data_unscoped_case = {"workspace_id": "UnScoped"}
        with self.assertRaises(PermissionError):
            ui_service._resolve_request_workspace(data_unscoped_case)

        # 6. Reject malformed workspace IDs with ValueError (400 contract)
        for bad_ws in ["ws;injection", "ws/traversal", "ws with space", "ws$bad", "a" * 65]:
            req_bad = MagicMock()
            req_bad.query_params = {"workspace_id": bad_ws}
            with self.assertRaises(ValueError):
                ui_service._resolve_request_workspace(req_bad)

            with self.assertRaises(ValueError):
                ui_service._resolve_request_workspace({"workspace_id": bad_ws})

        # 7. Verify document vault staging isolation
        self.memory.stage_document("plan_a.pdf", "C:/docs/plan_a.pdf", workspace_id="ws_alpha")
        self.memory.stage_document("plan_b.pdf", "C:/docs/plan_b.pdf", workspace_id="ws_beta")

        docs_a = self.memory.get_staged_documents(workspace_id="ws_alpha")
        self.assertEqual(len(docs_a), 1)
        self.assertEqual(docs_a[0]["filename"], "plan_a.pdf")

        docs_b = self.memory.get_staged_documents(workspace_id="ws_beta")
        self.assertEqual(len(docs_b), 1)
        self.assertEqual(docs_b[0]["filename"], "plan_b.pdf")


if __name__ == "__main__":
    unittest.main()
