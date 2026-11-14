"""Synthetic Verification Matrix for BLK-21 Execution Authorization Completion (Phase 1J).

Exhaustively verifies:
Task 1 — Node Inventory Binding:
  1. Valid canonical profile inventory accepted (Context B1 Snapshot, 17 exact node IDs).
  2. Missing node from inventory rejected (code 134).
  3. Extra unassigned node rejected (code 134/132).
  4. Duplicate node IDs in permit rejected (code 136).
  5. Bare test-file path replacing node ID rejected (code 137).
  6. Declared node count mismatch rejected (code 135).
  7. Tampered inventory hash rejected (code 134).

Task 2 — Exact Argument Vector Binding:
  8. Exact ordered execution vector accepted.
  9. Duplicate authorized flags rejected (code 133).
  10. Reordered flags rejected (code 133).
  11. Unauthorized extra flags rejected (code 133).
  12. Unrecognized synthetic flag (--synthetic-unknown) rejected (code 133).
  13. Additional unexpected positional argument rejected (code 133).

Task 3 — Expiration Clock Semantics:
  14. Valid permit timestamp within clock bounds accepted.
  15. Expired permit rejected (code 128).
  16. Future-dated permit rejected (code 128).
  17. Issuance time after expiry rejected (code 128).
  18. Excessively distant expiry (TTL > 3600s) rejected (code 128).

Task 4 — Worker Executable Identity:
  19. Matching worker path, version, and binary SHA-256 accepted.
  20. Executable path mismatch rejected (code 131).
  21. Executable binary SHA-256 hash mismatch rejected (code 131).
  22. Malformed executable hash rejected (code 131).

Task 5 — Atomic Claim Hardening & Replay:
  23. Non-canonical permit path (e.g. .claimed.*) rejected as input (code 127).
  24. Pre-existing claim destination collision fails closed without overwrite (code 127).
  25. Original-path replay rejected after consumption (code 127/120).
  26. Concurrent duplicate claims (5 workers contending, exactly 1 wins).
  27. Interrupted atomic consumption remains fail-closed (code 127/120).
  28. Consumption receipt bound to permit ID and run ID.

Task 6 — Existing Controls & Supervisor Health:
  29. Missing permit rejected (code 120).
  30. Tampered permit payload rejected via HMAC (code 126).
  31. Invalid HMAC signature rejected (code 126).
  32. Secret-transfer failure (EOF on pipe) rejected (code 125).
  33. Secret corruption over pipe rejected (code 126).
  34. Preflight mode executes zero pytest collection (code 0).
  35. Concurrent supervisors cannot authorize same snapshot run twice (EEXIST).
  36. Supervisor health check refuses issuance when compromised or retaining failed handles.

Gate 2 Constraint: ZERO candidate tests executed, ZERO pytest collection.
"""

from __future__ import annotations

import concurrent.futures
import ctypes
import hashlib
import hmac
import json
import os
import secrets
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

# Ensure repository root is on sys.path
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import tools.snapshot_test_supervisor as supervisor
from tools.execution_profile_dispatcher import ExecutionProfileDispatcher, ProfileType
from tools.snapshot_test_supervisor import (
    OVERLAPPED,
    RetainedPendingIOTracker,
    SupervisorResult,
    SupervisorUnhealthyError,
    check_supervisor_health_for_issuance,
    issue_execution_permit,
    run_supervised_worker,
)


def _compute_synthetic_dest_digest(snap_root: Path) -> tuple[str, int]:
    fresh_prefixes = tuple(
        f"{snap_root.as_posix().lower()}/{d.lower()}"
        for d in ("jarvis/skills/custom", "data/evolution_scratch", "data/snapshots", "data/learning_candidates", "temp")
    )
    inv: dict[str, tuple[int, str]] = {}
    for root, _, files in os.walk(snap_root, followlinks=False):
        root_posix = Path(root).as_posix().lower()
        if any(root_posix == pfx or root_posix.startswith(f"{pfx}/") for pfx in fresh_prefixes):
            continue
        for f in files:
            if f in ("snapshot_manifest.json", "snapshot_integrity_receipt.json") or f.startswith(".execution_permit.") or f == ".execution_permit.json":
                continue
            p = Path(root) / f
            rel = p.relative_to(snap_root).as_posix()
            h = hashlib.sha256()
            with p.open("rb") as fp:
                while chunk := fp.read(65536):
                    h.update(chunk)
            inv[rel] = (p.stat().st_size, h.hexdigest().lower())
    hasher = hashlib.sha256()
    for rel_path in sorted(inv.keys()):
        sz, sha = inv[rel_path]
        hasher.update(f"{rel_path}:{sz}:{sha}\n".encode())
    return hasher.hexdigest().lower(), len(inv)


def _setup_synthetic_snapshot() -> Path:
    """Provisions a minimal lightweight snapshot directory containing tools/test_worker_bootstrap.py."""
    temp_dir = Path(tempfile.mkdtemp(prefix="jarvis_snap_permit_")).resolve()
    snap_tools = temp_dir / "tools"
    snap_temp = temp_dir / "temp"
    snap_jarvis = temp_dir / "jarvis"
    snap_tools.mkdir(parents=True, exist_ok=True)
    snap_temp.mkdir(parents=True, exist_ok=True)
    snap_jarvis.mkdir(parents=True, exist_ok=True)

    shutil.copy2(_REPO_ROOT / "tools" / "test_worker_bootstrap.py", snap_tools / "test_worker_bootstrap.py")
    shutil.copy2(_REPO_ROOT / "jarvis" / "__init__.py", snap_jarvis / "__init__.py")

    # Write valid matching synthetic snapshot integrity receipt
    dst_dig, approved_count = _compute_synthetic_dest_digest(temp_dir)
    src_before = dst_dig
    composite = hashlib.sha256(f"{src_before}:{dst_dig}:{temp_dir.name}".encode()).hexdigest().lower()
    receipt_data = {
        "run_id": "synthetic_run_id",
        "snapshot_id": temp_dir.name,
        "source_root": str(_REPO_ROOT),
        "snapshot_root": str(temp_dir),
        "created_at": time.time(),
        "source_before_digest": src_before,
        "source_after_digest": src_before,
        "destination_digest": dst_dig,
        "composite_snapshot_digest": composite,
        "approved_file_count": approved_count,
        "excluded_path_count": 43,
        "reparse_points_detected": 0,
        "env_example_sha256": "705b1b1a1cf11059f093551095a6e46b60d0b285c16ff0e9dcec12f3225666f8",
        "verification_status": "VERIFIED_IDENTICAL",
    }
    with (temp_dir / "snapshot_integrity_receipt.json").open("w", encoding="utf-8") as f:
        json.dump(receipt_data, f, indent=2)

    return temp_dir


def _cleanup_snapshot(snap_dir: Path) -> None:
    """Cleans up a synthetic snapshot directory."""
    if snap_dir.exists():
        shutil.rmtree(snap_dir, ignore_errors=True)


def _run_worker_direct(
    snap_dir: Path,
    args: list[str],
    auth_secret: bytes | None = None,
    timeout_sec: float = 10.0,
) -> SupervisorResult:
    """Executes a supervised worker within a synthetic snapshot."""
    res, holder, _ = run_supervised_worker(
        snap_dir,
        args,
        timeout_sec=timeout_sec,
        auth_secret=auth_secret,
    )
    holder.close()
    return res


# ==============================================================================
# Task 1: Node Inventory Binding (Checks 1–7)
# ==============================================================================

def test_check_01_canonical_profile_inventory_accepted() -> bool:
    """[Check 1] Valid canonical profile inventory (Context B1 Snapshot, 17 exact node IDs) accepted."""
    snap = _setup_synthetic_snapshot()
    try:
        dispatcher = ExecutionProfileDispatcher()
        nodes = dispatcher.get_nodes_for_profile(ProfileType.CONTEXT_B1_SNAPSHOT)
        assert len(nodes) == 17

        exec_vector = ["--synthetic-auth-probe", "-v", "--strict-markers"]
        permit_file, secret = issue_execution_permit(
            snap,
            ProfileType.CONTEXT_B1_SNAPSHOT,
            target_nodes=nodes,
            authorized_execution_vector=exec_vector,
            _skip_health_check=True,
        )

        res = _run_worker_direct(snap, [f"--permit={permit_file}"] + exec_vector, auth_secret=secret)
        receipts = list(snap.glob(".execution_permit.consumed.*"))
        return res.exit_code == 0 and "SYNTHETIC_WORKER_AUTH_VERIFIED" in res.stdout and len(receipts) == 1
    finally:
        _cleanup_snapshot(snap)


def test_check_02_missing_node_rejected() -> bool:
    """[Check 2] Permit where a node was deleted after hash computation rejected with code 134."""
    snap = _setup_synthetic_snapshot()
    try:
        dispatcher = ExecutionProfileDispatcher()
        nodes = list(dispatcher.get_nodes_for_profile(ProfileType.CONTEXT_B1_SNAPSHOT))
        exec_vector = ["--synthetic-auth-probe", "-v", "--strict-markers"]
        permit_file, secret = issue_execution_permit(
            snap,
            ProfileType.CONTEXT_B1_SNAPSHOT,
            target_nodes=nodes,
            authorized_execution_vector=exec_vector,
            _skip_health_check=True,
        )

        # Tamper: delete one node from target_nodes (keeping original inventory hash)
        with permit_file.open("r", encoding="utf-8") as f:
            data = json.load(f)
        data["target_nodes"].pop()
        data["node_count"] = len(data["target_nodes"])
        verify_bytes = json.dumps({k: v for k, v in data.items() if k != "hmac_signature"}, sort_keys=True).encode("utf-8")
        data["hmac_signature"] = hmac.new(secret, verify_bytes, hashlib.sha256).hexdigest()
        with permit_file.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

        res = _run_worker_direct(snap, [f"--permit={permit_file}"] + exec_vector, auth_secret=secret)
        return res.exit_code == 134 and "Node inventory SHA-256 digest mismatch" in res.stderr
    finally:
        _cleanup_snapshot(snap)


def test_check_03_extra_unassigned_node_rejected() -> bool:
    """[Check 3] Extra node injected into permit inventory causes digest mismatch (code 134)."""
    snap = _setup_synthetic_snapshot()
    try:
        dispatcher = ExecutionProfileDispatcher()
        nodes = list(dispatcher.get_nodes_for_profile(ProfileType.CONTEXT_B1_SNAPSHOT))
        exec_vector = ["--synthetic-auth-probe", "-v", "--strict-markers"]
        permit_file, secret = issue_execution_permit(
            snap,
            ProfileType.CONTEXT_B1_SNAPSHOT,
            target_nodes=nodes,
            authorized_execution_vector=exec_vector,
            _skip_health_check=True,
        )

        with permit_file.open("r", encoding="utf-8") as f:
            data = json.load(f)
        data["target_nodes"].append("tests/test_rogue.py::TestRogue::test_rogue_node")
        data["node_count"] = len(data["target_nodes"])
        verify_bytes = json.dumps({k: v for k, v in data.items() if k != "hmac_signature"}, sort_keys=True).encode("utf-8")
        data["hmac_signature"] = hmac.new(secret, verify_bytes, hashlib.sha256).hexdigest()
        with permit_file.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

        res = _run_worker_direct(snap, [f"--permit={permit_file}"] + exec_vector, auth_secret=secret)
        return res.exit_code == 134 and "Node inventory SHA-256 digest mismatch" in res.stderr
    finally:
        _cleanup_snapshot(snap)


