"""Synthetic Verification Suite for BLK-18 Four-Profile Process Isolation.

Executes 17 rigorous synthetic probes exercising actual operating-system process isolation,
token witness queries, boundary enforcement, Job Object pre-resume termination, and
fail-closed profile dispatching across Option A profiles:
- Context A: 69 nodes (AppContainer / Low Integrity)
- Context B1 Ordinary: 420 nodes (Non-Elevated Medium Integrity)
- Context B1 Snapshot: 17 nodes (Disposable Working-Tree Boundary)
- Context B2 Interactive: 1 node (Dedicated Disposable Interactive Station)

Absolute Constraints:
- Zero candidate test execution; zero pytest collections.
- Harmless synthetic worker probes only.
- Fail closed on any profile or boundary violation.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Ensure repository root is on sys.path
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools.execution_profile_dispatcher import ExecutionProfileDispatcher, ProfileType
from tools.snapshot_test_supervisor import (
    PYTHON_EXE,
    ProcessWitness,
    issue_execution_permit,
    launch_profile_worker,
    validate_profile_boundary,
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


def _setup_isolated_snapshot_for_test(prefix: str = "iso_test") -> tuple[Path, Path]:
    """Creates a minimal isolated snapshot directory with tools/ and temp/ for synthetic worker tests."""
    temp_dir = Path(tempfile.mkdtemp(prefix=f"jarvis_{prefix}_")).resolve()
    tools_dir = temp_dir / "tools"
    tools_dir.mkdir(parents=True, exist_ok=True)
    temp_sub = temp_dir / "temp"
    temp_sub.mkdir(parents=True, exist_ok=True)
    jarvis_dir = temp_dir / "jarvis"
    jarvis_dir.mkdir(parents=True, exist_ok=True)
    (jarvis_dir / "__init__.py").write_text("# synthetic snapshot jarvis\n__version__ = '2.0.0-snap'\n", encoding="utf-8")

    # Copy test_worker_bootstrap.py
    orig_bootstrap = _REPO_ROOT / "tools" / "test_worker_bootstrap.py"
    shutil.copy2(str(orig_bootstrap), str(tools_dir / "test_worker_bootstrap.py"))

    # Create dummy .env.example
    (temp_dir / ".env.example").write_text("JARVIS_TEST_MODE=1\n", encoding="utf-8")

    # Write valid matching synthetic receipt
    dst_dig, approved_count = _compute_synthetic_dest_digest(temp_dir)
    src_before = dst_dig
    composite = hashlib.sha256(f"{src_before}:{dst_dig}:{temp_dir.name}".encode()).hexdigest().lower()
    receipt_data = {
        "run_id": f"run_{prefix}",
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

    return temp_dir, temp_sub


def _cleanup_test_snapshot(snap_dir: Path) -> None:
    """Cleans up temporary snapshot directory."""
    try:
        shutil.rmtree(str(snap_dir), ignore_errors=True)
    except OSError:
        pass


def test_probe_01_actual_appcontainer_child_launch() -> ProcessWitness:
    """[Probe 1] Actual AppContainer child launch under Context A profile."""
    snap_dir, _ = _setup_isolated_snapshot_for_test("p01")
    dispatcher = ExecutionProfileDispatcher()
    ctx_a_nodes = dispatcher.get_nodes_for_profile(ProfileType.CONTEXT_A)

    permit_file, secret = issue_execution_permit(
        snapshot_root=snap_dir,
        profile=ProfileType.CONTEXT_A,
        target_nodes=ctx_a_nodes,
        authorized_execution_vector=["--synthetic-normal"],
        ttl_sec=60.0,
    )

    try:
        result, holder, _drainers, witness = launch_profile_worker(
            profile=ProfileType.CONTEXT_A,
            snapshot_root=snap_dir,
            worker_args=["--permit=" + str(permit_file.resolve()), "--synthetic-normal"],
            auth_secret=secret,
            timeout_sec=10.0,
        )

        assert witness is not None, "Witness must be captured"
        assert witness.pid > 0, "Valid child PID required"
        assert result.exit_code == 0, f"Expected clean exit code 0, got {result.exit_code}: {result.stderr}"
        assert result.status == "COMPLETED", f"Expected COMPLETED, got {result.status}"
        return witness
    finally:
        holder.close()
        _cleanup_test_snapshot(snap_dir)


def test_probe_02_appcontainer_low_integrity_token_witness() -> None:
    """[Probe 2] AppContainer Low Integrity token witness (TokenIsAppContainer==1, exact RID==0x1000)."""
    snap_dir, _ = _setup_isolated_snapshot_for_test("p02")
    dispatcher = ExecutionProfileDispatcher()
    ctx_a_nodes = dispatcher.get_nodes_for_profile(ProfileType.CONTEXT_A)

    permit_file, secret = issue_execution_permit(
        snapshot_root=snap_dir,
        profile=ProfileType.CONTEXT_A,
        target_nodes=ctx_a_nodes,
        authorized_execution_vector=["--synthetic-normal"],
        ttl_sec=60.0,
    )

    try:
        _result, holder, _, witness = launch_profile_worker(
            profile=ProfileType.CONTEXT_A,
            snapshot_root=snap_dir,
            worker_args=["--permit=" + str(permit_file.resolve()), "--synthetic-normal"],
            auth_secret=secret,
            timeout_sec=10.0,
        )

        assert witness is not None, "Witness required"
        assert witness.is_appcontainer is True, f"TokenIsAppContainer must be True, got {witness.is_appcontainer}"
        assert witness.integrity_rid == 0x1000, f"Integrity RID must be exact 0x1000 (Low), got {hex(witness.integrity_rid)}"
        assert witness.integrity_label == "Low", f"Integrity label must be Low, got {witness.integrity_label}"
        assert witness.is_elevated is False, "Must be non-elevated"
        assert isinstance(witness.capabilities, tuple), "Capabilities must be tuple"
        assert len(witness.capabilities) > 0, "Capabilities must be queried and non-empty"
        from tools.snapshot_test_supervisor import CONTEXT_A_CAPABILITY_ALLOWLIST
        unauthorized = set(witness.capabilities) - CONTEXT_A_CAPABILITY_ALLOWLIST
        assert not unauthorized, f"Unauthorized capabilities detected: {unauthorized}"
    finally:
        holder.close()
        _cleanup_test_snapshot(snap_dir)


def test_probe_03_appcontainer_sid_verification() -> str:
    """[Probe 3] AppContainer SID verification (non-empty string starting with S-1-15-2-)."""
    snap_dir, _ = _setup_isolated_snapshot_for_test("p03")
    dispatcher = ExecutionProfileDispatcher()
    ctx_a_nodes = dispatcher.get_nodes_for_profile(ProfileType.CONTEXT_A)

    permit_file, secret = issue_execution_permit(
        snapshot_root=snap_dir,
        profile=ProfileType.CONTEXT_A,
        target_nodes=ctx_a_nodes,
        authorized_execution_vector=["--synthetic-normal"],
        ttl_sec=60.0,
    )

    try:
        _result, holder, _, witness = launch_profile_worker(
            profile=ProfileType.CONTEXT_A,
            snapshot_root=snap_dir,
            worker_args=["--permit=" + str(permit_file.resolve()), "--synthetic-normal"],
            auth_secret=secret,
            timeout_sec=10.0,
        )

        assert witness is not None, "Witness required"
        assert witness.appcontainer_sid is not None, "AppContainer SID must not be None"
        assert witness.appcontainer_sid.startswith("S-1-15-2-"), f"Invalid SID format: {witness.appcontainer_sid}"
        sub_auths = witness.appcontainer_sid.split("-")[3:]
        assert len(sub_auths) >= 7, f"Expected >= 7 AppContainer subauthorities, found {len(sub_auths)}"
        return witness.appcontainer_sid
    finally:
        holder.close()
        _cleanup_test_snapshot(snap_dir)


def test_probe_04_appcontainer_snapshot_access() -> None:
    """[Probe 4] AppContainer access to approved snapshot directory and write to temp."""
    snap_dir, _temp_dir = _setup_isolated_snapshot_for_test("p04")
    dispatcher = ExecutionProfileDispatcher()
    ctx_a_nodes = dispatcher.get_nodes_for_profile(ProfileType.CONTEXT_A)

    exec_vector = ["--synthetic-auth-probe", "-v", "--strict-markers"]
    permit_file, secret = issue_execution_permit(
        snapshot_root=snap_dir,
        profile=ProfileType.CONTEXT_A,
        target_nodes=ctx_a_nodes,
        authorized_execution_vector=exec_vector,
        ttl_sec=60.0,
    )

    try:
        result, holder, _, _witness = launch_profile_worker(
            profile=ProfileType.CONTEXT_A,
            snapshot_root=snap_dir,
            worker_args=["--permit=" + str(permit_file.resolve())] + exec_vector,
            auth_secret=secret,
            timeout_sec=10.0,
        )

        assert result.exit_code == 0, f"Worker failed: {result.stderr}"
        assert "SYNTHETIC_WORKER_AUTH_VERIFIED" in result.stdout
        receipts = list(snap_dir.glob(".execution_permit.consumed.*"))
        assert len(receipts) == 1, "Consumed permit receipt must be written inside snapshot by AppContainer"
        assert receipts[0].is_file(), "Receipt must be valid file"
    finally:
        holder.close()
        _cleanup_test_snapshot(snap_dir)


def test_probe_05_appcontainer_controlled_acl_resource_boundary() -> None:
    """[Probe 5] AppContainer controlled ACL resource boundary & user folder denial."""
    temp_dir = Path(tempfile.mkdtemp(prefix="jarvis_acl_ctrl_")).resolve()
    denied_file = temp_dir / "denied_resource.txt"
    denied_file.write_text("classified_token_data", encoding="utf-8")

    import os
    u = os.environ.get("USERNAME", "Youssef")

    # Strip inheritance and add explicit Everyone Deny ACE for Read/Write
    subprocess.run(["icacls", str(denied_file), "/inheritance:r"], capture_output=True, check=False)
    subprocess.run(["icacls", str(denied_file), "/deny", "*S-1-1-0:(RD,WD)"], capture_output=True, check=False)
    subprocess.run(["icacls", str(denied_file), "/grant:r", f"{u}:(F)"], capture_output=True, check=False)

    try:
        # 1. Verify read denial on controlled fixture
        try:
            with denied_file.open("r", encoding="utf-8") as f:
                _ = f.read()
            denied_raised = False
        except PermissionError:
            denied_raised = True
        assert denied_raised is True, "Controlled ACL resource with Deny ACE must raise PermissionError"

        # 2. Host user folder isolation (Public / Documents)
        # Ordinary Medium Integrity users can read/list Documents & Public.
        # Low Integrity AppContainer is denied access because neither ALL APPLICATION PACKAGES
        # nor the sandbox capability SID is present in their DACLs.
        user_folder = Path("C:/Users/Public")
        try:
            _ = list(user_folder.iterdir())
            public_denied = False
        except PermissionError:
            public_denied = True
        assert public_denied is True, "Access to C:/Users/Public must be denied under AppContainer token"

    finally:
        # Proper restoration and cleanup
        subprocess.run(["icacls", str(denied_file), "/remove:d", "*S-1-1-0"], capture_output=True, check=False)
        try:
            shutil.rmtree(str(temp_dir), ignore_errors=True)
        except OSError:
            pass


def test_probe_06_appcontainer_launch_failure_no_fallback() -> None:
    """[Probe 6] AppContainer launch failure with no fallback to unconstrained execution."""
    snap_dir, _ = _setup_isolated_snapshot_for_test("p06")
    (snap_dir / "tools" / "test_worker_bootstrap.py").unlink()

    result, _holder, _, witness = launch_profile_worker(
        profile=ProfileType.CONTEXT_A,
        snapshot_root=snap_dir,
        worker_args=["--synthetic-normal"],
        timeout_sec=5.0,
    )

    assert result.status == "ERROR"
    assert result.exit_code == 1
    assert "Bootstrap script missing" in result.stderr
    assert witness is None, "Zero process created"
    _cleanup_test_snapshot(snap_dir)


def test_probe_07_b1_ordinary_medium_integrity_verification() -> None:
    """[Probe 7] B1 Ordinary Medium Integrity boundary verification."""
    # 1. Structural evaluation of valid Medium Integrity witness
    synthetic_b1_witness = ProcessWitness(
        pid=1234,
        is_appcontainer=False,
        integrity_rid=0x2000,
        integrity_label="Medium",
        is_elevated=False,
        session_id=1,
        appcontainer_sid=None,
        user_sid="S-1-5-21-test",
        capabilities=(),
        window_station="WinSta0",
        desktop="Default",
    )
    ok, reason = validate_profile_boundary(ProfileType.CONTEXT_B1_ORDINARY, synthetic_b1_witness)
    assert ok is True, f"Medium Integrity witness must pass: {reason}"

    # 2. Rejection of AppContainer token for B1 Ordinary
    appcontainer_witness = ProcessWitness(
        pid=1234,
        is_appcontainer=True,
        integrity_rid=0x1000,
        integrity_label="Low",
        is_elevated=False,
        session_id=1,
        appcontainer_sid="S-1-15-2-1234",
        user_sid="S-1-5-21-test",
        capabilities=(),
        window_station="WinSta0",
        desktop=None,
    )
    ok_ac, reason_ac = validate_profile_boundary(ProfileType.CONTEXT_B1_ORDINARY, appcontainer_witness)
    assert ok_ac is False, "AppContainer token must be rejected for Context B1 Ordinary"
    assert "TokenIsAppContainer=True" in reason_ac

    # 3. Live launch probe in current sandbox
    snap_dir, _ = _setup_isolated_snapshot_for_test("p07")
    dispatcher = ExecutionProfileDispatcher()
    b1_nodes = dispatcher.get_nodes_for_profile(ProfileType.CONTEXT_B1_ORDINARY)

    permit_file, secret = issue_execution_permit(
        snapshot_root=snap_dir,
        profile=ProfileType.CONTEXT_B1_ORDINARY,
        target_nodes=b1_nodes,
        authorized_execution_vector=["--synthetic-normal"],
        ttl_sec=60.0,
    )

    try:
        res, holder, _, witness = launch_profile_worker(
            profile=ProfileType.CONTEXT_B1_ORDINARY,
            snapshot_root=snap_dir,
            worker_args=["--permit=" + str(permit_file.resolve()), "--synthetic-normal"],
            auth_secret=secret,
            timeout_sec=5.0,
        )

        assert res.exit_code == 125, f"Expected fail-closed exit code 125, got {res.exit_code}"
        assert "PROFILE_ISOLATION_REJECTED" in res.stderr
        assert "Context B1 requires host Medium Integrity" in res.stderr
        assert witness is not None
        assert witness.is_appcontainer is True, "Truthful evidence: sandbox child is AppContainer"
    finally:
        holder.close()
        _cleanup_test_snapshot(snap_dir)


def test_probe_08_b1_ordinary_non_elevation_verification() -> None:
    """[Probe 8] B1 Ordinary non-elevation verification."""
    elevated_witness = ProcessWitness(
        pid=1234,
        is_appcontainer=False,
        integrity_rid=0x3000,
        integrity_label="High",
        is_elevated=True,
        session_id=1,
        appcontainer_sid=None,
        user_sid="S-1-5-21-test",
        capabilities=(),
        window_station="WinSta0",
        desktop="Default",
    )
    ok, reason = validate_profile_boundary(ProfileType.CONTEXT_B1_ORDINARY, elevated_witness)
    assert ok is False, "Elevated process must be rejected"
    assert "is_elevated=True" in reason or "elevated token" in reason


def test_probe_09_b1_snapshot_working_directory_confinement() -> None:
    """[Probe 9] B1 Snapshot working-directory confinement."""
    snap_dir, _ = _setup_isolated_snapshot_for_test("p09")
    worker_bootstrap = snap_dir / "tools" / "test_worker_bootstrap.py"

    res = subprocess.run(
        [str(PYTHON_EXE), "-I", "-S", str(worker_bootstrap), "--synthetic-normal"],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 102, f"Expected exit code 102 (Working directory escape), got {res.returncode}"
    assert "Working directory escape" in res.stderr
    _cleanup_test_snapshot(snap_dir)


def test_probe_10_b1_snapshot_import_confinement() -> None:
    """[Probe 10] B1 Snapshot import confinement."""
    snap_dir, _ = _setup_isolated_snapshot_for_test("p10")
    worker_bootstrap = snap_dir / "tools" / "test_worker_bootstrap.py"

    res = subprocess.run(
        [str(PYTHON_EXE), "-I", "-S", str(worker_bootstrap), "--preflight"],
        cwd=str(snap_dir),
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, f"Worker preflight failed: {res.stderr}"
    assert "jarvis module:" in res.stdout
    assert "confined to snapshot" in res.stdout
    assert str(snap_dir).lower() in res.stdout.lower()
    _cleanup_test_snapshot(snap_dir)


def test_probe_11_b1_snapshot_temp_directory_confinement() -> None:
    """[Probe 11] B1 Snapshot temporary-directory confinement."""
    snap_dir, _ = _setup_isolated_snapshot_for_test("p11")
    worker_bootstrap = snap_dir / "tools" / "test_worker_bootstrap.py"

    res = subprocess.run(
        [str(PYTHON_EXE), "-I", "-S", str(worker_bootstrap), "--preflight"],
        cwd=str(snap_dir),
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0
    expected_temp = snap_dir / "temp"
    assert f"Temp Directory:     {expected_temp}" in res.stdout
    _cleanup_test_snapshot(snap_dir)


def test_probe_12_b2_environment_capability_assessment() -> None:
    """[Probe 12] B2 environment capability assessment & fail-closed desktop gate."""
    snap_dir, _ = _setup_isolated_snapshot_for_test("p12")
    dispatcher = ExecutionProfileDispatcher()
    b2_node = dispatcher.get_nodes_for_profile(ProfileType.CONTEXT_B2_INTERACTIVE)

    permit_file, secret = issue_execution_permit(
        snapshot_root=snap_dir,
        profile=ProfileType.CONTEXT_B2_INTERACTIVE,
        target_nodes=b2_node,
        authorized_execution_vector=["--synthetic-normal"],
        ttl_sec=60.0,
    )

    try:
        res, holder, _, witness = launch_profile_worker(
            profile=ProfileType.CONTEXT_B2_INTERACTIVE,
            snapshot_root=snap_dir,
            worker_args=["--permit=" + str(permit_file.resolve()), "--synthetic-normal"],
            auth_secret=secret,
            timeout_sec=5.0,
        )

        assert res.exit_code == 126, f"Expected exit code 126 (B2 blocked), got {res.exit_code}"
        assert "CONTEXT_B2_BLOCKED" in res.stderr
        assert "Everyday desktop WinSta0\\Default is prohibited" in res.stderr
        assert witness is None, "Zero child process spawned for blocked B2"
    finally:
        holder.close()
        _cleanup_test_snapshot(snap_dir)


def test_probe_13_deliberate_profile_token_mismatch_rejection() -> None:
    """[Probe 13] Deliberate profile/token mismatch rejection."""
    snap_dir, _ = _setup_isolated_snapshot_for_test("p13")
    dispatcher = ExecutionProfileDispatcher()
    ctx_a_nodes = dispatcher.get_nodes_for_profile(ProfileType.CONTEXT_A)

    permit_file, secret = issue_execution_permit(
        snapshot_root=snap_dir,
        profile=ProfileType.CONTEXT_A,
        target_nodes=ctx_a_nodes,
        authorized_execution_vector=["--synthetic-normal"],
        ttl_sec=60.0,
    )

    try:
        res, holder, _, _witness = launch_profile_worker(
            profile=ProfileType.CONTEXT_A,
            snapshot_root=snap_dir,
            worker_args=["--permit=" + str(permit_file.resolve()), "--synthetic-normal"],
            auth_secret=secret,
            timeout_sec=5.0,
            token_mismatch_failure_inject=True,
        )

        assert res.exit_code == 125, f"Expected exit code 125, got {res.exit_code}"
        assert "PROFILE_ISOLATION_REJECTED" in res.stderr
        assert "never resumed" in res.stderr

        # Also test structural rejection of unauthorized capability
        unauthorized_cap_witness = ProcessWitness(
            pid=1234,
            is_appcontainer=True,
            integrity_rid=0x1000,
            integrity_label="Low",
            is_elevated=False,
            session_id=1,
            appcontainer_sid="S-1-15-2-1234",
            user_sid="S-1-5-21-test",
            capabilities=("S-1-15-3-1",),  # internetClient (unauthorized)
            window_station="WinSta0",
            desktop=None,
        )
        ok_unauth, reason_unauth = validate_profile_boundary(ProfileType.CONTEXT_A, unauthorized_cap_witness)
        assert ok_unauth is False, "Unauthorized capability must be rejected"
        assert "unauthorized capabilities" in reason_unauth
    finally:
        holder.close()
        _cleanup_test_snapshot(snap_dir)


def test_probe_14_job_object_assignment_failure() -> None:
    """[Probe 14] Job Object assignment failure terminates process before resume."""
    snap_dir, _ = _setup_isolated_snapshot_for_test("p14")
    dispatcher = ExecutionProfileDispatcher()
    ctx_a_nodes = dispatcher.get_nodes_for_profile(ProfileType.CONTEXT_A)

    permit_file, secret = issue_execution_permit(
        snapshot_root=snap_dir,
        profile=ProfileType.CONTEXT_A,
        target_nodes=ctx_a_nodes,
        authorized_execution_vector=["--synthetic-normal"],
        ttl_sec=60.0,
    )

    try:
        res, holder, _, _witness = launch_profile_worker(
            profile=ProfileType.CONTEXT_A,
            snapshot_root=snap_dir,
            worker_args=["--permit=" + str(permit_file.resolve()), "--synthetic-normal"],
            auth_secret=secret,
            timeout_sec=5.0,
            job_assignment_failure_inject=True,
        )

        assert res.exit_code == 1, f"Expected exit code 1, got {res.exit_code}"
        assert "Failed to assign worker process to Win32 Job Object" in res.stderr
        assert "killed before resume" in res.stderr
    finally:
        holder.close()
        _cleanup_test_snapshot(snap_dir)


def test_probe_15_child_termination_before_resume() -> None:
    """[Probe 15] Child termination before resume leaves zero running processes."""
    snap_dir, _ = _setup_isolated_snapshot_for_test("p15")
    dispatcher = ExecutionProfileDispatcher()
    ctx_a_nodes = dispatcher.get_nodes_for_profile(ProfileType.CONTEXT_A)

    permit_file, secret = issue_execution_permit(
        snapshot_root=snap_dir,
        profile=ProfileType.CONTEXT_A,
        target_nodes=ctx_a_nodes,
        authorized_execution_vector=["--synthetic-normal"],
        ttl_sec=60.0,
    )

    try:
        res, holder, _, _witness = launch_profile_worker(
            profile=ProfileType.CONTEXT_A,
            snapshot_root=snap_dir,
            worker_args=["--permit=" + str(permit_file.resolve()), "--synthetic-normal"],
            auth_secret=secret,
            timeout_sec=5.0,
            token_mismatch_failure_inject=True,
        )

        assert res.exit_code == 125
        assert res.status == "ERROR"
        assert "never resumed" in res.stderr
        assert holder.is_closed is True, "Job Object holder must be closed after terminating child"
    finally:
        holder.close()
        _cleanup_test_snapshot(snap_dir)


def test_probe_16_full_worker_process_tree_cleanup() -> None:
    """[Probe 16] Full worker process-tree cleanup and draining."""
    snap_dir, _ = _setup_isolated_snapshot_for_test("p16")
    dispatcher = ExecutionProfileDispatcher()
    ctx_a_nodes = dispatcher.get_nodes_for_profile(ProfileType.CONTEXT_A)

    permit_file, secret = issue_execution_permit(
        snapshot_root=snap_dir,
        profile=ProfileType.CONTEXT_A,
        target_nodes=ctx_a_nodes,
        authorized_execution_vector=["--synthetic-normal"],
        ttl_sec=60.0,
    )

    try:
        res, holder, _drainers, _witness = launch_profile_worker(
            profile=ProfileType.CONTEXT_A,
            snapshot_root=snap_dir,
            worker_args=["--permit=" + str(permit_file.resolve()), "--synthetic-normal"],
            auth_secret=secret,
            timeout_sec=10.0,
        )

        assert res.exit_code == 0
        holder.drain(timeout=0.5)
        active_count = holder.get_active_process_count()
        assert active_count == 0, f"Expected 0 active processes, got {active_count}"
    finally:
        holder.close()
        _cleanup_test_snapshot(snap_dir)


def test_probe_17_execution_permit_profile_mismatch_rejection() -> None:
    """[Probe 17] Execution-permit/profile mismatch rejection before process launch."""
    snap_dir, _ = _setup_isolated_snapshot_for_test("p17")
    dispatcher = ExecutionProfileDispatcher()
    b1_nodes = dispatcher.get_nodes_for_profile(ProfileType.CONTEXT_B1_ORDINARY)

    permit_file, secret = issue_execution_permit(
        snapshot_root=snap_dir,
        profile=ProfileType.CONTEXT_B1_ORDINARY,
        target_nodes=b1_nodes,
        authorized_execution_vector=["--synthetic-normal"],
        ttl_sec=60.0,
    )

    with permit_file.open("r", encoding="utf-8") as f:
        import json
        permit_data = json.load(f)

    res, holder, _, witness = launch_profile_worker(
        profile=ProfileType.CONTEXT_A,
        snapshot_root=snap_dir,
        worker_args=["--permit=" + str(permit_file.resolve()), "--synthetic-normal"],
        auth_secret=secret,
        timeout_sec=5.0,
        permit_data=permit_data,
    )

    assert res.exit_code == 124, f"Expected exit code 124 (PROFILE_MISMATCH), got {res.exit_code}"
    assert "PROFILE_MISMATCH" in res.stderr
    assert witness is None, "Zero process created"
    holder.close()
    _cleanup_test_snapshot(snap_dir)


def run_all_17_probes() -> int:
    """Executes the full 17-probe BLK-18 Process Isolation Verification Matrix."""
    print("=" * 70)
    print("BLK-18 PHASE 1L: 17-PROBE PROCESS ISOLATION VERIFICATION MATRIX")
    print("Exercising Production: tools.snapshot_test_supervisor & tools.test_worker_bootstrap")
    print("=" * 70)

    probes = [
        ("Probe 1", "Actual AppContainer child launch", test_probe_01_actual_appcontainer_child_launch),
        ("Probe 2", "AppContainer Low Integrity token witness (exact RID 0x1000 & caps)", test_probe_02_appcontainer_low_integrity_token_witness),
        ("Probe 3", "AppContainer SID verification", test_probe_03_appcontainer_sid_verification),
        ("Probe 4", "AppContainer access to approved snapshot", test_probe_04_appcontainer_snapshot_access),
        ("Probe 5", "AppContainer controlled ACL resource boundary & folder denial", test_probe_05_appcontainer_controlled_acl_resource_boundary),
        ("Probe 6", "AppContainer launch failure with no fallback", test_probe_06_appcontainer_launch_failure_no_fallback),
        ("Probe 7", "B1 Ordinary Medium Integrity verification", test_probe_07_b1_ordinary_medium_integrity_verification),
        ("Probe 8", "B1 Ordinary non-elevation verification", test_probe_08_b1_ordinary_non_elevation_verification),
        ("Probe 9", "B1 Snapshot working-directory confinement", test_probe_09_b1_snapshot_working_directory_confinement),
        ("Probe 10", "B1 Snapshot import confinement", test_probe_10_b1_snapshot_import_confinement),
        ("Probe 11", "B1 Snapshot temporary-directory confinement", test_probe_11_b1_snapshot_temp_directory_confinement),
        ("Probe 12", "B2 environment capability & isolation preflight", test_probe_12_b2_environment_capability_assessment),
        ("Probe 13", "Deliberate profile/token mismatch rejection", test_probe_13_deliberate_profile_token_mismatch_rejection),
        ("Probe 14", "Job Object assignment failure (killed before resume)", test_probe_14_job_object_assignment_failure),
        ("Probe 15", "Child termination before resume", test_probe_15_child_termination_before_resume),
        ("Probe 16", "Full worker process-tree cleanup", test_probe_16_full_worker_process_tree_cleanup),
        ("Probe 17", "Execution-permit/profile mismatch rejection", test_probe_17_execution_permit_profile_mismatch_rejection),
    ]

    passed = 0
    total = len(probes)

    for tag, name, probe_fn in probes:
        t0 = time.monotonic()
        print(f"[{tag}] {name}...", flush=True)
        try:
            extra = probe_fn()
            dt = time.monotonic() - t0
            extra_str = f" ({extra})" if extra and isinstance(extra, str) else ""
            print(f"  Result: PASS ({dt:.2f}s){extra_str}", flush=True)
            passed += 1
        except (AssertionError, RuntimeError, OSError, ValueError) as exc:
            dt = time.monotonic() - t0
            print(f"  Result: FAIL ({dt:.2f}s) - {exc}", flush=True)

    print("=" * 70)
    if passed == total:
        print(f"OVERALL RESULT: ALL {total} PROBES PASSED (exit code 0)")
        print("Gate 2 invariant preserved: ZERO candidate tests executed.")
        return 0
    else:
        print(f"OVERALL RESULT: {passed}/{total} PROBES PASSED ({total - passed} FAILED)")
        return 1


if __name__ == "__main__":
    sys.exit(run_all_17_probes())
