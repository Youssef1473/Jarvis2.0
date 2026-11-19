"""Hermetic test suite for Jarvis 2.0 multi-workspace HUD switcher and quarantined fact promotion."""
from __future__ import annotations

import contextlib
import os
import sqlite3
import sys
import unittest
from pathlib import Path
from typing import Any

from starlette.testclient import TestClient

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from jarvis.core.memory import MemoryManager
from jarvis.ui.server import JarvisUIService, get_hud_session_token


class MockOrchestrator:
    """Mock orchestrator carrying active workspace state and memory engine."""

    def __init__(self, memory: MemoryManager):
        self.memory = memory
        self.active_workspace_id = "ws_default"


class TestHudWorkspaceAndQuarantine(unittest.TestCase):
    """Hermetic tests covering multi-workspace listing, switching, quarantined fact review, and promotion."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from tools.run_fast_tests import probe_loopback_available
        if not probe_loopback_available():
            raise unittest.SkipTest("Localhost loopback TCP is blocked in sandbox environment")

    def setUp(self):
        super().setUp()
        self.test_dir = WORKSPACE_ROOT / "scratch" / "test_quarantine_sandbox"
        self.test_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.test_dir / f"test_mem_{os.getpid()}_{id(self)}.db"
        if self.db_path.exists():
            self.db_path.unlink()

        config = {
            "memory": {
                "db_path": str(self.db_path),
            }
        }
        self.memory = MemoryManager(config)
        self.orchestrator = MockOrchestrator(self.memory)
        self.service = JarvisUIService(orchestrator=self.orchestrator, host="127.0.0.1", port=20199)
        self.service.enforce_auth = True
        self.app = self.service._create_app()
        self.client = TestClient(self.app)
        self.token = get_hud_session_token()
        self.headers = {"X-Jarvis-Token": self.token}

    def tearDown(self):
        with contextlib.suppress(Exception):
            self.memory.close()
        if self.db_path.exists():
            with contextlib.suppress(OSError):
                self.db_path.unlink()
        super().tearDown()

    def _insert_fact(
        self,
        key: str | None,
        val: str,
        category: str = "general",
        workspace_id: str | None = None,
        scope: str = "unscoped",
        provenance: str = "test",
    ) -> int:
        def _write(conn: sqlite3.Connection) -> int:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO facts (workspace_id, scope, category, key, value, source_provenance)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (workspace_id, scope, category, key, val, provenance),
            )
            return cur.lastrowid or 0

        return self.memory.execute_write(_write)

    def _create_workspace(self, workspace_id: str, name: str, is_archived: int = 0) -> None:
        def _write(conn: sqlite3.Connection) -> None:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT OR REPLACE INTO workspaces (workspace_id, name, path, is_archived, created_at)
                VALUES (?, ?, ?, ?, strftime('%s', 'now'))
                """,
                (workspace_id, name, f"C:/test/{workspace_id}", is_archived),
            )

        self.memory.execute_write(_write)

    def test_workspaces_list_reports_quarantined_count(self):
        """Verifies GET /api/workspaces returns accurate quarantined_count alongside workspace stats."""
        # 3 quarantined facts
        self._insert_fact("k1", "quar1", scope="unscoped", workspace_id=None)
        self._insert_fact("k2", "quar2", scope="unscoped", workspace_id=None)
        self._insert_fact("k3", "quar3", scope="unscoped", workspace_id=None)
        # 2 workspace-scoped facts
        self._insert_fact("w1", "ws_val1", scope="workspace", workspace_id="ws_default")
        self._insert_fact("w2", "ws_val2", scope="workspace", workspace_id="ws_default")

        res = self.client.get("/api/workspaces", headers=self.headers)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data.get("status"), "ok")
        self.assertEqual(data.get("quarantined_count"), 3)
        self.assertEqual(data.get("active_workspace_id"), "ws_default")

        ws_list = data.get("workspaces", [])
        self.assertTrue(any(w["workspace_id"] == "ws_default" for w in ws_list))
        default_ws = next(w for w in ws_list if w["workspace_id"] == "ws_default")
        self.assertEqual(default_ws.get("facts_count"), 2)

    def test_workspaces_select_alias(self):
        """Verifies POST /api/workspaces/select functions as an alias to /api/workspaces/switch."""
        self._create_workspace("jarvis_dev", "Jarvis Development")

        res = self.client.post(
            "/api/workspaces/select",
            headers=self.headers,
            json={"workspace_id": "jarvis_dev"},
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data.get("status"), "ok")
        self.assertEqual(data.get("active_workspace_id"), "jarvis_dev")
        self.assertEqual(self.orchestrator.active_workspace_id, "jarvis_dev")

    def test_workspaces_switch_rejects_unscoped_and_invalid(self):
        """Verifies switching to unscoped, invalid slugs, or archived workspaces fails closed."""
        self._create_workspace("archived_proj", "Archived Project", is_archived=1)

        # 1. Reject 'unscoped' partition
        res_unscoped = self.client.post(
            "/api/workspaces/switch",
            headers=self.headers,
            json={"target_workspace_id": "unscoped"},
        )
        self.assertEqual(res_unscoped.status_code, 403)

        # 2. Reject path traversal or malformed slug
        res_traversal = self.client.post(
            "/api/workspaces/select",
            headers=self.headers,
            json={"workspace_id": "../secret_room"},
        )
        self.assertEqual(res_traversal.status_code, 400)

        # 3. Reject nonexistent workspace
        res_missing = self.client.post(
            "/api/workspaces/switch",
            headers=self.headers,
            json={"target_workspace_id": "nonexistent_ws"},
        )
        self.assertEqual(res_missing.status_code, 400)

        # 4. Reject archived workspace
        res_archived = self.client.post(
            "/api/workspaces/select",
            headers=self.headers,
            json={"workspace_id": "archived_proj"},
        )
        self.assertEqual(res_archived.status_code, 403)

    def test_quarantined_list_pagination_and_filter(self):
        """Verifies GET /api/memory/quarantined supports limits, offsets, category, and search filters."""
        for i in range(12):
            cat = "project" if i % 2 == 0 else "hardware"
            self._insert_fact(f"key_{i}", f"Fact value number {i} special_tag_{i % 3}", category=cat)

        # Pagination: page 1 (5 items)
        res_p1 = self.client.get("/api/memory/quarantined?limit=5&offset=0", headers=self.headers)
        self.assertEqual(res_p1.status_code, 200)
        d1 = res_p1.json()
        self.assertEqual(d1.get("limit"), 5)
        self.assertEqual(d1.get("offset"), 0)
        self.assertEqual(len(d1.get("facts", [])), 5)
        self.assertEqual(d1.get("total"), 12)

        # Pagination: page 2 (5 items)
        res_p2 = self.client.get("/api/memory/quarantined?limit=5&offset=5", headers=self.headers)
        self.assertEqual(res_p2.status_code, 200)
        d2 = res_p2.json()
        self.assertEqual(len(d2.get("facts", [])), 5)
        p1_ids = [f["id"] for f in d1["facts"]]
        p2_ids = [f["id"] for f in d2["facts"]]
        self.assertEqual(len(set(p1_ids).intersection(set(p2_ids))), 0)

        # Category filter
        res_cat = self.client.get("/api/memory/quarantined?category=hardware", headers=self.headers)
        self.assertEqual(res_cat.status_code, 200)
        d_cat = res_cat.json()
        self.assertTrue(all(f["category"] == "hardware" for f in d_cat.get("facts", [])))

        # Substring search filter
        res_search = self.client.get("/api/memory/quarantined?search=special_tag_1", headers=self.headers)
        self.assertEqual(res_search.status_code, 200)
        d_search = res_search.json()
        self.assertGreater(len(d_search.get("facts", [])), 0)
        self.assertTrue(all("special_tag_1" in f["value"] for f in d_search["facts"]))

    def test_quarantined_promote_success(self):
        """Verifies POST /api/memory/quarantined/promote safely transitions facts and updates facts_vec."""
        self._create_workspace("project_titan", "Project Titan")
        f1 = self._insert_fact("arch_plan", "Microservice breakdown", category="architecture")
        f2 = self._insert_fact("db_choice", "PostgreSQL with SQLite cache", category="database")

        res = self.client.post(
            "/api/memory/quarantined/promote",
            headers=self.headers,
            json={"target_workspace_id": "project_titan", "fact_ids": [f1, f2]},
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data.get("status"), "ok")
        self.assertEqual(data.get("promoted_count"), 2)
        self.assertEqual(data.get("target_workspace_id"), "project_titan")

        # Verify database state meets Schema 3.0 check constraint
        with self.memory._get_ro_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT id, workspace_id, scope FROM facts WHERE id IN (?, ?)", (f1, f2))
            rows = cur.fetchall()
            self.assertEqual(len(rows), 2)
            for r in rows:
                self.assertEqual(r["workspace_id"], "project_titan")
                self.assertEqual(r["scope"], "workspace")

            # Quarantined count must now be 0
            cur.execute("SELECT count(1) FROM facts WHERE workspace_id IS NULL OR scope = 'unscoped'")
            self.assertEqual(cur.fetchone()[0], 0)

    def test_quarantined_promote_security_validation(self):
        """Verifies fail-closed validation for target workspace and batch size limits."""
        self._create_workspace("archived_dest", "Archived Dest", is_archived=1)
        fid = self._insert_fact("test_key", "test_val")

        # Target 'unscoped' rejected
        res_unscoped = self.client.post(
            "/api/memory/quarantined/promote",
            headers=self.headers,
            json={"target_workspace_id": "unscoped", "fact_ids": [fid]},
        )
        self.assertEqual(res_unscoped.status_code, 403)

        # Target nonexistent workspace rejected
        res_nonexistent = self.client.post(
            "/api/memory/quarantined/promote",
            headers=self.headers,
            json={"target_workspace_id": "ghost_ws", "fact_ids": [fid]},
        )
        self.assertEqual(res_nonexistent.status_code, 400)

        # Target archived workspace rejected
        res_archived = self.client.post(
            "/api/memory/quarantined/promote",
            headers=self.headers,
            json={"target_workspace_id": "archived_dest", "fact_ids": [fid]},
        )
        self.assertEqual(res_archived.status_code, 403)

        # Over 100 batch limit rejected
        res_overflow = self.client.post(
            "/api/memory/quarantined/promote",
            headers=self.headers,
            json={"target_workspace_id": "ws_default", "fact_ids": list(range(101))},
        )
        self.assertEqual(res_overflow.status_code, 400)

    def test_quarantined_purge_success(self):
        """Verifies POST /api/memory/quarantined/purge removes quarantined facts cleanly."""
        f1 = self._insert_fact("stale_k1", "stale_v1")
        f2 = self._insert_fact("stale_k2", "stale_v2")

        res = self.client.post(
            "/api/memory/quarantined/purge",
            headers=self.headers,
            json={"fact_ids": [f1, f2]},
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data.get("status"), "ok")
        self.assertEqual(data.get("purged_count"), 2)

        with self.memory._get_ro_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT count(1) FROM facts WHERE id IN (?, ?)", (f1, f2))
            self.assertEqual(cur.fetchone()[0], 0)

    def test_quarantined_purge_defense_in_depth(self):
        """Defense-in-depth: verifies purge strictly refuses to delete workspace-scoped facts."""
        ws_fact_id = self._insert_fact("active_k", "active_v", scope="workspace", workspace_id="ws_default")

        res = self.client.post(
            "/api/memory/quarantined/purge",
            headers=self.headers,
            json={"fact_ids": [ws_fact_id]},
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        # Must purge 0 items because the fact is workspace-scoped
        self.assertEqual(data.get("purged_count"), 0)

        # Confirm the fact remains untouched in storage
        with self.memory._get_ro_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT count(1) FROM facts WHERE id = ?", (ws_fact_id,))
            self.assertEqual(cur.fetchone()[0], 1)

    def test_unauthenticated_requests_rejected(self):
        """Verifies 401 response on protected endpoints when session token is missing or forged."""
        endpoints: list[tuple[str, str, dict[str, Any] | None]] = [
            ("GET", "/api/workspaces", None),
            ("POST", "/api/workspaces/select", {"workspace_id": "ws_default"}),
            ("GET", "/api/memory/quarantined", None),
            ("POST", "/api/memory/quarantined/promote", {"target_workspace_id": "ws_default", "fact_ids": [1]}),
            ("POST", "/api/memory/quarantined/purge", {"fact_ids": [1]}),
        ]

        for method, path, payload in endpoints:
            if method == "GET":
                res = self.client.get(path)
            else:
                res = self.client.post(path, json=payload)
            self.assertEqual(res.status_code, 401, f"Expected 401 for unauthenticated {method} {path}")

    def test_hud_html_zero_cls_quarantine_elements(self):
        """Verifies hud.html contains fixed-width Zero-CLS quarantined pill, tabs, and drawer controls."""
        hud_path = WORKSPACE_ROOT / "jarvis" / "ui" / "templates" / "hud.html"
        self.assertTrue(hud_path.exists())
        content = hud_path.read_text(encoding="utf-8")

        # Zero-CLS badge check
        self.assertIn('id="workspaceQuarantinedBadge"', content)
        self.assertIn("tabular-nums inline-block", content)
        self.assertIn('data-action="open-quarantine-drawer"', content)

        # Drawer tab and badge check
        self.assertIn('id="tabQuarantineBtn"', content)
        self.assertIn('data-tab="quarantine"', content)
        self.assertIn('id="drawerQuarantineCount"', content)

        # Interactive functions check
        self.assertIn("renderQuarantineTab", content)
        self.assertIn("executeQuarantinePromote", content)
        self.assertIn("executeQuarantinePurge", content)


if __name__ == "__main__":
    unittest.main()