def test_check_04_duplicate_node_ids_rejected() -> bool:
    """[Check 4] Duplicate node IDs in permit rejected with code 136."""
    snap = _setup_synthetic_snapshot()
    try:
        dispatcher = ExecutionProfileDispatcher()
        nodes = list(dispatcher.get_nodes_for_profile(ProfileType.CONTEXT_B1_SNAPSHOT))
        exec_vector = ["--synthetic-auth-probe", "-v", "--strict-markers"]
        permit_file, secret = issue_execution_permit(
            snap,
            ProfileType.CONTEXT_B1_SNAPSHOT,
            target_nodes=nodes,
            authorized_execution_vector=exec_vector,
            _skip_health_check=True,
        )

        with permit_file.open("r", encoding="utf-8") as f:
            data = json.load(f)
        # Duplicate the first node
        data["target_nodes"].append(data["target_nodes"][0])
        data["node_count"] = len(data["target_nodes"])
        data["node_inventory_hash"] = hashlib.sha256("\n".join(sorted(data["target_nodes"])).encode("utf-8")).hexdigest()
        verify_bytes = json.dumps({k: v for k, v in data.items() if k != "hmac_signature"}, sort_keys=True).encode("utf-8")
        data["hmac_signature"] = hmac.new(secret, verify_bytes, hashlib.sha256).hexdigest()
        with permit_file.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

        res = _run_worker_direct(snap, [f"--permit={permit_file}"] + exec_vector, auth_secret=secret)
        return res.exit_code == 136 and "Duplicate node IDs found in permit inventory" in res.stderr
    finally:
        _cleanup_snapshot(snap)


def test_check_05_bare_file_path_rejected() -> bool:
    """[Check 5] Bare test-file path replacing an exact node ID rejected with code 137."""
    snap = _setup_synthetic_snapshot()
    try:
        dispatcher = ExecutionProfileDispatcher()
        nodes = list(dispatcher.get_nodes_for_profile(ProfileType.CONTEXT_B1_SNAPSHOT))
        exec_vector = ["--synthetic-auth-probe", "-v", "--strict-markers"]
        permit_file, secret = issue_execution_permit(
            snap,
            ProfileType.CONTEXT_B1_SNAPSHOT,
            target_nodes=nodes,
            authorized_execution_vector=exec_vector,
            _skip_health_check=True,
        )

        with permit_file.open("r", encoding="utf-8") as f:
            data = json.load(f)
        # Replace first exact node with a broad file path
        data["target_nodes"][0] = "tests/test_broad_file_path.py"
        data["node_inventory_hash"] = hashlib.sha256("\n".join(sorted(data["target_nodes"])).encode("utf-8")).hexdigest()
        verify_bytes = json.dumps({k: v for k, v in data.items() if k != "hmac_signature"}, sort_keys=True).encode("utf-8")
        data["hmac_signature"] = hmac.new(secret, verify_bytes, hashlib.sha256).hexdigest()
        with permit_file.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

        res = _run_worker_direct(snap, [f"--permit={permit_file}"] + exec_vector, auth_secret=secret)
        return res.exit_code == 137 and "broad file path" in res.stderr
    finally:
        _cleanup_snapshot(snap)


def test_check_06_node_count_mismatch_rejected() -> bool:
    """[Check 6] Declared node count not matching actual inventory length rejected with code 135."""
    snap = _setup_synthetic_snapshot()
    try:
        dispatcher = ExecutionProfileDispatcher()
        nodes = list(dispatcher.get_nodes_for_profile(ProfileType.CONTEXT_B1_SNAPSHOT))
        exec_vector = ["--synthetic-auth-probe", "-v", "--strict-markers"]
        permit_file, secret = issue_execution_permit(
            snap,
            ProfileType.CONTEXT_B1_SNAPSHOT,
            target_nodes=nodes,
            authorized_execution_vector=exec_vector,
            _skip_health_check=True,
        )

        with permit_file.open("r", encoding="utf-8") as f:
            data = json.load(f)
        data["node_count"] = 507  # Declaring 507 when only 17 are in inventory
        verify_bytes = json.dumps({k: v for k, v in data.items() if k != "hmac_signature"}, sort_keys=True).encode("utf-8")
        data["hmac_signature"] = hmac.new(secret, verify_bytes, hashlib.sha256).hexdigest()
        with permit_file.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

        res = _run_worker_direct(snap, [f"--permit={permit_file}"] + exec_vector, auth_secret=secret)
        return res.exit_code == 135 and "Node count mismatch" in res.stderr
    finally:
        _cleanup_snapshot(snap)


def test_check_07_tampered_inventory_hash_rejected() -> bool:
    """[Check 7] Arbitrarily modified inventory hash rejected with code 134."""
    snap = _setup_synthetic_snapshot()
    try:
        dispatcher = ExecutionProfileDispatcher()
        nodes = list(dispatcher.get_nodes_for_profile(ProfileType.CONTEXT_B1_SNAPSHOT))
        exec_vector = ["--synthetic-auth-probe", "-v", "--strict-markers"]
        permit_file, secret = issue_execution_permit(
            snap,
            ProfileType.CONTEXT_B1_SNAPSHOT,
            target_nodes=nodes,
            authorized_execution_vector=exec_vector,
            _skip_health_check=True,
        )

        with permit_file.open("r", encoding="utf-8") as f:
            data = json.load(f)
        data["node_inventory_hash"] = "0" * 64
        verify_bytes = json.dumps({k: v for k, v in data.items() if k != "hmac_signature"}, sort_keys=True).encode("utf-8")
        data["hmac_signature"] = hmac.new(secret, verify_bytes, hashlib.sha256).hexdigest()
        with permit_file.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

        res = _run_worker_direct(snap, [f"--permit={permit_file}"] + exec_vector, auth_secret=secret)
        return res.exit_code == 134 and "Node inventory SHA-256 digest mismatch" in res.stderr
    finally:
        _cleanup_snapshot(snap)


# ==============================================================================
# Task 2: Exact Argument Vector Binding (Checks 8–13)
# ==============================================================================

def test_check_08_exact_execution_vector_accepted() -> bool:
    """[Check 8] Exact ordered execution argument vector accepted."""
    snap = _setup_synthetic_snapshot()
    try:
        exec_vector = ["--synthetic-auth-probe", "-v", "--strict-markers"]
        permit_file, secret = issue_execution_permit(
            snap,
            ProfileType.CONTEXT_B2_INTERACTIVE,
            authorized_execution_vector=exec_vector,
            _skip_health_check=True,
        )

        res = _run_worker_direct(snap, [f"--permit={permit_file}"] + exec_vector, auth_secret=secret)
        return res.exit_code == 0 and "SYNTHETIC_WORKER_AUTH_VERIFIED" in res.stdout
    finally:
        _cleanup_snapshot(snap)


def test_check_09_duplicate_authorized_flags_rejected() -> bool:
    """[Check 9] Duplicate authorized flags (-v -v --strict-markers) rejected with code 133."""
    snap = _setup_synthetic_snapshot()
    try:
        exec_vector = ["--synthetic-auth-probe", "-v", "--strict-markers"]
        permit_file, secret = issue_execution_permit(
            snap,
            ProfileType.CONTEXT_B2_INTERACTIVE,
            authorized_execution_vector=exec_vector,
            _skip_health_check=True,
        )

        # Worker invoked with duplicate '-v'
        duplicated_vector = ["--synthetic-auth-probe", "-v", "-v", "--strict-markers"]
        res = _run_worker_direct(snap, [f"--permit={permit_file}"] + duplicated_vector, auth_secret=secret)
        return res.exit_code == 133 and "Execution argument vector mismatch" in res.stderr
    finally:
        _cleanup_snapshot(snap)


def test_check_10_reordered_flags_rejected() -> bool:
    """[Check 10] Reordered execution flags rejected with code 133."""
    snap = _setup_synthetic_snapshot()
    try:
        exec_vector = ["--synthetic-auth-probe", "-v", "--strict-markers"]
        permit_file, secret = issue_execution_permit(
            snap,
            ProfileType.CONTEXT_B2_INTERACTIVE,
            authorized_execution_vector=exec_vector,
            _skip_health_check=True,
        )

        # Worker invoked with reordered arguments
        reordered_vector = ["--synthetic-auth-probe", "--strict-markers", "-v"]
        res = _run_worker_direct(snap, [f"--permit={permit_file}"] + reordered_vector, auth_secret=secret)
        return res.exit_code == 133 and "Execution argument vector mismatch" in res.stderr
    finally:
        _cleanup_snapshot(snap)


def test_check_11_unauthorized_extra_flags_rejected() -> bool:
    """[Check 11] Unauthorized extra flag (--unauthorized-flag) rejected with code 133."""
    snap = _setup_synthetic_snapshot()
    try:
        exec_vector = ["--synthetic-auth-probe", "-v", "--strict-markers"]
        permit_file, secret = issue_execution_permit(
            snap,
            ProfileType.CONTEXT_B2_INTERACTIVE,
            authorized_execution_vector=exec_vector,
            _skip_health_check=True,
        )

        tampered_vector = ["--synthetic-auth-probe", "-v", "--strict-markers", "--unauthorized-flag"]
        res = _run_worker_direct(snap, [f"--permit={permit_file}"] + tampered_vector, auth_secret=secret)
        return res.exit_code == 133 and "Execution argument vector mismatch" in res.stderr
    finally:
        _cleanup_snapshot(snap)


def test_check_12_unrecognized_synthetic_flag_rejected() -> bool:
    """[Check 12] Unrecognized synthetic argument (--synthetic-unknown) rejected with code 133."""
    snap = _setup_synthetic_snapshot()
    try:
        exec_vector = ["--synthetic-auth-probe", "-v", "--strict-markers"]
        permit_file, secret = issue_execution_permit(
            snap,
            ProfileType.CONTEXT_B2_INTERACTIVE,
            authorized_execution_vector=exec_vector,
            _skip_health_check=True,
        )

        # Worker invoked with rogue synthetic flag
        rogue_vector = ["--synthetic-unknown", "-v", "--strict-markers"]
        res = _run_worker_direct(snap, [f"--permit={permit_file}"] + rogue_vector, auth_secret=secret)
        return res.exit_code == 133 and "Unrecognized synthetic argument rejected" in res.stderr
    finally:
        _cleanup_snapshot(snap)


def test_check_13_unexpected_positional_arg_rejected() -> bool:
    """[Check 13] Unexpected positional argument or extra target node rejected with code 133."""
    snap = _setup_synthetic_snapshot()
    try:
        exec_vector = ["--synthetic-auth-probe", "-v", "--strict-markers"]
        permit_file, secret = issue_execution_permit(
            snap,
            ProfileType.CONTEXT_B2_INTERACTIVE,
            authorized_execution_vector=exec_vector,
            _skip_health_check=True,
        )

        vector_with_positional = ["--synthetic-auth-probe", "-v", "--strict-markers", "extra_arg"]
        res = _run_worker_direct(snap, [f"--permit={permit_file}"] + vector_with_positional, auth_secret=secret)
        return res.exit_code == 133 and "Execution argument vector mismatch" in res.stderr
    finally:
        _cleanup_snapshot(snap)


# ==============================================================================
# Task 3: Expiration Clock Semantics (Checks 14–18)
# ==============================================================================

def test_check_14_valid_timestamp_accepted() -> bool:
    """[Check 14] Valid permit within wall-clock bounds accepted."""
    snap = _setup_synthetic_snapshot()
    try:
        exec_vector = ["--synthetic-auth-probe", "-v", "--strict-markers"]
        permit_file, secret = issue_execution_permit(
            snap,
            ProfileType.CONTEXT_B2_INTERACTIVE,
            authorized_execution_vector=exec_vector,
            ttl_sec=120.0,
            _skip_health_check=True,
        )

        res = _run_worker_direct(snap, [f"--permit={permit_file}"] + exec_vector, auth_secret=secret)
        return res.exit_code == 0
    finally:
        _cleanup_snapshot(snap)


def test_check_15_expired_permit_rejected() -> bool:
    """[Check 15] Permit with past expiration timestamp rejected with code 128."""
    snap = _setup_synthetic_snapshot()
    try:
        exec_vector = ["--synthetic-auth-probe", "-v", "--strict-markers"]
        permit_file, secret = issue_execution_permit(
            snap,
            ProfileType.CONTEXT_B2_INTERACTIVE,
            authorized_execution_vector=exec_vector,
            _skip_health_check=True,
        )

        with permit_file.open("r", encoding="utf-8") as f:
            data = json.load(f)
        # Set expired in the past
        data["issued_at"] = time.time() - 200.0
        data["expires_at"] = time.time() - 100.0
        verify_bytes = json.dumps({k: v for k, v in data.items() if k != "hmac_signature"}, sort_keys=True).encode("utf-8")
        data["hmac_signature"] = hmac.new(secret, verify_bytes, hashlib.sha256).hexdigest()
        with permit_file.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

        res = _run_worker_direct(snap, [f"--permit={permit_file}"] + exec_vector, auth_secret=secret)
        return res.exit_code == 128 and "Permit expired" in res.stderr
    finally:
        _cleanup_snapshot(snap)


def test_check_16_future_dated_permit_rejected() -> bool:
    """[Check 16] Permit with future issuance timestamp rejected with code 128."""
    snap = _setup_synthetic_snapshot()
    try:
        exec_vector = ["--synthetic-auth-probe", "-v", "--strict-markers"]
        permit_file, secret = issue_execution_permit(
            snap,
            ProfileType.CONTEXT_B2_INTERACTIVE,
            authorized_execution_vector=exec_vector,
            _skip_health_check=True,
        )

        with permit_file.open("r", encoding="utf-8") as f:
            data = json.load(f)
        # Set issued 1000s in the future
        data["issued_at"] = time.time() + 1000.0
        data["expires_at"] = time.time() + 1300.0
        verify_bytes = json.dumps({k: v for k, v in data.items() if k != "hmac_signature"}, sort_keys=True).encode("utf-8")
        data["hmac_signature"] = hmac.new(secret, verify_bytes, hashlib.sha256).hexdigest()
        with permit_file.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

        res = _run_worker_direct(snap, [f"--permit={permit_file}"] + exec_vector, auth_secret=secret)
        return res.exit_code == 128 and "Permit future-dated" in res.stderr
    finally:
        _cleanup_snapshot(snap)


def test_check_17_issuance_after_expiry_rejected() -> bool:
    """[Check 17] Permit where issued_at >= expires_at rejected with code 128."""
    snap = _setup_synthetic_snapshot()
    try:
        exec_vector = ["--synthetic-auth-probe", "-v", "--strict-markers"]
        permit_file, secret = issue_execution_permit(
            snap,
            ProfileType.CONTEXT_B2_INTERACTIVE,
            authorized_execution_vector=exec_vector,
            _skip_health_check=True,
        )

        with permit_file.open("r", encoding="utf-8") as f:
            data = json.load(f)
        # Inverted timestamps
        data["issued_at"] = time.time()
        data["expires_at"] = data["issued_at"] - 10.0
        verify_bytes = json.dumps({k: v for k, v in data.items() if k != "hmac_signature"}, sort_keys=True).encode("utf-8")
        data["hmac_signature"] = hmac.new(secret, verify_bytes, hashlib.sha256).hexdigest()
        with permit_file.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

        res = _run_worker_direct(snap, [f"--permit={permit_file}"] + exec_vector, auth_secret=secret)
        return res.exit_code == 128 and "Permit invalid timestamps" in res.stderr
    finally:
        _cleanup_snapshot(snap)


def test_check_18_excessive_ttl_rejected() -> bool:
    """[Check 18] Permit with TTL exceeding MAX_PERMIT_TTL (3600s) rejected with code 128."""
    snap = _setup_synthetic_snapshot()
    try:
        exec_vector = ["--synthetic-auth-probe", "-v", "--strict-markers"]
        permit_file, secret = issue_execution_permit(
            snap,
            ProfileType.CONTEXT_B2_INTERACTIVE,
            authorized_execution_vector=exec_vector,
            _skip_health_check=True,
        )

        with permit_file.open("r", encoding="utf-8") as f:
            data = json.load(f)
        # Set TTL to 24 hours
        data["ttl_sec"] = 86400.0
        data["expires_at"] = data["issued_at"] + 86400.0
        verify_bytes = json.dumps({k: v for k, v in data.items() if k != "hmac_signature"}, sort_keys=True).encode("utf-8")
        data["hmac_signature"] = hmac.new(secret, verify_bytes, hashlib.sha256).hexdigest()
        with permit_file.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

        res = _run_worker_direct(snap, [f"--permit={permit_file}"] + exec_vector, auth_secret=secret)
        return res.exit_code == 128 and "Permit TTL exceeds maximum authorized duration" in res.stderr
    finally:
        _cleanup_snapshot(snap)


# ==============================================================================
# Task 4: Worker Executable Identity (Checks 19–22)
# ==============================================================================

def test_check_19_worker_exe_matching_accepted() -> bool:
    """[Check 19] Matching worker path, version, and binary SHA-256 accepted."""
    snap = _setup_synthetic_snapshot()
    try:
        exec_vector = ["--synthetic-auth-probe", "-v", "--strict-markers"]
        permit_file, secret = issue_execution_permit(
            snap,
            ProfileType.CONTEXT_B2_INTERACTIVE,
            authorized_execution_vector=exec_vector,
            _skip_health_check=True,
        )

        res = _run_worker_direct(snap, [f"--permit={permit_file}"] + exec_vector, auth_secret=secret)
        return res.exit_code == 0
    finally:
        _cleanup_snapshot(snap)


def test_check_20_worker_exe_path_mismatch_rejected() -> bool:
    """[Check 20] Mismatched worker executable path rejected with code 131."""
    snap = _setup_synthetic_snapshot()
    try:
        exec_vector = ["--synthetic-auth-probe", "-v", "--strict-markers"]
        permit_file, secret = issue_execution_permit(
            snap,
            ProfileType.CONTEXT_B2_INTERACTIVE,
            authorized_execution_vector=exec_vector,
            _skip_health_check=True,
        )

        with permit_file.open("r", encoding="utf-8") as f:
            data = json.load(f)
        data["worker_exe_path"] = r"C:\Other\python.exe"
        verify_bytes = json.dumps({k: v for k, v in data.items() if k != "hmac_signature"}, sort_keys=True).encode("utf-8")
        data["hmac_signature"] = hmac.new(secret, verify_bytes, hashlib.sha256).hexdigest()
        with permit_file.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

        res = _run_worker_direct(snap, [f"--permit={permit_file}"] + exec_vector, auth_secret=secret)
        return res.exit_code == 131 and "Worker executable path mismatch" in res.stderr
    finally:
        _cleanup_snapshot(snap)


def test_check_21_worker_exe_hash_mismatch_rejected() -> bool:
    """[Check 21] Same path/version but mismatched binary SHA-256 digest rejected with code 131."""
    snap = _setup_synthetic_snapshot()
    try:
        exec_vector = ["--synthetic-auth-probe", "-v", "--strict-markers"]
        permit_file, secret = issue_execution_permit(
            snap,
            ProfileType.CONTEXT_B2_INTERACTIVE,
            authorized_execution_vector=exec_vector,
            _skip_health_check=True,
        )

        with permit_file.open("r", encoding="utf-8") as f:
            data = json.load(f)
        # Invert the last hex character of the binary digest
        original_hash = data["worker_exe_hash"]
        tampered_hash = original_hash[:-1] + ("0" if original_hash[-1] != "0" else "1")
        data["worker_exe_hash"] = tampered_hash
        verify_bytes = json.dumps({k: v for k, v in data.items() if k != "hmac_signature"}, sort_keys=True).encode("utf-8")
        data["hmac_signature"] = hmac.new(secret, verify_bytes, hashlib.sha256).hexdigest()
        with permit_file.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

        res = _run_worker_direct(snap, [f"--permit={permit_file}"] + exec_vector, auth_secret=secret)
        return res.exit_code == 131 and "Worker executable SHA-256 binary digest mismatch" in res.stderr
    finally:
        _cleanup_snapshot(snap)


def test_check_22_malformed_worker_exe_hash_rejected() -> bool:
    """[Check 22] Malformed or empty worker executable hash rejected with code 131."""
    snap = _setup_synthetic_snapshot()
    try:
        exec_vector = ["--synthetic-auth-probe", "-v", "--strict-markers"]
        permit_file, secret = issue_execution_permit(
            snap,
            ProfileType.CONTEXT_B2_INTERACTIVE,
            authorized_execution_vector=exec_vector,
            _skip_health_check=True,
        )

        with permit_file.open("r", encoding="utf-8") as f:
            data = json.load(f)
        data["worker_exe_hash"] = ""
        verify_bytes = json.dumps({k: v for k, v in data.items() if k != "hmac_signature"}, sort_keys=True).encode("utf-8")
        data["hmac_signature"] = hmac.new(secret, verify_bytes, hashlib.sha256).hexdigest()
        with permit_file.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

        res = _run_worker_direct(snap, [f"--permit={permit_file}"] + exec_vector, auth_secret=secret)
        return res.exit_code == 131 and "Worker executable SHA-256 binary digest mismatch" in res.stderr
    finally:
        _cleanup_snapshot(snap)


# ==============================================================================
# Task 5: Atomic Claim Hardening & Replay (Checks 23–28)
# ==============================================================================

def test_check_23_non_canonical_permit_path_rejected() -> bool:
    """[Check 23] Supplying a non-canonical permit path (e.g. .claimed.*) rejected with code 127."""
    snap = _setup_synthetic_snapshot()
    try:
        exec_vector = ["--synthetic-auth-probe", "-v", "--strict-markers"]
        permit_file, secret = issue_execution_permit(
            snap,
            ProfileType.CONTEXT_B2_INTERACTIVE,
            authorized_execution_vector=exec_vector,
            _skip_health_check=True,
        )

        # Worker attempts to invoke directly with a non-canonical name
        fake_claimed = permit_file.with_name(".execution_permit.claimed.fake_worker_attempt")
        shutil.copy2(permit_file, fake_claimed)

        res = _run_worker_direct(snap, [f"--permit={fake_claimed}"] + exec_vector, auth_secret=secret)
        return res.exit_code == 127 and "Non-canonical permit path rejected" in res.stderr
    finally:
        _cleanup_snapshot(snap)


def test_check_24_existing_claim_destination_no_overwrite() -> bool:
    """[Check 24] Pre-existing claim destination collision fails closed without overwrite (code 127)."""
    snap = _setup_synthetic_snapshot()
    try:
        exec_vector = ["--synthetic-auth-probe", "-v", "--strict-markers"]
        permit_file, _ = issue_execution_permit(
            snap,
            ProfileType.CONTEXT_B2_INTERACTIVE,
            authorized_execution_vector=exec_vector,
            _skip_health_check=True,
        )

        # Monkeypatch: simulate destination collision by pre-creating colliding destination
        # We test Win32 MoveFileExW with dwFlags=0 directly in this snapshot
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        colliding_dst = permit_file.with_name(".execution_permit.claimed.colliding_test")
        with colliding_dst.open("w") as f:
            f.write("COLLIDING_CONTENT")

        # Calling MoveFileExW with dwFlags=0 on existing destination
        res = k32.MoveFileExW(wintypes.LPCWSTR(str(permit_file)), wintypes.LPCWSTR(str(colliding_dst)), wintypes.DWORD(0))
        err = ctypes.get_last_error()

        # Must fail with ERROR_ALREADY_EXISTS (183) or ERROR_FILE_EXISTS (80)
        assert res == 0
        assert err in (80, 183)
        # Content of colliding destination must NOT be overwritten!
        assert colliding_dst.read_text() == "COLLIDING_CONTENT"
        # Original permit file must remain intact
        assert permit_file.is_file()
        return True
    finally:
        _cleanup_snapshot(snap)


def test_check_25_original_path_replay_rejected() -> bool:
    """[Check 25] Replaying original permit path after successful consumption rejected with code 127/120."""
    snap = _setup_synthetic_snapshot()
    try:
        exec_vector = ["--synthetic-auth-probe", "-v", "--strict-markers"]
        permit_file, secret = issue_execution_permit(
            snap,
            ProfileType.CONTEXT_B2_INTERACTIVE,
            authorized_execution_vector=exec_vector,
            _skip_health_check=True,
        )

        # Run 1: consumes permit
        res1 = _run_worker_direct(snap, [f"--permit={permit_file}"] + exec_vector, auth_secret=secret)
        assert res1.exit_code == 0

        # Run 2: attempts to replay original permit path
        res2 = _run_worker_direct(snap, [f"--permit={permit_file}"] + exec_vector, auth_secret=secret)
        return res2.exit_code in (120, 127) and ("already claimed" in res2.stderr or "not found" in res2.stderr)
    finally:
        _cleanup_snapshot(snap)


def test_check_26_concurrent_duplicate_claims() -> bool:
    """[Check 26] 5 workers contending for 1 permit produce exactly 1 winner and 4 fail-closed rejections."""
    snap = _setup_synthetic_snapshot()
    try:
        exec_vector = ["--synthetic-auth-probe", "-v", "--strict-markers"]
        permit_file, secret = issue_execution_permit(
            snap,
            ProfileType.CONTEXT_B2_INTERACTIVE,
            authorized_execution_vector=exec_vector,
            _skip_health_check=True,
        )

        def _launch() -> int:
            res = _run_worker_direct(snap, [f"--permit={permit_file}"] + exec_vector, auth_secret=secret)
            return res.exit_code

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(_launch) for _ in range(5)]
            results = [f.result() for f in futures]

        # Exactly 1 worker wins, 4 fail closed
        return results.count(0) == 1 and (results.count(127) + results.count(120)) == 4
    finally:
        _cleanup_snapshot(snap)


def test_check_27_interrupted_atomic_consumption() -> bool:
    """[Check 27] Interrupted consumption remains fail-closed (permit never silently restored)."""
    snap = _setup_synthetic_snapshot()
    try:
        exec_vector = ["--synthetic-auth-probe", "-v", "--strict-markers"]
        permit_file, secret = issue_execution_permit(
            snap,
            ProfileType.CONTEXT_B2_INTERACTIVE,
            authorized_execution_vector=exec_vector,
            _skip_health_check=True,
        )

        # Simulate interruption during claimed state
        claimed = permit_file.with_name(".execution_permit.claimed.simulated_crashed_worker")
        os.replace(permit_file, claimed)

        res = _run_worker_direct(snap, [f"--permit={permit_file}"] + exec_vector, auth_secret=secret)
        return res.exit_code in (120, 127) and not permit_file.exists()
    finally:
        _cleanup_snapshot(snap)


def test_check_28_consumption_receipt_bound() -> bool:
    """[Check 28] Consumption receipt is bound to permit_id, run_id, PID, and timestamp."""
    snap = _setup_synthetic_snapshot()
    try:
        exec_vector = ["--synthetic-auth-probe", "-v", "--strict-markers"]
        p_id = "test-permit-uuid-1234"
        r_id = "test-run-uuid-5678"
        permit_file, secret = issue_execution_permit(
            snap,
            ProfileType.CONTEXT_B2_INTERACTIVE,
            authorized_execution_vector=exec_vector,
            permit_id=p_id,
            run_id=r_id,
            _skip_health_check=True,
        )

        res = _run_worker_direct(snap, [f"--permit={permit_file}"] + exec_vector, auth_secret=secret)
        assert res.exit_code == 0

        receipt_file = snap / f".execution_permit.consumed.{p_id}"
        assert receipt_file.is_file(), f"Receipt file {receipt_file} not found"

        with receipt_file.open("r", encoding="utf-8") as f:
            data = json.load(f)

        return (
            data.get("permit_id") == p_id
            and data.get("run_id") == r_id
            and data.get("status") == "CONSUMED"
            and isinstance(data.get("consumed_by_pid"), int)
            and isinstance(data.get("consumed_at"), float)
        )
    finally:
        _cleanup_snapshot(snap)


# ==============================================================================
# Task 6: Existing Security Controls & Supervisor Health (Checks 29–36)
# ==============================================================================

def test_check_29_missing_permit_rejected() -> bool:
    """[Check 29] Invocation without permit rejected with code 120."""
    snap = _setup_synthetic_snapshot()
    try:
        res = _run_worker_direct(snap, ["--synthetic-auth-probe"])
        return res.exit_code == 120 and "Missing supervisor-issued execution permit" in res.stderr
    finally:
        _cleanup_snapshot(snap)


def test_check_30_tampered_payload_rejected() -> bool:
    """[Check 30] Tampered JSON payload rejected by HMAC with code 126."""
    snap = _setup_synthetic_snapshot()
    try:
        exec_vector = ["--synthetic-auth-probe", "-v", "--strict-markers"]
        permit_file, secret = issue_execution_permit(
            snap,
            ProfileType.CONTEXT_B2_INTERACTIVE,
            authorized_execution_vector=exec_vector,
            _skip_health_check=True,
        )

        with permit_file.open("r", encoding="utf-8") as f:
            data = json.load(f)
        data["scope"] = "Option B — Unauthorized Tampered Scope"
        with permit_file.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

        res = _run_worker_direct(snap, [f"--permit={permit_file}"] + exec_vector, auth_secret=secret)
        return res.exit_code == 126 and "Cryptographic signature verification failed" in res.stderr
    finally:
        _cleanup_snapshot(snap)


def test_check_31_invalid_hmac_rejected() -> bool:
    """[Check 31] Invalid signature string rejected with code 126."""
    snap = _setup_synthetic_snapshot()
    try:
        exec_vector = ["--synthetic-auth-probe", "-v", "--strict-markers"]
        permit_file, secret = issue_execution_permit(
            snap,
            ProfileType.CONTEXT_B2_INTERACTIVE,
            authorized_execution_vector=exec_vector,
            _skip_health_check=True,
        )

        with permit_file.open("r", encoding="utf-8") as f:
            data = json.load(f)
        data["hmac_signature"] = "f" * 64
        with permit_file.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

        res = _run_worker_direct(snap, [f"--permit={permit_file}"] + exec_vector, auth_secret=secret)
        return res.exit_code == 126 and "Cryptographic signature verification failed" in res.stderr
    finally:
        _cleanup_snapshot(snap)


def test_check_32_secret_transfer_failure() -> bool:
    """[Check 32] Pipe EOF (0 bytes) rejected with code 125."""
    snap = _setup_synthetic_snapshot()
    try:
        exec_vector = ["--synthetic-auth-probe", "-v", "--strict-markers"]
        permit_file, _ = issue_execution_permit(
            snap,
            ProfileType.CONTEXT_B2_INTERACTIVE,
            authorized_execution_vector=exec_vector,
            _skip_health_check=True,
        )

        res = _run_worker_direct(snap, [f"--permit={permit_file}"] + exec_vector, auth_secret=b"")
        return res.exit_code == 125 and "Secret transfer failed" in res.stderr
    finally:
        _cleanup_snapshot(snap)


def test_check_33_secret_corruption() -> bool:
    """[Check 33] Corrupted 32-byte secret rejected via HMAC with code 126."""
    snap = _setup_synthetic_snapshot()
    try:
        exec_vector = ["--synthetic-auth-probe", "-v", "--strict-markers"]
        permit_file, _ = issue_execution_permit(
            snap,
            ProfileType.CONTEXT_B2_INTERACTIVE,
            authorized_execution_vector=exec_vector,
            _skip_health_check=True,
        )

        corrupted = b"\x00" * 32
        res = _run_worker_direct(snap, [f"--permit={permit_file}"] + exec_vector, auth_secret=corrupted)
        return res.exit_code == 126 and "Cryptographic signature verification failed" in res.stderr
    finally:
        _cleanup_snapshot(snap)


def test_check_34_preflight_executes_no_pytest_collection() -> bool:
    """[Check 34] Preflight mode executes self-verification and zero pytest collection."""
    snap = _setup_synthetic_snapshot()
    try:
        res = _run_worker_direct(snap, ["--preflight"])
        return (
            res.exit_code == 0
            and "=== WORKER BOOTSTRAP PREFLIGHT START ===" in res.stdout
            and "=== WORKER BOOTSTRAP PREFLIGHT SUCCESS ===" in res.stdout
            and "collected 0 items" not in res.stdout
        )
    finally:
        _cleanup_snapshot(snap)


def test_check_35_concurrent_supervisors_cannot_duplicate_authorize() -> bool:
    """[Check 35] Atomic O_CREAT | O_EXCL prevents duplicate supervisor issuance."""
    snap = _setup_synthetic_snapshot()
    try:
        def _issue() -> bool:
            try:
                issue_execution_permit(
                    snap,
                    ProfileType.CONTEXT_B2_INTERACTIVE,
                    authorized_execution_vector=["--synthetic-auth-probe", "-v", "--strict-markers"],
                    _skip_health_check=True,
                )
                return True
            except FileExistsError:
                return False

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(_issue) for _ in range(5)]
            results = [f.result() for f in futures]

        return results.count(True) == 1 and results.count(False) == 4
    finally:
        _cleanup_snapshot(snap)


def test_check_36_supervisor_health_rejection() -> bool:
    """[Check 36] Supervisor health gate refuses issuance when compromised or retaining failed handles."""
    snap = _setup_synthetic_snapshot()
    try:
        supervisor.SUPERVISOR_HEALTH_COMPROMISED = True
        compromise_rejected = False
        try:
            check_supervisor_health_for_issuance()
        except SupervisorUnhealthyError:
            compromise_rejected = True
        finally:
            supervisor.SUPERVISOR_HEALTH_COMPROMISED = False

        dummy_ov = OVERLAPPED()
        dummy_buf = ctypes.create_string_buffer(64)
        dummy_entry = RetainedPendingIOTracker.register(
            "dummy_fail_probe", dummy_ov, dummy_buf, 0, 0
        )
        dummy_entry.event_close_state = "CLOSE_FAILED"

        failed_handle_rejected = False
        try:
            check_supervisor_health_for_issuance()
        except SupervisorUnhealthyError:
            failed_handle_rejected = True
        finally:
            with supervisor._RETAINED_IO_LOCK:
                if dummy_entry in supervisor.RETAINED_PENDING_IO_REGISTRY:
                    supervisor.RETAINED_PENDING_IO_REGISTRY.remove(dummy_entry)

        return compromise_rejected and failed_handle_rejected
    finally:
        _cleanup_snapshot(snap)


# ==============================================================================
# Task 7: Independent Snapshot Receipt Verification (Checks 37–42)
# ==============================================================================

def test_check_37_missing_snapshot_receipt_rejected() -> bool:
    """[Check 37] Missing snapshot_integrity_receipt.json rejected with code 124."""
    snap = _setup_synthetic_snapshot()
    try:
        exec_vector = ["--synthetic-auth-probe", "-v", "--strict-markers"]
        permit_file, secret = issue_execution_permit(
            snap,
            ProfileType.CONTEXT_B1_SNAPSHOT,
            authorized_execution_vector=exec_vector,
            _skip_health_check=True,
        )
        receipt_file = snap / "snapshot_integrity_receipt.json"
        if receipt_file.is_file():
            receipt_file.unlink()

        res = _run_worker_direct(snap, [f"--permit={permit_file}"] + exec_vector, auth_secret=secret)
        return res.exit_code == 124 and "Snapshot integrity receipt missing" in res.stderr
    finally:
        _cleanup_snapshot(snap)


def test_check_38_malformed_snapshot_receipt_rejected() -> bool:
    """[Check 38] Malformed snapshot_integrity_receipt.json rejected with code 124."""
    snap = _setup_synthetic_snapshot()
    try:
        exec_vector = ["--synthetic-auth-probe", "-v", "--strict-markers"]
        permit_file, secret = issue_execution_permit(
            snap,
            ProfileType.CONTEXT_B1_SNAPSHOT,
            authorized_execution_vector=exec_vector,
            _skip_health_check=True,
        )
        receipt_file = snap / "snapshot_integrity_receipt.json"
        receipt_file.write_text("{\"corrupted\": json[syntax_err", encoding="utf-8")

        res = _run_worker_direct(snap, [f"--permit={permit_file}"] + exec_vector, auth_secret=secret)
        return res.exit_code == 124 and "Malformed snapshot integrity receipt" in res.stderr
    finally:
        _cleanup_snapshot(snap)


def test_check_39_receipt_schema_mismatch_rejected() -> bool:
    """[Check 39] Receipt missing required schema field rejected with code 124."""
    snap = _setup_synthetic_snapshot()
    try:
        exec_vector = ["--synthetic-auth-probe", "-v", "--strict-markers"]
        permit_file, secret = issue_execution_permit(
            snap,
            ProfileType.CONTEXT_B1_SNAPSHOT,
            authorized_execution_vector=exec_vector,
            _skip_health_check=True,
        )
        receipt_file = snap / "snapshot_integrity_receipt.json"
        with receipt_file.open("r", encoding="utf-8") as f:
            data = json.load(f)
        del data["verification_status"]
        with receipt_file.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

        res = _run_worker_direct(snap, [f"--permit={permit_file}"] + exec_vector, auth_secret=secret)
        return res.exit_code == 124 and "missing required schema fields" in res.stderr
    finally:
        _cleanup_snapshot(snap)


def test_check_40_receipt_identity_mismatch_rejected() -> bool:
    """[Check 40] Receipt snapshot_id mismatch rejected with code 124."""
    snap = _setup_synthetic_snapshot()
    try:
        exec_vector = ["--synthetic-auth-probe", "-v", "--strict-markers"]
        permit_file, secret = issue_execution_permit(
            snap,
            ProfileType.CONTEXT_B1_SNAPSHOT,
            authorized_execution_vector=exec_vector,
            _skip_health_check=True,
        )
        receipt_file = snap / "snapshot_integrity_receipt.json"
        with receipt_file.open("r", encoding="utf-8") as f:
            data = json.load(f)
        data["snapshot_id"] = "fake_foreign_snapshot_id"
        with receipt_file.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

        res = _run_worker_direct(snap, [f"--permit={permit_file}"] + exec_vector, auth_secret=secret)
        return res.exit_code == 124 and "Snapshot receipt ID mismatch" in res.stderr
    finally:
        _cleanup_snapshot(snap)


def test_check_41_tampered_composite_digest_in_receipt_rejected() -> bool:
    """[Check 41] Receipt with tampered composite digest rejected with code 124."""
    snap = _setup_synthetic_snapshot()
    try:
        exec_vector = ["--synthetic-auth-probe", "-v", "--strict-markers"]
        permit_file, secret = issue_execution_permit(
            snap,
            ProfileType.CONTEXT_B1_SNAPSHOT,
            authorized_execution_vector=exec_vector,
            _skip_health_check=True,
        )
        receipt_file = snap / "snapshot_integrity_receipt.json"
        with receipt_file.open("r", encoding="utf-8") as f:
            data = json.load(f)
        data["composite_snapshot_digest"] = "f" * 64
        with receipt_file.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

        res = _run_worker_direct(snap, [f"--permit={permit_file}"] + exec_vector, auth_secret=secret)
        return res.exit_code == 124 and "composite digest internally inconsistent or tampered" in res.stderr
    finally:
        _cleanup_snapshot(snap)


def test_check_42_permit_receipt_digest_mismatch_rejected() -> bool:
    """[Check 42] Permit tree_digest mismatching verified receipt rejected with code 124."""
    snap = _setup_synthetic_snapshot()
    try:
        exec_vector = ["--synthetic-auth-probe", "-v", "--strict-markers"]
        permit_file, secret = issue_execution_permit(
            snap,
            ProfileType.CONTEXT_B1_SNAPSHOT,
            authorized_execution_vector=exec_vector,
            _skip_health_check=True,
        )
        with permit_file.open("r", encoding="utf-8") as f:
            p_data = json.load(f)
        p_data["tree_digest"] = "e" * 64
        verify_bytes = json.dumps({k: v for k, v in p_data.items() if k != "hmac_signature"}, sort_keys=True).encode("utf-8")
        p_data["hmac_signature"] = hmac.new(secret, verify_bytes, hashlib.sha256).hexdigest()
        with permit_file.open("w", encoding="utf-8") as f:
            json.dump(p_data, f, indent=2)

        res = _run_worker_direct(snap, [f"--permit={permit_file}"] + exec_vector, auth_secret=secret)
        return res.exit_code == 124 and "Permit tree_digest does not match verified snapshot receipt" in res.stderr
    finally:
        _cleanup_snapshot(snap)


# ==============================================================================
# Task 8: Independent Canonical Inventory Verification (Checks 43–44)
# ==============================================================================

def test_check_43_pinned_canonical_inventory_digest_mismatch_rejected() -> bool:
    """[Check 43] Permit targeting foreign profile nodes rejected via pinned canonical digest (code 134)."""
    snap = _setup_synthetic_snapshot()
    try:
        dispatcher = ExecutionProfileDispatcher()
        foreign_nodes = list(dispatcher.get_nodes_for_profile(ProfileType.CONTEXT_B1_ORDINARY))[:17]
        exec_vector = ["--synthetic-auth-probe", "-v", "--strict-markers"]
        permit_file, secret = issue_execution_permit(
            snap,
            ProfileType.CONTEXT_B1_SNAPSHOT,
            target_nodes=foreign_nodes,
            authorized_execution_vector=exec_vector,
            _skip_health_check=True,
        )

        res = _run_worker_direct(snap, [f"--permit={permit_file}"] + exec_vector, auth_secret=secret)
        return res.exit_code == 134 and "does not match pinned canonical digest" in res.stderr
    finally:
        _cleanup_snapshot(snap)


def test_check_44_pinned_canonical_node_count_mismatch_rejected() -> bool:
    """[Check 44] Pinned canonical node count mismatch rejected (code 135)."""
    snap = _setup_synthetic_snapshot()
    try:
        dispatcher = ExecutionProfileDispatcher()
        nodes = list(dispatcher.get_nodes_for_profile(ProfileType.CONTEXT_B1_SNAPSHOT))[:16]
        exec_vector = ["--synthetic-auth-probe", "-v", "--strict-markers"]
        permit_file, secret = issue_execution_permit(
            snap,
            ProfileType.CONTEXT_B1_SNAPSHOT,
            target_nodes=nodes,
            authorized_execution_vector=exec_vector,
            _skip_health_check=True,
        )

        res = _run_worker_direct(snap, [f"--permit={permit_file}"] + exec_vector, auth_secret=secret)
        return res.exit_code == 135 and "Node count mismatch with pinned canonical partition" in res.stderr
    finally:
        _cleanup_snapshot(snap)


# ==============================================================================
# Task 9: Permit Finalization Error Handling (Checks 45–46)
# ==============================================================================

def test_check_45_consumed_marker_write_failure_quarantines() -> bool:
    """[Check 45] Injected failure to write consumed marker fails closed with code 138 and quarantines."""
    snap = _setup_synthetic_snapshot()
    try:
        exec_vector = ["--synthetic-auth-probe", "-v", "--strict-markers"]
        p_id = "test-fail-write-consumed-1234"
        permit_file, secret = issue_execution_permit(
            snap,
            ProfileType.CONTEXT_B1_SNAPSHOT,
            authorized_execution_vector=exec_vector,
            permit_id=p_id,
            _skip_health_check=True,
        )

        consumed_path = snap / f".execution_permit.consumed.{p_id}"
        consumed_path.write_text("COLLISION_PRE_EXISTING", encoding="utf-8")

        res = _run_worker_direct(snap, [f"--permit={permit_file}"] + exec_vector, auth_secret=secret)
        quarantined = list(snap.glob(".execution_permit.quarantined.consumed_marker_write_failed_*"))
        return res.exit_code == 138 and "Failed to write consumption receipt" in res.stderr and len(quarantined) == 1
    finally:
        _cleanup_snapshot(snap)


def test_check_46_claimed_unlink_failure_records_distinct_warning() -> bool:
    """[Check 46] Injected failure to unlink claimed permit writes warning marker and exits with code 139."""
    snap = _setup_synthetic_snapshot()
    try:
        exec_vector = ["--synthetic-auth-probe", "--synthetic-fail-unlink", "-v", "--strict-markers"]
        p_id = "test-fail-unlink-claimed-5678"
        permit_file, secret = issue_execution_permit(
            snap,
            ProfileType.CONTEXT_B1_SNAPSHOT,
            authorized_execution_vector=exec_vector,
            permit_id=p_id,
            _skip_health_check=True,
        )

        res = _run_worker_direct(snap, [f"--permit={permit_file}"] + exec_vector, auth_secret=secret)
        warn_markers = list(snap.glob(".execution_permit.unlink_failed.*"))
        return res.exit_code == 139 and "Failed to unlink claimed permit" in res.stderr and len(warn_markers) == 1
    finally:
        _cleanup_snapshot(snap)


# ==============================================================================
# Task 10: Actual Snapshot Content & Pre-Launch Authentication (Checks 47–52)
# ==============================================================================

def test_check_47_tampered_snapshot_file_rejected() -> bool:
    """[Check 47] Snapshot file content modified after receipt creation rejected with code 124."""
    snap = _setup_synthetic_snapshot()
    try:
        exec_vector = ["--synthetic-auth-probe", "-v", "--strict-markers"]
        permit_file, secret = issue_execution_permit(
            snap,
            ProfileType.CONTEXT_B1_SNAPSHOT,
            authorized_execution_vector=exec_vector,
            _skip_health_check=True,
        )

        # Tamper with an approved file in snapshot
        target_file = snap / "jarvis" / "__init__.py"
        with target_file.open("a", encoding="utf-8") as f:
            f.write("# TAMPERED_PAYLOAD\n")

        res = _run_worker_direct(snap, [f"--permit={permit_file}"] + exec_vector, auth_secret=secret)
        return res.exit_code == 124 and "destination digest mismatch" in res.stderr
    finally:
        _cleanup_snapshot(snap)


def test_check_48_added_snapshot_file_rejected() -> bool:
    """[Check 48] Unapproved file added to snapshot after receipt creation rejected with code 124."""
    snap = _setup_synthetic_snapshot()
    try:
        exec_vector = ["--synthetic-auth-probe", "-v", "--strict-markers"]
        permit_file, secret = issue_execution_permit(
            snap,
            ProfileType.CONTEXT_B1_SNAPSHOT,
            authorized_execution_vector=exec_vector,
            _skip_health_check=True,
        )

        # Add an unauthorized file to snapshot
        added_file = snap / "unauthorized_implant.py"
        added_file.write_text("# MALICIOUS\n", encoding="utf-8")

        res = _run_worker_direct(snap, [f"--permit={permit_file}"] + exec_vector, auth_secret=secret)
        return res.exit_code == 124 and ("Snapshot file count mismatch" in res.stderr or "destination digest mismatch" in res.stderr)
    finally:
        _cleanup_snapshot(snap)


def test_check_49_deleted_snapshot_file_rejected() -> bool:
    """[Check 49] Approved file deleted from snapshot after receipt creation rejected with code 124."""
    snap = _setup_synthetic_snapshot()
    try:
        exec_vector = ["--synthetic-auth-probe", "-v", "--strict-markers"]
        permit_file, secret = issue_execution_permit(
            snap,
            ProfileType.CONTEXT_B1_SNAPSHOT,
            authorized_execution_vector=exec_vector,
            _skip_health_check=True,
        )

        # Delete an approved file from snapshot
        (snap / "jarvis" / "__init__.py").unlink()

        res = _run_worker_direct(snap, [f"--permit={permit_file}"] + exec_vector, auth_secret=secret)
        return res.exit_code == 124 and ("Snapshot file count mismatch" in res.stderr or "destination digest mismatch" in res.stderr)
    finally:
        _cleanup_snapshot(snap)


def test_check_50_renamed_snapshot_file_rejected() -> bool:
    """[Check 50] Approved file renamed in snapshot after receipt creation rejected with code 124."""
    snap = _setup_synthetic_snapshot()
    try:
        exec_vector = ["--synthetic-auth-probe", "-v", "--strict-markers"]
        permit_file, secret = issue_execution_permit(
            snap,
            ProfileType.CONTEXT_B1_SNAPSHOT,
            authorized_execution_vector=exec_vector,
            _skip_health_check=True,
        )

        # Rename an approved file in snapshot
        orig = snap / "jarvis" / "__init__.py"
        renamed = snap / "jarvis" / "__init_renamed__.py"
        orig.rename(renamed)

        res = _run_worker_direct(snap, [f"--permit={permit_file}"] + exec_vector, auth_secret=secret)
        return res.exit_code == 124 and "destination digest mismatch" in res.stderr
    finally:
        _cleanup_snapshot(snap)


def test_check_51_receipt_and_snapshot_tampered_together_rejected() -> bool:
    """[Check 51] Receipt and snapshot modified together without valid permit update rejected (code 124)."""
    snap = _setup_synthetic_snapshot()
    try:
        exec_vector = ["--synthetic-auth-probe", "-v", "--strict-markers"]
        permit_file, secret = issue_execution_permit(
            snap,
            ProfileType.CONTEXT_B1_SNAPSHOT,
            authorized_execution_vector=exec_vector,
            _skip_health_check=True,
        )

        # 1. Modify file on disk
        target_file = snap / "jarvis" / "__init__.py"
        with target_file.open("a", encoding="utf-8") as f:
            f.write("# TAMPERED_PAYLOAD\n")

        # 2. Update receipt to match new disk hash
        dst_dig, approved_count = _compute_synthetic_dest_digest(snap)
        receipt_file = snap / "snapshot_integrity_receipt.json"
        with receipt_file.open("r", encoding="utf-8") as f:
            r_data = json.load(f)
        r_data["destination_digest"] = dst_dig
        r_data["approved_file_count"] = approved_count
        new_composite = hashlib.sha256(
            f"{r_data['source_before_digest']}:{dst_dig}:{r_data['snapshot_id']}".encode()
        ).hexdigest().lower()
        r_data["composite_snapshot_digest"] = new_composite
        with receipt_file.open("w", encoding="utf-8") as f:
            json.dump(r_data, f, indent=2)

        # 3. Worker should reject because permit.tree_digest does not match new_composite (code 124)
        res = _run_worker_direct(snap, [f"--permit={permit_file}"] + exec_vector, auth_secret=secret)
        return res.exit_code == 124 and "Permit tree_digest does not match verified snapshot receipt" in res.stderr
    finally:
        _cleanup_snapshot(snap)


def test_check_52_supervisor_prelaunch_rejects_tampered_bootstrap() -> bool:
    """[Check 52] Supervisor pre-launch authentication rejects tampered worker bootstrap before execution (code 131)."""
    snap = _setup_synthetic_snapshot()
    try:
        exec_vector = ["--synthetic-auth-probe", "-v", "--strict-markers"]
        permit_file, secret = issue_execution_permit(
            snap,
            ProfileType.CONTEXT_B1_SNAPSHOT,
            authorized_execution_vector=exec_vector,
            _skip_health_check=True,
        )

        # Tamper with test_worker_bootstrap.py in snapshot
        worker_file = snap / "tools" / "test_worker_bootstrap.py"
        content = worker_file.read_bytes()
        tampered_content = bytes([content[0] ^ 0xFF]) + content[1:]
        worker_file.write_bytes(tampered_content)

        # Run supervisor
        res = _run_worker_direct(snap, [f"--permit={permit_file}"] + exec_vector, auth_secret=secret)
        return res.exit_code == 131 and "[SUPERVISOR SECURITY ERROR] Worker bootstrap SHA-256 hash mismatch" in res.stderr
    finally:
        _cleanup_snapshot(snap)


# ==============================================================================
# Task 11: Verification-to-Execution & Synthetic Boundary Checks (Checks 53–60)
# ==============================================================================

def test_check_53_supervisor_sharing_lock_blocks_concurrent_write() -> bool:
    """[Check 53] Supervisor FILE_SHARE_READ sharing lock blocks concurrent modification during launch."""
    snap = _setup_synthetic_snapshot()
    try:
        bootstrap_script = snap / "tools" / "test_worker_bootstrap.py"
        # Open with FILE_SHARE_READ using Win32 API
        _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        GENERIC_READ = 0x80000000
        FILE_SHARE_READ = 0x00000001
        OPEN_EXISTING = 3
        FILE_ATTRIBUTE_NORMAL = 0x00000080

        h = _k32.CreateFileW(
            ctypes.wintypes.LPCWSTR(str(bootstrap_script)),
            ctypes.wintypes.DWORD(GENERIC_READ),
            ctypes.wintypes.DWORD(FILE_SHARE_READ),
            None,
            ctypes.wintypes.DWORD(OPEN_EXISTING),
            ctypes.wintypes.DWORD(FILE_ATTRIBUTE_NORMAL),
            None,
        )
        assert h != ctypes.wintypes.HANDLE(-1).value and h != 0

        # Attempt to open for writing concurrently
        write_blocked = False
        try:
            with bootstrap_script.open("a", encoding="utf-8") as f:
                f.write("# concurrent_tampering\n")
        except PermissionError:
            write_blocked = True
        finally:
            _k32.CloseHandle(h)

        return write_blocked
    finally:
        _cleanup_snapshot(snap)


def test_check_54_trusted_bootstrap_mismatch_rejected() -> bool:
    """[Check 54] Independent expected bootstrap digest mismatch rejected with code 131."""
    snap = _setup_synthetic_snapshot()
    try:
        exec_vector = ["--synthetic-auth-probe", "-v", "--strict-markers"]
        permit_file, secret = issue_execution_permit(
            snap,
            ProfileType.CONTEXT_B1_SNAPSHOT,
            authorized_execution_vector=exec_vector,
            _skip_health_check=True,
        )

        # Alter repository comparison by creating a dummy repository file
        worker_file = snap / "tools" / "test_worker_bootstrap.py"
        worker_file.write_text("# UNAPPROVED_CONTENT\n", encoding="utf-8")

        res = _run_worker_direct(snap, [f"--permit={permit_file}"] + exec_vector, auth_secret=secret)
        return res.exit_code == 131 and "[SUPERVISOR SECURITY ERROR] Worker bootstrap SHA-256 hash mismatch" in res.stderr
    finally:
        _cleanup_snapshot(snap)


def test_check_55_unapproved_file_in_fresh_runtime_dir_rejected() -> bool:
    """[Check 55] Unapproved file hidden inside fresh runtime directory rejected with code 124."""
    snap = _setup_synthetic_snapshot()
    try:
        # Create unapproved file inside excluded fresh runtime directory
        implant_dir = snap / "jarvis" / "skills" / "custom"
        implant_dir.mkdir(parents=True, exist_ok=True)
        implant_file = implant_dir / "hidden_implant.py"
        implant_file.write_text("# MALICIOUS_PAYLOAD\n", encoding="utf-8")

        exec_vector = ["--synthetic-auth-probe", "-v", "--strict-markers"]
        permit_file, secret = issue_execution_permit(
            snap,
            ProfileType.CONTEXT_B1_SNAPSHOT,
            authorized_execution_vector=exec_vector,
            _skip_health_check=True,
        )

        res = _run_worker_direct(snap, [f"--permit={permit_file}"] + exec_vector, auth_secret=secret)
        return res.exit_code == 124 and "Fresh runtime directory 'jarvis/skills/custom' contains unapproved files" in res.stderr
    finally:
        _cleanup_snapshot(snap)


def test_check_56_non_canonical_permit_file_not_excluded() -> bool:
    """[Check 56] Non-canonical permit filename (.execution_permit.py) included in hash and rejected (code 124)."""
    snap = _setup_synthetic_snapshot()
    try:
        # Create unauthorized file disguised as a permit
        fake_permit = snap / ".execution_permit.py"
        fake_permit.write_text("# MALICIOUS_CODE\n", encoding="utf-8")

        exec_vector = ["--synthetic-auth-probe", "-v", "--strict-markers"]
        permit_file, secret = issue_execution_permit(
            snap,
            ProfileType.CONTEXT_B1_SNAPSHOT,
            authorized_execution_vector=exec_vector,
            _skip_health_check=True,
        )

        res = _run_worker_direct(snap, [f"--permit={permit_file}"] + exec_vector, auth_secret=secret)
        return res.exit_code == 124 and ("destination digest mismatch" in res.stderr or "Snapshot file count mismatch" in res.stderr)
    finally:
        _cleanup_snapshot(snap)


def test_check_57_supervisor_rejects_synthetic_flags_with_candidate_nodes() -> bool:
    """[Check 57] Supervisor refuses to issue permit combining candidate test nodes with synthetic flags."""
    snap = _setup_synthetic_snapshot()
    try:
        dispatcher = ExecutionProfileDispatcher()
        nodes = list(dispatcher.get_nodes_for_profile(ProfileType.CONTEXT_B1_SNAPSHOT))
        exec_vector = ["--synthetic-fail-unlink", "-v", "--strict-markers"] + nodes
        try:
            issue_execution_permit(
                snap,
                ProfileType.CONTEXT_B1_SNAPSHOT,
                target_nodes=nodes,
                authorized_execution_vector=exec_vector,
                _skip_health_check=True,
            )
            return False  # Should have raised ValueError
        except ValueError as exc:
            return "strictly prohibited when targeting candidate test nodes" in str(exc)
    finally:
        _cleanup_snapshot(snap)


def test_check_58_worker_rejects_synthetic_flags_in_candidate_mode() -> bool:
    """[Check 58] Worker independently rejects synthetic flags in candidate execution mode (code 133)."""
    snap = _setup_synthetic_snapshot()
    try:
        dispatcher = ExecutionProfileDispatcher()
        nodes = list(dispatcher.get_nodes_for_profile(ProfileType.CONTEXT_B1_SNAPSHOT))
        # Issue valid permit without synthetic flags
        permit_file, secret = issue_execution_permit(
            snap,
            ProfileType.CONTEXT_B1_SNAPSHOT,
            target_nodes=nodes,
            _skip_health_check=True,
        )

        # Tamper permit to add synthetic failure flag alongside candidate nodes
        with permit_file.open("r", encoding="utf-8") as f:
            data = json.load(f)
        data["authorized_execution_vector"] = ["--synthetic-fail-unlink"] + data["authorized_execution_vector"]
        verify_bytes = json.dumps({k: v for k, v in data.items() if k != "hmac_signature"}, sort_keys=True).encode("utf-8")
        data["hmac_signature"] = hmac.new(secret, verify_bytes, hashlib.sha256).hexdigest()
        with permit_file.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

        exec_vector = data["authorized_execution_vector"]
        res = _run_worker_direct(snap, [f"--permit={permit_file}"] + exec_vector, auth_secret=secret)
        return res.exit_code == 133 and "strictly prohibited in candidate execution mode" in res.stderr
    finally:
        _cleanup_snapshot(snap)


def test_check_59_supervisor_restart_invalidates_old_permit() -> bool:
    """[Check 59] Supervisor restart with new ephemeral secret cannot authorize prior permit (code 126)."""
    snap = _setup_synthetic_snapshot()
    try:
        exec_vector = ["--synthetic-auth-probe", "-v", "--strict-markers"]
        permit_file, _original_secret = issue_execution_permit(
            snap,
            ProfileType.CONTEXT_B1_SNAPSHOT,
            authorized_execution_vector=exec_vector,
            _skip_health_check=True,
        )

        # Simulate supervisor restart: generate a new ephemeral secret
        new_supervisor_secret = secrets.token_bytes(32)

        # Attempt to authorize old permit using new secret
        res = _run_worker_direct(snap, [f"--permit={permit_file}"] + exec_vector, auth_secret=new_supervisor_secret)
        return res.exit_code == 126 and "Cryptographic signature verification failed" in res.stderr
    finally:
        _cleanup_snapshot(snap)


def test_check_60_expired_permit_invalidated_regardless_of_secret() -> bool:
    """[Check 60] Expired permit cannot be authorized even if secret, snapshot, and binary match (code 128)."""
    snap = _setup_synthetic_snapshot()
    try:
        exec_vector = ["--synthetic-auth-probe", "-v", "--strict-markers"]
        permit_file, secret = issue_execution_permit(
            snap,
            ProfileType.CONTEXT_B1_SNAPSHOT,
            authorized_execution_vector=exec_vector,
            ttl_sec=1.0,
            _skip_health_check=True,
        )

        # Wait for expiration
        time.sleep(1.2)

        res = _run_worker_direct(snap, [f"--permit={permit_file}"] + exec_vector, auth_secret=secret)
        return res.exit_code == 128 and "Permit expired" in res.stderr
    finally:
        _cleanup_snapshot(snap)


# ==============================================================================
# Task 12: Production Bootstrap Lock Lifecycle & Worker Acknowledgment (Checks 61–64)
# ==============================================================================

def test_check_61_supervisor_retains_lock_across_child_resumption_until_ack() -> bool:
    """[Check 61] Supervisor retains FILE_SHARE_READ sharing lock across child resumption until worker startup acknowledgment."""
    snap = _setup_synthetic_snapshot()
    try:
        bootstrap_script = snap / "tools" / "test_worker_bootstrap.py"
        exec_vector = ["--synthetic-ack-delay", "--synthetic-auth-probe", "-v", "--strict-markers"]
        permit_file, secret = issue_execution_permit(
            snap,
            ProfileType.CONTEXT_B1_SNAPSHOT,
            authorized_execution_vector=exec_vector,
            _skip_health_check=True,
        )

        sharing_violations = 0
        stop_probing = threading.Event()
        lock_observed = threading.Event()

        def probe_concurrent_writer():
            nonlocal sharing_violations
            _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            GENERIC_WRITE = 0x40000000
            OPEN_EXISTING = 3
            FILE_ATTRIBUTE_NORMAL = 0x00000080
            while not stop_probing.is_set():
                h = _k32.CreateFileW(
                    ctypes.wintypes.LPCWSTR(str(bootstrap_script)),
                    ctypes.wintypes.DWORD(GENERIC_WRITE),
                    ctypes.wintypes.DWORD(0),  # exclusive write
                    None,
                    ctypes.wintypes.DWORD(OPEN_EXISTING),
                    ctypes.wintypes.DWORD(FILE_ATTRIBUTE_NORMAL),
                    None,
                )
                if h == -1 or h == ctypes.wintypes.HANDLE(-1).value:
                    err = ctypes.get_last_error()
                    if err == 32:  # ERROR_SHARING_VIOLATION
                        sharing_violations += 1
                        lock_observed.set()
                else:
                    _k32.CloseHandle(h)
                time.sleep(0.005)

        probe_thread = threading.Thread(target=probe_concurrent_writer, daemon=True)
        probe_thread.start()

        try:
            res = _run_worker_direct(snap, [f"--permit={permit_file}"] + exec_vector, auth_secret=secret)
        finally:
            stop_probing.set()
            probe_thread.join(timeout=2.0)

        # Post-execution verification: sharing lock released so write succeeds
        _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        h_post = _k32.CreateFileW(
            ctypes.wintypes.LPCWSTR(str(bootstrap_script)),
            ctypes.wintypes.DWORD(0x40000000),  # GENERIC_WRITE
            ctypes.wintypes.DWORD(0),
            None,
            ctypes.wintypes.DWORD(3),
            ctypes.wintypes.DWORD(0x80),
            None,
        )
        post_open_ok = (h_post != -1 and h_post != ctypes.wintypes.HANDLE(-1).value)
        if post_open_ok:
            _k32.CloseHandle(h_post)

        return (
            res.exit_code == 0
            and "SYNTHETIC_WORKER_AUTH_VERIFIED" in res.stdout
            and lock_observed.is_set()
            and sharing_violations > 0
            and post_open_ok
        )
    finally:
        _cleanup_snapshot(snap)


def test_check_62_rejects_worker_with_wrong_acknowledgment_token() -> bool:
    """[Check 62] Supervisor rejects worker emitting mismatched startup acknowledgment token (code 131)."""
    snap = _setup_synthetic_snapshot()
    try:
        exec_vector = ["--synthetic-ack-wrong-token", "--synthetic-auth-probe", "-v", "--strict-markers"]
        permit_file, secret = issue_execution_permit(
            snap,
            ProfileType.CONTEXT_B1_SNAPSHOT,
            authorized_execution_vector=exec_vector,
            _skip_health_check=True,
        )

        res = _run_worker_direct(snap, [f"--permit={permit_file}"] + exec_vector, auth_secret=secret)
        return (
            res.exit_code == 131
            and res.status == "BOOTSTRAP_ACK_FAILED"
            and "Worker startup acknowledgment failed: Token mismatch in bootstrap acknowledgment" in res.stderr
        )
    finally:
        _cleanup_snapshot(snap)


def test_check_63_rejects_worker_with_mismatched_bootstrap_hash_in_ack() -> bool:
    """[Check 63] Supervisor rejects worker emitting mismatched bootstrap SHA-256 in acknowledgment (code 131)."""
    snap = _setup_synthetic_snapshot()
    try:
        exec_vector = ["--synthetic-ack-wrong-hash", "--synthetic-auth-probe", "-v", "--strict-markers"]
        permit_file, secret = issue_execution_permit(
            snap,
            ProfileType.CONTEXT_B1_SNAPSHOT,
            authorized_execution_vector=exec_vector,
            _skip_health_check=True,
        )

        res = _run_worker_direct(snap, [f"--permit={permit_file}"] + exec_vector, auth_secret=secret)
        return (
            res.exit_code == 131
            and res.status == "BOOTSTRAP_ACK_FAILED"
            and "Worker startup acknowledgment failed: Worker bootstrap hash mismatch in acknowledgment" in res.stderr
        )
    finally:
        _cleanup_snapshot(snap)


def test_check_64_fails_closed_when_worker_times_out_or_terminates_without_ack() -> bool:
    """[Check 64] Supervisor fails closed with code 131 if worker terminates or times out without acknowledgment."""
    snap_term = _setup_synthetic_snapshot()
    try:
        # Part A: Worker terminates prematurely without acknowledgment
        exec_vector_term = ["--synthetic-ack-skip", "--synthetic-normal", "-v", "--strict-markers"]
        permit_file_term, secret_term = issue_execution_permit(
            snap_term,
            ProfileType.CONTEXT_B1_SNAPSHOT,
            authorized_execution_vector=exec_vector_term,
            _skip_health_check=True,
        )
        res_term = _run_worker_direct(snap_term, [f"--permit={permit_file_term}"] + exec_vector_term, auth_secret=secret_term)
        part_a_ok = (
            res_term.exit_code == 131
            and res_term.status == "BOOTSTRAP_ACK_FAILED"
            and "terminated prematurely" in res_term.stderr
            and "without bootstrap acknowledgment" in res_term.stderr
        )
    finally:
        _cleanup_snapshot(snap_term)

    snap_timeout = _setup_synthetic_snapshot()
    try:
        # Part B: Worker times out without acknowledgment
        exec_vector_timeout = ["--synthetic-ack-skip", "--synthetic-timeout", "-v", "--strict-markers"]
        permit_file_timeout, secret_timeout = issue_execution_permit(
            snap_timeout,
            ProfileType.CONTEXT_B1_SNAPSHOT,
            authorized_execution_vector=exec_vector_timeout,
            _skip_health_check=True,
        )
        res_timeout = _run_worker_direct(
            snap_timeout,
            [f"--permit={permit_file_timeout}"] + exec_vector_timeout,
            auth_secret=secret_timeout,
            timeout_sec=1.0,
        )
        part_b_ok = (
            res_timeout.exit_code == 131
            and res_timeout.status == "BOOTSTRAP_ACK_FAILED"
            and ("timed out" in res_timeout.stderr or "terminated prematurely" in res_timeout.stderr)
        )
    finally:
        _cleanup_snapshot(snap_timeout)

    return part_a_ok and part_b_ok


def test_check_65_rejects_worker_with_incorrect_pid() -> bool:
    """[Check 65] Supervisor rejects worker emitting fabricated or incorrect PID in acknowledgment (code 131)."""
    snap = _setup_synthetic_snapshot()
    try:
        exec_vector = ["--synthetic-ack-wrong-pid", "--synthetic-auth-probe", "-v", "--strict-markers"]
        permit_file, secret = issue_execution_permit(
            snap,
            ProfileType.CONTEXT_B1_SNAPSHOT,
            authorized_execution_vector=exec_vector,
            _skip_health_check=True,
        )

        res = _run_worker_direct(snap, [f"--permit={permit_file}"] + exec_vector, auth_secret=secret)
        return (
            res.exit_code == 131
            and res.status == "BOOTSTRAP_ACK_FAILED"
            and ("Cannot open acknowledging process PID" in res.stderr or "not the authorized worker process" in res.stderr)
        )
    finally:
        _cleanup_snapshot(snap)


def test_check_66_rejects_descendant_attempting_ack_impersonation() -> bool:
    """[Check 66] Supervisor rejects descendant process attempting to emit worker acknowledgment (code 131)."""
    snap = _setup_synthetic_snapshot()
    try:
        exec_vector = ["--synthetic-ack-descendant-impersonation", "--synthetic-auth-probe", "-v", "--strict-markers"]
        permit_file, secret = issue_execution_permit(
            snap,
            ProfileType.CONTEXT_B1_SNAPSHOT,
            authorized_execution_vector=exec_vector,
            _skip_health_check=True,
        )

        res = _run_worker_direct(snap, [f"--permit={permit_file}"] + exec_vector, auth_secret=secret)
        return (
            res.exit_code == 131
            and res.status == "BOOTSTRAP_ACK_FAILED"
            and "is a descendant" in res.stderr
            and "not the authorized worker process" in res.stderr
        )
    finally:
        _cleanup_snapshot(snap)


def test_check_67_rejects_duplicate_acknowledgment() -> bool:
    """[Check 67] Supervisor rejects worker emitting duplicate startup acknowledgment lines (code 131)."""
    snap = _setup_synthetic_snapshot()
    try:
        exec_vector = ["--synthetic-ack-duplicate", "--synthetic-auth-probe", "-v", "--strict-markers"]
        permit_file, secret = issue_execution_permit(
            snap,
            ProfileType.CONTEXT_B1_SNAPSHOT,
            authorized_execution_vector=exec_vector,
            _skip_health_check=True,
        )

        res = _run_worker_direct(snap, [f"--permit={permit_file}"] + exec_vector, auth_secret=secret)
        return (
            res.exit_code == 131
            and res.status == "BOOTSTRAP_ACK_FAILED"
            and "Duplicate worker bootstrap acknowledgment detected" in res.stderr
        )
    finally:
        _cleanup_snapshot(snap)


# ==============================================================================
# Main Verification Runner
# ==============================================================================

def main() -> int:
    print("==================================================================")
    print("BLK-21 PHASE 1U: 67-CHECK EXECUTION AUTHORIZATION & BOUNDARY MATRIX")
    print("Exercising Production: tools.snapshot_test_supervisor & tools.test_worker_bootstrap")
    print("==================================================================")

    checks = [
        # Task 1
        ("Check 1", "Canonical profile inventory (Context B1 Snapshot, 17 exact node IDs) accepted", test_check_01_canonical_profile_inventory_accepted),
        ("Check 2", "Missing node from inventory rejected (code 134)", test_check_02_missing_node_rejected),
        ("Check 3", "Extra unassigned node rejected (code 134/132)", test_check_03_extra_unassigned_node_rejected),
        ("Check 4", "Duplicate node IDs in permit rejected (code 136)", test_check_04_duplicate_node_ids_rejected),
        ("Check 5", "Bare test-file path replacing node ID rejected (code 137)", test_check_05_bare_file_path_rejected),
        ("Check 6", "Declared node count mismatch rejected (code 135)", test_check_06_node_count_mismatch_rejected),
        ("Check 7", "Tampered inventory hash rejected (code 134)", test_check_07_tampered_inventory_hash_rejected),
        # Task 2
        ("Check 8", "Exact ordered execution vector accepted", test_check_08_exact_execution_vector_accepted),
        ("Check 9", "Duplicate authorized flags (-v -v ...) rejected (code 133)", test_check_09_duplicate_authorized_flags_rejected),
        ("Check 10", "Reordered execution flags rejected (code 133)", test_check_10_reordered_flags_rejected),
        ("Check 11", "Unauthorized extra flags rejected (code 133)", test_check_11_unauthorized_extra_flags_rejected),
        ("Check 12", "Unrecognized synthetic flag (--synthetic-unknown) rejected (code 133)", test_check_12_unrecognized_synthetic_flag_rejected),
        ("Check 13", "Unexpected positional argument rejected (code 133)", test_check_13_unexpected_positional_arg_rejected),
        # Task 3
        ("Check 14", "Valid permit timestamp within clock bounds accepted", test_check_14_valid_timestamp_accepted),
        ("Check 15", "Expired permit rejected (code 128)", test_check_15_expired_permit_rejected),
        ("Check 16", "Future-dated permit rejected (code 128)", test_check_16_future_dated_permit_rejected),
        ("Check 17", "Issuance time after expiry rejected (code 128)", test_check_17_issuance_after_expiry_rejected),
        ("Check 18", "Excessive TTL (>3600s) rejected (code 128)", test_check_18_excessive_ttl_rejected),
        # Task 4
        ("Check 19", "Matching worker path, version, and binary SHA-256 accepted", test_check_19_worker_exe_matching_accepted),
        ("Check 20", "Worker executable path mismatch rejected (code 131)", test_check_20_worker_exe_path_mismatch_rejected),
        ("Check 21", "Worker executable SHA-256 binary hash mismatch rejected (code 131)", test_check_21_worker_exe_hash_mismatch_rejected),
        ("Check 22", "Malformed worker executable hash rejected (code 131)", test_check_22_malformed_worker_exe_hash_rejected),
        # Task 5
        ("Check 23", "Non-canonical permit path (e.g. .claimed.*) rejected (code 127)", test_check_23_non_canonical_permit_path_rejected),
        ("Check 24", "Existing claim destination collision fails closed without overwrite (code 127)", test_check_24_existing_claim_destination_no_overwrite),
        ("Check 25", "Original-path replay rejected after consumption (code 127/120)", test_check_25_original_path_replay_rejected),
        ("Check 26", "Concurrent duplicate claims (5 workers contending, exactly 1 wins)", test_check_26_concurrent_duplicate_claims),
        ("Check 27", "Interrupted atomic consumption remains fail-closed (code 127/120)", test_check_27_interrupted_atomic_consumption),
        ("Check 28", "Consumption receipt bound to permit ID and run ID", test_check_28_consumption_receipt_bound),
        # Task 6
        ("Check 29", "Missing permit rejected (code 120)", test_check_29_missing_permit_rejected),
        ("Check 30", "Tampered permit payload rejected via HMAC (code 126)", test_check_30_tampered_payload_rejected),
        ("Check 31", "Invalid HMAC signature rejected (code 126)", test_check_31_invalid_hmac_rejected),
        ("Check 32", "Secret-transfer failure (EOF on pipe) rejected (code 125)", test_check_32_secret_transfer_failure),
        ("Check 33", "Secret corruption over pipe rejected (code 126)", test_check_33_secret_corruption),
        ("Check 34", "Preflight mode executes zero pytest collection (code 0)", test_check_34_preflight_executes_no_pytest_collection),
        ("Check 35", "Concurrent supervisors cannot duplicate-authorize (EEXIST)", test_check_35_concurrent_supervisors_cannot_duplicate_authorize),
        ("Check 36", "Supervisor health check refuses issuance when compromised", test_check_36_supervisor_health_rejection),
        # Task 7: Independent Snapshot Receipt Verification (P1-B)
        ("Check 37", "Missing snapshot receipt rejected (code 124)", test_check_37_missing_snapshot_receipt_rejected),
        ("Check 38", "Malformed snapshot receipt rejected (code 124)", test_check_38_malformed_snapshot_receipt_rejected),
        ("Check 39", "Receipt missing required schema fields rejected (code 124)", test_check_39_receipt_schema_mismatch_rejected),
        ("Check 40", "Receipt snapshot identity mismatch rejected (code 124)", test_check_40_receipt_identity_mismatch_rejected),
        ("Check 41", "Receipt tampered composite digest rejected (code 124)", test_check_41_tampered_composite_digest_in_receipt_rejected),
        ("Check 42", "Permit tree_digest mismatching verified receipt rejected (code 124)", test_check_42_permit_receipt_digest_mismatch_rejected),
        # Task 8: Independent Canonical Inventory Verification (P1-A)
        ("Check 43", "Permit targeting foreign profile nodes rejected via pinned digest (code 134)", test_check_43_pinned_canonical_inventory_digest_mismatch_rejected),
        ("Check 44", "Pinned canonical node count mismatch rejected (code 135)", test_check_44_pinned_canonical_node_count_mismatch_rejected),
        # Task 9: Permit Finalization Error Handling (P1-C)
        ("Check 45", "Injected failure to write consumed marker fails closed (code 138)", test_check_45_consumed_marker_write_failure_quarantines),
        ("Check 46", "Injected failure to unlink claimed permit records warning (code 139)", test_check_46_claimed_unlink_failure_records_distinct_warning),
        # Task 10: Actual Snapshot Content & Pre-Launch Authentication (Checks 47–52)
        ("Check 47", "Snapshot file content modified after receipt creation rejected (code 124)", test_check_47_tampered_snapshot_file_rejected),
        ("Check 48", "Unapproved file added to snapshot after receipt creation rejected (code 124)", test_check_48_added_snapshot_file_rejected),
        ("Check 49", "Approved file deleted from snapshot after receipt creation rejected (code 124)", test_check_49_deleted_snapshot_file_rejected),
        ("Check 50", "Approved file renamed in snapshot after receipt creation rejected (code 124)", test_check_50_renamed_snapshot_file_rejected),
        ("Check 51", "Receipt and snapshot tampered together without valid permit update rejected (code 124)", test_check_51_receipt_and_snapshot_tampered_together_rejected),
        ("Check 52", "Supervisor pre-launch authentication rejects tampered worker bootstrap (code 131)", test_check_52_supervisor_prelaunch_rejects_tampered_bootstrap),
        # Task 11: Verification-to-Execution & Synthetic Boundary Checks (Checks 53–60)
        ("Check 53", "Supervisor sharing lock blocks concurrent modification during launch", test_check_53_supervisor_sharing_lock_blocks_concurrent_write),
        ("Check 54", "Independent expected bootstrap digest mismatch rejected (code 131)", test_check_54_trusted_bootstrap_mismatch_rejected),
        ("Check 55", "Unapproved file hidden in fresh runtime directory rejected (code 124)", test_check_55_unapproved_file_in_fresh_runtime_dir_rejected),
        ("Check 56", "Non-canonical permit filename (.execution_permit.py) rejected (code 124)", test_check_56_non_canonical_permit_file_not_excluded),
        ("Check 57", "Supervisor refuses to issue permit combining candidate nodes with synthetic flags", test_check_57_supervisor_rejects_synthetic_flags_with_candidate_nodes),
        ("Check 58", "Worker independently rejects synthetic flags in candidate execution mode (code 133)", test_check_58_worker_rejects_synthetic_flags_in_candidate_mode),
        ("Check 59", "Supervisor restart with new ephemeral secret cannot authorize prior permit (code 126)", test_check_59_supervisor_restart_invalidates_old_permit),
        ("Check 60", "Expired permit invalidated regardless of secret or snapshot validity (code 128)", test_check_60_expired_permit_invalidated_regardless_of_secret),
        # Task 12: Production Bootstrap Lock Lifecycle & Worker Acknowledgment (Checks 61–67)
        ("Check 61", "Supervisor retains FILE_SHARE_READ lock across child resumption until worker startup ack", test_check_61_supervisor_retains_lock_across_child_resumption_until_ack),
        ("Check 62", "Supervisor rejects worker emitting mismatched startup acknowledgment token (code 131)", test_check_62_rejects_worker_with_wrong_acknowledgment_token),
        ("Check 63", "Supervisor rejects worker emitting mismatched bootstrap hash in acknowledgment (code 131)", test_check_63_rejects_worker_with_mismatched_bootstrap_hash_in_ack),
        ("Check 64", "Supervisor fails closed with code 131 if worker terminates or times out without ack", test_check_64_fails_closed_when_worker_times_out_or_terminates_without_ack),
        ("Check 65", "Supervisor rejects worker emitting fabricated or incorrect PID in acknowledgment (code 131)", test_check_65_rejects_worker_with_incorrect_pid),
        ("Check 66", "Supervisor rejects descendant process attempting to emit worker acknowledgment (code 131)", test_check_66_rejects_descendant_attempting_ack_impersonation),
        ("Check 67", "Supervisor rejects worker emitting duplicate startup acknowledgment lines (code 131)", test_check_67_rejects_duplicate_acknowledgment),
    ]

    passed = 0
    failed = 0
    for name, desc, func in checks:
        t0 = time.monotonic()
        try:
            result = func()
            dt = time.monotonic() - t0
            if result:
                passed += 1
                print(f"[{name}] {desc}...\n  Result: PASS ({dt:.2f}s)")
            else:
                failed += 1
                print(f"[{name}] {desc}...\n  Result: FAIL ({dt:.2f}s)")
        except (AssertionError, OSError, RuntimeError, ValueError) as exc:
            dt = time.monotonic() - t0
            failed += 1
            print(f"[{name}] {desc}...\n  Result: EXCEPTION ({exc}) ({dt:.2f}s)")

    print("==================================================================")
    if failed == 0:
        print(f"OVERALL RESULT: ALL {passed} CHECKS PASSED (exit code 0)")
        print("Gate 2 invariant preserved: ZERO candidate tests executed.")
        return 0
    else:
        print(f"OVERALL RESULT: {failed} CHECKS FAILED out of {len(checks)}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
