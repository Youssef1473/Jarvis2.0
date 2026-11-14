"""Snapshot Test Supervisor — Isolated Execution & Containment Pipeline.

Manages disposable filesystem snapshots, deterministic 12-variable worker environments,
Win32 Job Object containment, full source-to-destination SHA-256 tree integrity,
conditional failure-dependent quarantine, and single-use execution permits.

Invariants Remediated (ENG-OS-03C9 Phase 1B):
- BLK-18: Four-profile dispatcher integration and boundary enforcement.
- BLK-19: Full recursive source/destination SHA-256 tree manifest, reparse-point audit.
- BLK-20: Conditional cleanup state machine; failure/timeout snapshot quarantine.
- BLK-21: Fail-closed supervisor-issued single-use execution permit gate.
- BLK-22: Drainer thread lifecycle verification and stall detection.
"""

from __future__ import annotations

import ctypes
import fnmatch
import hashlib
import hmac
import json
import msvcrt
import os
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from ctypes import wintypes
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# Ensure repository root is on sys.path for supervisor imports
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from jarvis.skills.job_object_sandbox import (
    JobHandleHolder,
    _active_process_count,
    _resume_process,
    _terminate_and_drain,
    assign_process_to_job,
    create_process_job_object,
)
from tools.execution_profile_dispatcher import ExecutionProfileDispatcher, ProfileType

# ---------------------------------------------------------------------------
# Constants & Configuration
# ---------------------------------------------------------------------------
ORIGINAL_REPO: Path = Path(r"C:\Users\Youssef\Projects\Jarvis_2.0").resolve()
PYTHON_EXE: Path = Path(r"C:\Users\Youssef\Projects\.venv\Scripts\python.exe").resolve()
APPROVED_ENV_EXAMPLE_SHA256: str = "705b1b1a1cf11059f093551095a6e46b60d0b285c16ff0e9dcec12f3225666f8"

# Subtrees to exclude entirely from snapshot copy
SUBTREE_EXCLUSIONS: frozenset[str] = frozenset({
    ".git",
    ".venv",
    ".agents",
    ".gemini",
    ".pytest_cache",
    ".ruff_cache",
    "docs",
    "tests/eval",
    "data/notes",
    "data/execution_evidence",
    "data/pytest_tmp",
    "data/pytest_temp",
    "data/temp_pytest",
    "data/pytest_cache",
    "data/test_artifacts",
    "data/agy_runtime_workspace",
    "data/browser_debug_profile",
    "data/models",
    "jarvis/skills/custom",
    "data/evolution_scratch",
    "data/snapshots",
    "data/learning_candidates",
})

# Sensitive filename patterns excluded from snapshot
SENSITIVE_PATTERNS: tuple[str, ...] = (
    "*.pem",
    "*.key",
    "*.pfx",
    "*.p12",
    "*.crt",
    "*.cer",
    "*.der",
    ".npmrc",
    ".pypirc",
    ".netrc",
)

# Fresh runtime directories initialized empty after copy
FRESH_RUNTIME_DIRS: tuple[str, ...] = (
    "jarvis/skills/custom",
    "data/evolution_scratch",
    "data/snapshots",
    "data/learning_candidates",
    "temp",
)


@dataclass
class SupervisorResult:
    """Structured result from supervised worker execution."""
    status: str  # "COMPLETED", "TIMEOUT", "ERROR", "PREFLIGHT_PASSED", "DRAINER_STALLED"
    exit_code: int
    stdout: str
    stderr: str
    duration_sec: float
    active_processes_at_timeout: int | None = None
    drainer_stalled: bool = False
    cleanup_action: str = "UNKNOWN"
    diagnostics: list[str] = field(default_factory=list)


# Exclusion Rule Identifiers (Task 5)
RULE_VCS_METADATA = "RULE_VCS_METADATA"           # .git, .github, .gemini
RULE_VIRTUALENV = "RULE_VIRTUALENV"               # .venv
RULE_AGENTS_FRAMEWORK = "RULE_AGENTS_FRAMEWORK"   # .agents
RULE_DOCS_TREE = "RULE_DOCS_TREE"                 # docs
RULE_BYTECODE_CACHE = "RULE_BYTECODE_CACHE"       # __pycache__, *.pyc, *.pyo
RULE_TEST_CACHE = "RULE_TEST_CACHE"               # .pytest_cache, .ruff_cache
RULE_TEST_EVAL = "RULE_TEST_EVAL"                 # tests/eval
RULE_DATA_SCRATCH = "RULE_DATA_SCRATCH"           # data/notes, data/execution_evidence, data/pytest_tmp, etc.
RULE_BROWSER_PROFILE = "RULE_BROWSER_PROFILE"     # data/browser_debug_profile
RULE_MODEL_STORAGE = "RULE_MODEL_STORAGE"         # data/models
RULE_RUNTIME_MUTATION = "RULE_RUNTIME_MUTATION"   # jarvis/skills/custom, data/evolution_scratch, data/snapshots, data/learning_candidates
RULE_SENSITIVE_SECRET = "RULE_SENSITIVE_SECRET"   # *.pem, *.key, *.pfx, etc.
RULE_UNAPPROVED_ENV = "RULE_UNAPPROVED_ENV"       # .env, .env.local, nested .env.example


@dataclass(frozen=True)
class FileInventoryEntry:
    """Canonical inventory entry for an approved file."""
    rel_path: str
    size_bytes: int
    sha256: str
    mtime: float


@dataclass(frozen=True)
class ExclusionLedgerEntry:
    """Machine-readable ledger entry for an excluded path."""
    relative_path: str
    rule_id: str
    reason: str
    exclusion_type: str  # "SUBTREE" or "FILE"
    verification_status: str = "VERIFIED_EXCLUDED"


@dataclass
class SnapshotManifest:
    """Full recursive tree manifest and provenance receipt."""
    snapshot_id: str
    created_at: float
    source_root: str
    destination_root: str
    source_file_count: int
    destination_file_count: int
    matched_hashes_count: int
    source_before_digest: str
    source_after_digest: str
    destination_digest: str
    tree_digest: str
    exclusion_count: int
    reparse_points_detected: int
    env_example_verified: bool
    fresh_directory_assertions: list[str]
    exclusions: list[dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Reparse-Point Audit & Hash Calculations (BLK-19)
# ---------------------------------------------------------------------------
def audit_reparse_points(root_path: Path) -> list[str]:
    """Scans directory tree and returns list of paths containing reparse points.

    Guarantees:
    - Traversal never descends into reparse points (dirs pruned in-place).
    - Windows FILE_ATTRIBUTE_REPARSE_POINT (0x400) and stat.S_ISLNK checked.
    - Errors reading directory or file metadata fail closed by raising RuntimeError.
    """
    reparse_points: list[str] = []
    for root, dirs, files in os.walk(root_path, followlinks=False):
        kept_dirs: list[str] = []
        for d in dirs:
            p = Path(root) / d
            try:
                st = p.lstat()
                is_sym = stat.S_ISLNK(st.st_mode)
                file_attr = getattr(st, "st_file_attributes", 0)
                is_reparse = bool(file_attr & stat.FILE_ATTRIBUTE_REPARSE_POINT)
                if is_sym or is_reparse:
                    reparse_points.append(str(p))
                    # Prune so os.walk NEVER traverses into the reparse point
                    continue
            except OSError as exc:
                raise RuntimeError(f"REPARSE_AUDIT_STAT_FAILED: Cannot stat directory {p}: {exc}")
            kept_dirs.append(d)
        dirs[:] = kept_dirs

        for f in files:
            p = Path(root) / f
            try:
                st = p.lstat()
                is_sym = stat.S_ISLNK(st.st_mode)
                file_attr = getattr(st, "st_file_attributes", 0)
                is_reparse = bool(file_attr & stat.FILE_ATTRIBUTE_REPARSE_POINT)
                if is_sym or is_reparse:
                    reparse_points.append(str(p))
            except OSError as exc:
                raise RuntimeError(f"REPARSE_AUDIT_STAT_FAILED: Cannot stat file {p}: {exc}")
    return reparse_points


def compute_file_sha256(file_path: Path) -> str:
    """Compute lowercase SHA-256 hex digest of a file."""
    h = hashlib.sha256()
    with file_path.open("rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest().lower()


def compute_inventory_digest(inventory: dict[str, FileInventoryEntry]) -> str:
    """Computes deterministic canonical SHA-256 digest over sorted inventory entries."""
    hasher = hashlib.sha256()
    for rel_path in sorted(inventory.keys()):
        entry = inventory[rel_path]
        hasher.update(f"{rel_path}:{entry.size_bytes}:{entry.sha256}\n".encode())
    return hasher.hexdigest().lower()


def classify_exclusion_path(rel_path: str, is_dir: bool) -> tuple[str, str, str] | None:
    """Classifies relative path against security exclusion policy.

    Returns:
        (rule_id, reason, 'SUBTREE'|'FILE') or None if eligible for copy.
    """
    rel_posix = rel_path.replace("\\", "/").strip("/")
    rel_lower = rel_posix.lower()
    name_lower = rel_lower.split("/")[-1]

    # 1. Bytecode & Cache Directories / Files
    if name_lower == "__pycache__":
        return (RULE_BYTECODE_CACHE, "Compiled Python bytecode directory", "SUBTREE")
    if name_lower.endswith((".pyc", ".pyo")):
        return (RULE_BYTECODE_CACHE, "Compiled Python bytecode file", "FILE")
    if name_lower in (".pytest_cache", ".ruff_cache"):
        return (RULE_TEST_CACHE, "Linter and test cache directory", "SUBTREE")

    # 2. VCS & Agent Framework
    if rel_lower in (".git", ".github", ".gemini") or any(rel_lower.startswith(f"{p}/") for p in (".git", ".github", ".gemini")):
        return (RULE_VCS_METADATA, "Version control system metadata", "SUBTREE" if is_dir else "FILE")
    if rel_lower == ".venv" or rel_lower.startswith(".venv/"):
        return (RULE_VIRTUALENV, "Host Python virtual environment", "SUBTREE" if is_dir else "FILE")
    if rel_lower == ".agents" or rel_lower.startswith(".agents/"):
        return (RULE_AGENTS_FRAMEWORK, "Agent configuration and historical logs", "SUBTREE" if is_dir else "FILE")
    if rel_lower == "docs" or rel_lower.startswith("docs/"):
        return (RULE_DOCS_TREE, "Repository documentation tree", "SUBTREE" if is_dir else "FILE")
    if rel_lower == "tests/eval" or rel_lower.startswith("tests/eval/"):
        return (RULE_TEST_EVAL, "Evaluation test datasets and historical logs", "SUBTREE" if is_dir else "FILE")

    # 3. Dynamic Scratch & Runtime Directories
    scratch_subtrees = (
        "data/notes", "data/execution_evidence", "data/pytest_tmp", "data/pytest_temp",
        "data/temp_pytest", "data/pytest_cache", "data/test_artifacts", "data/agy_runtime_workspace",
    )
    if rel_lower in scratch_subtrees or any(rel_lower.startswith(f"{p}/") for p in scratch_subtrees):
        return (RULE_DATA_SCRATCH, "Test execution evidence and ephemeral scratch", "SUBTREE" if is_dir else "FILE")
    if rel_lower == "data/browser_debug_profile" or rel_lower.startswith("data/browser_debug_profile/"):
        return (RULE_BROWSER_PROFILE, "Browser debugging profile and cookies", "SUBTREE" if is_dir else "FILE")
    if rel_lower == "data/models" or rel_lower.startswith("data/models/"):
        return (RULE_MODEL_STORAGE, "Large language model and neural weights", "SUBTREE" if is_dir else "FILE")

    # 4. Fresh Runtime Directories (created empty in destination)
    mutation_dirs = ("jarvis/skills/custom", "data/evolution_scratch", "data/snapshots", "data/learning_candidates")
    if rel_lower in mutation_dirs or any(rel_lower.startswith(f"{p}/") for p in mutation_dirs):
        return (RULE_RUNTIME_MUTATION, "Dynamic runtime mutation directory (initialized fresh)", "SUBTREE" if is_dir else "FILE")

    # 5. Sensitive credentials, keys, certificates
    if any(fnmatch.fnmatch(name_lower, pat) for pat in SENSITIVE_PATTERNS):
        return (RULE_SENSITIVE_SECRET, f"Sensitive secret pattern match: {name_lower}", "FILE")

    # 6. Environment Files (.env*)
    if name_lower.startswith(".env"):
        if rel_posix == ".env.example":
            return None  # Root .env.example is permitted (subject to hash verification)
        return (RULE_UNAPPROVED_ENV, f"Unapproved environment file variant: {rel_posix}", "FILE")

    return None


def build_source_inventory(
    source_root: Path,
) -> tuple[dict[str, FileInventoryEntry], list[ExclusionLedgerEntry]]:
    """Recursively audits source tree without following reparse points.

    Returns:
        tuple[inventory, exclusion_ledger]
        inventory: canonical_rel_path -> FileInventoryEntry
        exclusion_ledger: list of ExclusionLedgerEntry
    """
    inventory: dict[str, FileInventoryEntry] = {}
    ledger: list[ExclusionLedgerEntry] = []
    source_root_resolved = source_root.resolve()

    for root, dirs, files in os.walk(source_root_resolved, followlinks=False):
        rel_root = Path(root).relative_to(source_root_resolved).as_posix()

        # Check directories for pruning
        kept_dirs = []
        for d in dirs:
            rel_d = f"{rel_root}/{d}" if rel_root != "." else d
            classification = classify_exclusion_path(rel_d, is_dir=True)
            if classification is not None:
                rule_id, reason, ex_type = classification
                ledger.append(ExclusionLedgerEntry(
                    relative_path=rel_d.replace("\\", "/"),
                    rule_id=rule_id,
                    reason=reason,
                    exclusion_type=ex_type,
                    verification_status="VERIFIED_EXCLUDED",
                ))
            else:
                # Check for reparse point
                d_path = Path(root) / d
                try:
                    st = d_path.lstat()
                    if stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
                        raise RuntimeError(f"REPARSE_POINT_DETECTED: Reparse point in source directory: {rel_d}")
                except OSError as exc:
                    raise RuntimeError(f"STAT_FAILED: Cannot stat source directory {rel_d}: {exc}")
                kept_dirs.append(d)
        dirs[:] = kept_dirs

        for f in files:
            rel_f = f"{rel_root}/{f}" if rel_root != "." else f
            classification = classify_exclusion_path(rel_f, is_dir=False)
            if classification is not None:
                rule_id, reason, ex_type = classification
                ledger.append(ExclusionLedgerEntry(
                    relative_path=rel_f.replace("\\", "/"),
                    rule_id=rule_id,
                    reason=reason,
                    exclusion_type=ex_type,
                    verification_status="VERIFIED_EXCLUDED",
                ))
                continue

            f_path = Path(root) / f
            try:
                st = f_path.lstat()
                if stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
                    raise RuntimeError(f"REPARSE_POINT_DETECTED: Reparse point in source file: {rel_f}")
                sz = st.st_size
                mtime = st.st_mtime
            except OSError as exc:
                raise RuntimeError(f"STAT_FAILED: Cannot stat source file {rel_f}: {exc}")

            h = compute_file_sha256(f_path)
            rel_key = rel_f.replace("\\", "/")
            inventory[rel_key] = FileInventoryEntry(
                rel_path=rel_key,
                size_bytes=sz,
                sha256=h,
                mtime=mtime,
            )

    return inventory, ledger


def snapshot_copy_filter(src: str, names: list[str], base_repo: Path | None = None) -> set[str]:
    """Relative copy filter for shutil.copytree adhering to isolation invariants."""
    base = (base_repo if base_repo is not None else ORIGINAL_REPO).resolve()
    src_path = Path(src).resolve()
    try:
        rel_dir = src_path.relative_to(base).as_posix().lower()
    except ValueError:
        rel_dir = "."

    ignored: set[str] = set()
    for item in names:
        rel_path = f"{rel_dir}/{item}" if rel_dir != "." else item
        item_path = src_path / item
        is_dir = item_path.is_dir()
        if classify_exclusion_path(rel_path, is_dir=is_dir) is not None:
            ignored.add(item)

    return ignored


def build_tree_manifest(
    source_root: Path,
    dest_root: Path,
    exclusions: dict[str, str],
) -> SnapshotManifest:
    """Builds complete 1:1 source-to-destination SHA-256 manifest and verifies integrity."""
    dest_posix = dest_root.as_posix().lower()
    fresh_prefixes = tuple(f"{dest_posix}/{d.lower()}" for d in FRESH_RUNTIME_DIRS)

    dest_inventory: dict[str, FileInventoryEntry] = {}
    for root, _, files in os.walk(dest_root, followlinks=False):
        root_posix = Path(root).as_posix().lower()
        if any(root_posix == pfx or root_posix.startswith(f"{pfx}/") for pfx in fresh_prefixes):
            continue

        for f in files:
            if f in ("snapshot_manifest.json", "snapshot_integrity_receipt.json"):
                continue
            dest_file = Path(root) / f
            rel_file = dest_file.relative_to(dest_root).as_posix()
            st = dest_file.lstat()
            dest_inventory[rel_file] = FileInventoryEntry(
                rel_path=rel_file,
                size_bytes=st.st_size,
                sha256=compute_file_sha256(dest_file),
                mtime=st.st_mtime,
            )

    source_inventory, _ = build_source_inventory(source_root)

    matched = 0
    mismatches: list[str] = []
    for rel_file, d_entry in dest_inventory.items():
        s_entry = source_inventory.get(rel_file)
        if s_entry and s_entry.sha256 == d_entry.sha256:
            matched += 1
        else:
            mismatches.append(f"{rel_file} (src: {s_entry.sha256 if s_entry else None} != dst: {d_entry.sha256})")

    if mismatches:
        raise RuntimeError(f"INTEGRITY_HASH_MISMATCH: {len(mismatches)} files differed: {mismatches[:3]}")

    digest_before = compute_inventory_digest(source_inventory)
    dest_digest = compute_inventory_digest(dest_inventory)
    composite = hashlib.sha256(f"{digest_before}:{dest_digest}:{dest_root.name}".encode()).hexdigest().lower()

    fresh_assertions: list[str] = []
    for d in FRESH_RUNTIME_DIRS:
        target = dest_root / d
        if not target.is_dir():
            raise RuntimeError(f"FRESH_DIR_MISSING: Expected {d} to exist in snapshot")
        leaf_count = sum(len(f) for _, _, f in os.walk(target))
        if leaf_count != 0:
            raise RuntimeError(f"FRESH_DIR_NOT_EMPTY: Expected {d} to be empty, found {leaf_count} files")
        fresh_assertions.append(f"{d}: verified empty (0 files)")

    return SnapshotManifest(
        snapshot_id=dest_root.name,
        created_at=time.time(),
        source_root=str(source_root),
        destination_root=str(dest_root),
        source_file_count=len(source_inventory),
        destination_file_count=len(dest_inventory),
        matched_hashes_count=matched,
        source_before_digest=digest_before,
        source_after_digest=digest_before,
        destination_digest=dest_digest,
        tree_digest=composite,
        exclusion_count=len(exclusions),
        reparse_points_detected=0,
        env_example_verified=True,
        fresh_directory_assertions=fresh_assertions,
        exclusions=[{"relative_path": k, "rule_id": v} for k, v in exclusions.items()],
    )


def quarantine_snapshot(dest_dir: Path, reason: str) -> Path:
    """Quarantines corrupted or failed snapshot directory, tagging reason and preserving evidence."""
    if not dest_dir.exists():
        return dest_dir
    quarantine_target = dest_dir.parent / f"{dest_dir.name}_QUARANTINED"
    try:
        marker = dest_dir / ".snapshot_quarantine_reason.txt"
        try:
            marker.write_text(f"QUARANTINE_REASON={reason}\nTIMESTAMP={time.time()}\n", encoding="utf-8")
        except OSError:
            pass
        if quarantine_target.exists():
            cleanup_snapshot(quarantine_target, force=True)
        dest_dir.rename(quarantine_target)
        return quarantine_target
    except OSError:
        return dest_dir


def provision_snapshot(
    dest_dir: Path | None = None,
    source_root: Path | None = None,
    run_id: str | None = None,
) -> tuple[Path, SnapshotManifest]:
    """Provisions isolated snapshot with full SHA-256 tree verification, stable source audit, and provenance receipt."""
    src = (source_root if source_root is not None else ORIGINAL_REPO).resolve()
    rid = run_id or f"run_{uuid.uuid4().hex[:8]}"

    if dest_dir is None:
        temp_base = Path(tempfile.gettempdir()).resolve()
        dest_dir = temp_base / f"jarvis_snap_{uuid.uuid4().hex[:8]}"
    dest_dir = dest_dir.resolve()

    if dest_dir.exists():
        raise FileExistsError(
            f"SECURITY_GATE_FAILED: Snapshot destination already exists; collision refused: {dest_dir}"
        )

    # Validate canonical destination path and ensure parent is not a reparse point
    dest_dir = dest_dir.resolve()
    try:
        parent_st = dest_dir.parent.lstat()
        if stat.S_ISLNK(parent_st.st_mode) or bool(
            getattr(parent_st, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
        ):
            raise RuntimeError(
                f"SECURITY_GATE_FAILED: Snapshot destination parent is a reparse point: {dest_dir.parent}"
            )
    except OSError as exc:
        raise RuntimeError(
            f"SECURITY_GATE_FAILED: Cannot stat snapshot destination parent {dest_dir.parent}: {exc}"
        )

    # 1. Source Reparse-Point Audit (pre-copy) (Task 4)
    src_reparse = audit_reparse_points(src)
    if src_reparse:
        raise RuntimeError(f"SECURITY_GATE_FAILED: Reparse points found in source repository: {src_reparse[:3]}")

    # 2. Capture Complete Source Inventory BEFORE Copy (Task 2)
    source_before, ledger = build_source_inventory(src)
    digest_before = compute_inventory_digest(source_before)

    # Verify .env.example presence and hash in source_before (Task 5)
    if ".env.example" not in source_before:
        raise RuntimeError("SECURITY_GATE_FAILED: .env.example not found in source repository")
    if source_before[".env.example"].sha256 != APPROVED_ENV_EXAMPLE_SHA256:
        raise RuntimeError(
            f"SECURITY_GATE_FAILED: .env.example hash mismatch: {source_before['.env.example'].sha256} != {APPROVED_ENV_EXAMPLE_SHA256}"
        )

    # 3. Perform Copy
    def _tracking_filter(cur_src: str, names: list[str]) -> set[str]:
        ignored = set()
        cur_path = Path(cur_src).resolve()
        for name in names:
            item_path = cur_path / name
            try:
                rel = item_path.relative_to(src).as_posix()
            except ValueError:
                rel = name
            is_dir = item_path.is_dir()
            if classify_exclusion_path(rel, is_dir) is not None:
                ignored.add(name)
        return ignored

    try:
        shutil.copytree(str(src), str(dest_dir), ignore=_tracking_filter)
    except (OSError, shutil.Error) as exc:
        quarantine_snapshot(dest_dir, f"COPY_FAILED: {exc}")
        raise RuntimeError(f"SECURITY_GATE_FAILED: Copy tree failed: {exc}")

    # 4. Capture Complete Source Inventory AFTER Copy (Task 2)
    source_after, _ = build_source_inventory(src)
    digest_after = compute_inventory_digest(source_after)

    # Source Stability Equality Check: SOURCE_BEFORE == SOURCE_AFTER (Task 2)
    if digest_before != digest_after or set(source_before.keys()) != set(source_after.keys()):
        quarantine_snapshot(dest_dir, "SOURCE_MUTATION_DURING_COPY")
        added = set(source_after.keys()) - set(source_before.keys())
        removed = set(source_before.keys()) - set(source_after.keys())
        diff_hashes = [
            k for k in (set(source_before.keys()) & set(source_after.keys()))
            if source_before[k].sha256 != source_after[k].sha256
        ]
        raise RuntimeError(
            f"SECURITY_GATE_FAILED: Source mutated during copy! Added: {len(added)}, "
            f"Removed: {len(removed)}, Modified: {len(diff_hashes)}"
        )

    # 5. Destination Reparse-Point Audit (Task 4)
    dest_reparse = audit_reparse_points(dest_dir)
    if dest_reparse:
        quarantine_snapshot(dest_dir, "REPARSE_POINT_IN_DESTINATION")
        raise RuntimeError(f"SECURITY_GATE_FAILED: Reparse points generated in snapshot destination: {dest_reparse[:3]}")

    # 6. Initialize and verify fresh runtime directories
    fresh_assertions: list[str] = []
    for sub in FRESH_RUNTIME_DIRS:
        target = dest_dir / sub
        target.mkdir(parents=True, exist_ok=True)
        leaf_count = sum(len(f) for _, _, f in os.walk(target))
        if leaf_count != 0:
            quarantine_snapshot(dest_dir, f"FRESH_DIR_NOT_EMPTY_{sub}")
            raise RuntimeError(f"SECURITY_GATE_FAILED: Fresh directory {sub} is not empty (found {leaf_count} files)")
        fresh_assertions.append(f"{sub}: verified empty (0 files)")

    # 7. Audit Destination Inventory & Exact Parity (Task 3)
    dest_posix = dest_dir.as_posix().lower()
    fresh_prefixes = tuple(f"{dest_posix}/{d.lower()}" for d in FRESH_RUNTIME_DIRS)
    dest_inventory: dict[str, FileInventoryEntry] = {}
    dest_cases: set[str] = set()

    for root, _, files in os.walk(dest_dir, followlinks=False):
        root_posix = Path(root).as_posix().lower()
        if any(root_posix == pfx or root_posix.startswith(f"{pfx}/") for pfx in fresh_prefixes):
            continue

        for f in files:
            # Exclude metadata manifests from inventory calculation to prevent circular self-hashing
            if f in ("snapshot_manifest.json", "snapshot_integrity_receipt.json"):
                continue

            dest_file = Path(root) / f
            rel_file = dest_file.relative_to(dest_dir).as_posix()
            rel_lower = rel_file.lower()

            # Case collision check
            if rel_lower in dest_cases:
                quarantine_snapshot(dest_dir, f"CASE_COLLISION_{rel_file}")
                raise RuntimeError(f"SECURITY_GATE_FAILED: Case collision detected in destination: {rel_file}")
            dest_cases.add(rel_lower)

            # Prohibited .env variant check
            if f.lower().startswith(".env") and (f.lower() != ".env.example" or rel_file != ".env.example"):
                quarantine_snapshot(dest_dir, f"PROHIBITED_ENV_{rel_file}")
                raise RuntimeError(f"SECURITY_GATE_FAILED: Prohibited .env variant found: {rel_file}")

            # Sensitive pattern check
            if any(fnmatch.fnmatch(f.lower(), pat) for pat in SENSITIVE_PATTERNS):
                quarantine_snapshot(dest_dir, f"SENSITIVE_LEAK_{rel_file}")
                raise RuntimeError(f"SECURITY_GATE_FAILED: Sensitive file leaked into snapshot: {rel_file}")

            st = dest_file.lstat()
            h = compute_file_sha256(dest_file)
            dest_inventory[rel_file] = FileInventoryEntry(
                rel_path=rel_file,
                size_bytes=st.st_size,
                sha256=h,
                mtime=st.st_mtime,
            )

    # Verify exact inventory equality (Task 3)
    missing_in_dest = set(source_before.keys()) - set(dest_inventory.keys())
    if missing_in_dest:
        quarantine_snapshot(dest_dir, "MISSING_DESTINATION_FILES")
        raise RuntimeError(f"SECURITY_GATE_FAILED: {len(missing_in_dest)} source files missing in destination: {sorted(missing_in_dest)[:3]}")

    unexpected_in_dest = set(dest_inventory.keys()) - set(source_before.keys())
    if unexpected_in_dest:
        quarantine_snapshot(dest_dir, "UNEXPECTED_DESTINATION_FILES")
        raise RuntimeError(f"SECURITY_GATE_FAILED: {len(unexpected_in_dest)} unexpected files in destination: {sorted(unexpected_in_dest)[:3]}")

    # Verify 1:1 Hash Parity
    matched = 0
    mismatched = []
    for rel_file, d_entry in dest_inventory.items():
        s_entry = source_before[rel_file]
        if d_entry.sha256 == s_entry.sha256 and d_entry.size_bytes == s_entry.size_bytes:
            matched += 1
        else:
            mismatched.append(f"{rel_file} (src: {s_entry.sha256} != dst: {d_entry.sha256})")

    if mismatched:
        quarantine_snapshot(dest_dir, "DESTINATION_HASH_MISMATCH")
        raise RuntimeError(f"SECURITY_GATE_FAILED: {len(mismatched)} destination files differed: {mismatched[:3]}")

    dest_digest = compute_inventory_digest(dest_inventory)
    if dest_digest != digest_before:
        quarantine_snapshot(dest_dir, "DESTINATION_DIGEST_MISMATCH")
        raise RuntimeError(f"SECURITY_GATE_FAILED: Destination digest mismatch: {dest_digest} != {digest_before}")

    # 8. Cryptographic Provenance Receipt & Manifest (Task 7)
    composite_snapshot_digest = hashlib.sha256(f"{digest_before}:{dest_digest}:{dest_dir.name}".encode()).hexdigest().lower()

    receipt_data = {
        "run_id": rid,
        "snapshot_id": dest_dir.name,
        "source_root": str(src),
        "snapshot_root": str(dest_dir),
        "created_at": time.time(),
        "source_before_digest": digest_before,
        "source_after_digest": digest_after,
        "destination_digest": dest_digest,
        "composite_snapshot_digest": composite_snapshot_digest,
        "approved_file_count": len(dest_inventory),
        "excluded_path_count": len(ledger),
        "reparse_points_detected": 0,
        "env_example_sha256": APPROVED_ENV_EXAMPLE_SHA256,
        "verification_status": "VERIFIED_IDENTICAL",
    }

    receipt_path = dest_dir / "snapshot_integrity_receipt.json"
    with receipt_path.open("w", encoding="utf-8") as f:
        json.dump(receipt_data, f, indent=2)

    manifest_data = {
        "snapshot_id": dest_dir.name,
        "created_at": receipt_data["created_at"],
        "source_root": str(src),
        "destination_root": str(dest_dir),
        "source_file_count": len(source_before),
        "destination_file_count": len(dest_inventory),
        "matched_hashes_count": matched,
        "source_before_digest": digest_before,
        "source_after_digest": digest_after,
        "destination_digest": dest_digest,
        "tree_digest": composite_snapshot_digest,
        "exclusion_count": len(ledger),
        "reparse_points_detected": 0,
        "env_example_verified": True,
        "fresh_directory_assertions": fresh_assertions,
        "exclusions": [asdict(e) for e in ledger],
    }

    manifest_path = dest_dir / "snapshot_manifest.json"
    with manifest_path.open("w", encoding="utf-8") as f:
        json.dump(manifest_data, f, indent=2)

    manifest = SnapshotManifest(
        snapshot_id=dest_dir.name,
        created_at=receipt_data["created_at"],
        source_root=str(src),
        destination_root=str(dest_dir),
        source_file_count=len(source_before),
        destination_file_count=len(dest_inventory),
        matched_hashes_count=matched,
        source_before_digest=digest_before,
        source_after_digest=digest_after,
        destination_digest=dest_digest,
        tree_digest=composite_snapshot_digest,
        exclusion_count=len(ledger),
        reparse_points_detected=0,
        env_example_verified=True,
        fresh_directory_assertions=fresh_assertions,
        exclusions=[asdict(e) for e in ledger],
    )

    return dest_dir, manifest


def verify_snapshot_receipt(
    snapshot_root: Path,
    expected_run_id: str | None = None,
    expected_source_root: Path | None = None,
) -> tuple[bool, str]:
    """Verifies the snapshot_integrity_receipt.json against actual files in snapshot.

    Enforces:
    - Strict schema validation: all 13 canonical fields must be present.
    - Snapshot identity binding: snapshot_id must match snapshot_root.name.
    - Path confinement: snapshot_root in receipt must match actual directory.
    - Composite digest recalculation: asserts composite_snapshot_digest == SHA256(before:dest:id).
    - File count parity: actual destination files must equal approved_file_count.
    - Raw byte recalculation: SHA-256 over actual files must equal destination_digest.
    - Approved .env.example hash verification.
    """
    receipt_file = snapshot_root / "snapshot_integrity_receipt.json"
    if not receipt_file.is_file():
        return False, "Receipt file missing"
    try:
        with receipt_file.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        return False, f"Failed to parse receipt: {exc}"

    # 1. Strict Schema Validation (Audit 4 & Audit 5)
    required_fields = (
        "run_id", "snapshot_id", "source_root", "snapshot_root", "created_at",
        "source_before_digest", "source_after_digest", "destination_digest",
        "composite_snapshot_digest", "approved_file_count", "excluded_path_count",
        "reparse_points_detected", "env_example_sha256", "verification_status",
    )
    for rf in required_fields:
        if rf not in data:
            return False, f"Receipt schema error: missing required field '{rf}'"

    if data.get("verification_status") != "VERIFIED_IDENTICAL":
        return False, f"Receipt status is not VERIFIED_IDENTICAL: {data.get('verification_status')}"

    # 2. Snapshot Identity & Path Binding (Audit 4 & Audit 5)
    if data.get("snapshot_id") != snapshot_root.name:
        return False, f"Receipt snapshot_id mismatch: {data.get('snapshot_id')} != {snapshot_root.name}"

    if Path(data.get("snapshot_root", "")).resolve() != snapshot_root.resolve():
        return False, f"Receipt snapshot_root mismatch: {data.get('snapshot_root')} != {snapshot_root.resolve()}"

    if expected_run_id is not None and data.get("run_id") != expected_run_id:
        return False, f"Receipt run_id mismatch: {data.get('run_id')} != {expected_run_id}"

    if expected_source_root is not None and Path(data.get("source_root", "")).resolve() != expected_source_root.resolve():
        return False, f"Receipt source_root mismatch: {data.get('source_root')} != {expected_source_root.resolve()}"

    if data.get("env_example_sha256") != APPROVED_ENV_EXAMPLE_SHA256:
        return False, "Approved .env.example hash mismatch in receipt"

    # 3. Cryptographic Composite Digest Recalculation
    expected_composite = hashlib.sha256(
        f"{data['source_before_digest']}:{data['destination_digest']}:{data['snapshot_id']}".encode()
    ).hexdigest().lower()
    if data.get("composite_snapshot_digest") != expected_composite:
        return False, f"Composite digest mismatch: {data.get('composite_snapshot_digest')} != {expected_composite}"

    # 4. Actual File Traversal & Raw Byte Recalculation
    fresh_prefixes = tuple(f"{snapshot_root.as_posix().lower()}/{d.lower()}" for d in FRESH_RUNTIME_DIRS)
    dest_inv: dict[str, FileInventoryEntry] = {}
    for root, _, files in os.walk(snapshot_root, followlinks=False):
        root_posix = Path(root).as_posix().lower()
        if any(root_posix == pfx or root_posix.startswith(f"{pfx}/") for pfx in fresh_prefixes):
            continue
        for f in files:
            if f in ("snapshot_manifest.json", "snapshot_integrity_receipt.json"):
                continue
            df = Path(root) / f
            rel = df.relative_to(snapshot_root).as_posix()
            dest_inv[rel] = FileInventoryEntry(
                rel_path=rel,
                size_bytes=df.stat().st_size,
                sha256=compute_file_sha256(df),
                mtime=df.stat().st_mtime,
            )

    curr_digest = compute_inventory_digest(dest_inv)
    if curr_digest != data.get("destination_digest"):
        return False, f"Destination digest tampered: {curr_digest} != {data.get('destination_digest')}"

    if len(dest_inv) != data.get("approved_file_count"):
        return False, f"Approved file count mismatch: {len(dest_inv)} != {data.get('approved_file_count')}"

    return True, "Snapshot receipt verified identical"


# ---------------------------------------------------------------------------
# Conditional Snapshot Teardown & Quarantine State Machine (BLK-20)
# ---------------------------------------------------------------------------
def cleanup_snapshot(snapshot_root: Path, force: bool = False) -> None:
    """Tears down snapshot directory, clearing read-only attributes."""
    if not snapshot_root.exists():
        return

    def _remove_readonly(func: Any, path: str, _: Any) -> None:
        try:
            os.chmod(path, stat.S_IWRITE)
            func(path)
        except OSError:
            pass

    for _ in range(3):
        try:
            shutil.rmtree(str(snapshot_root), onexc=_remove_readonly)
            break
        except OSError:
            time.sleep(0.1)


def evaluate_cleanup_and_teardown(
    snapshot_root: Path,
    result: SupervisorResult,
    holder: JobHandleHolder,
    drainers: list[threading.Thread],
) -> str:
    """Evaluates cleanup eligibility; tears down on success or quarantines on failure."""
    # Production Call Site: Poll tracker for eventual completions before evaluation
    RetainedPendingIOTracker.poll_and_drain_eventual_completions()

    active_procs = holder.get_active_process_count()
    drainers_alive = any(t.is_alive() for t in drainers)
    active_unresolved = len(RetainedPendingIOTracker.get_active_unresolved())

    # Positive verification: active_procs must be integer 0, never None; pending I/O must be 0
    is_eligible = (
        result.status == "COMPLETED"
        and result.exit_code == 0
        and active_procs is not None
        and active_procs == 0
        and not drainers_alive
        and not result.drainer_stalled
        and active_unresolved == 0
    )

    if is_eligible:
        cleanup_snapshot(snapshot_root)
        result.cleanup_action = "CLEANUP_PERMITTED_AND_EXECUTED"
        return result.cleanup_action

    # Quarantine Failure Path: Preserve Snapshot
    quarantine_name = f"{snapshot_root.name}_QUARANTINED"
    quarantine_dir = snapshot_root.parent / quarantine_name

    try:
        if snapshot_root.exists() and not quarantine_dir.exists():
            snapshot_root.rename(quarantine_dir)
            quarantine_path = quarantine_dir
        else:
            quarantine_path = snapshot_root
    except OSError:
        quarantine_path = snapshot_root

    receipt_file = quarantine_path / "quarantine_receipt.json"
    try:
        with receipt_file.open("w", encoding="utf-8") as f:
            json.dump(
                {
                    "quarantine_timestamp": time.time(),
                    "status": result.status,
                    "exit_code": result.exit_code,
                    "active_processes_at_cleanup": active_procs,
                    "drainer_stalled": drainers_alive or result.drainer_stalled,
                    "active_unresolved_io": active_unresolved,
                    "duration_sec": result.duration_sec,
                    "diagnostics": result.diagnostics,
                },
                f,
                indent=2,
            )
    except OSError:
        pass

    result.cleanup_action = f"QUARANTINED_AT_{quarantine_path}"
    return result.cleanup_action


# ---------------------------------------------------------------------------
# Single-Use Execution Permit Generator (BLK-21)
# ---------------------------------------------------------------------------
class SupervisorUnhealthyError(RuntimeError):
    """Raised when supervisor attempts to issue permits while in an unhealthy or compromised state."""


SUPERVISOR_HEALTH_COMPROMISED: bool = False
_WORKER_EXE_HASH_CACHE: str | None = None


def get_worker_exe_hash() -> str:
    """Retrieves cached SHA-256 digest of the pinned CPython worker executable."""
    global _WORKER_EXE_HASH_CACHE
    if _WORKER_EXE_HASH_CACHE is None:
        if PYTHON_EXE.is_file():
            _WORKER_EXE_HASH_CACHE = compute_file_sha256(PYTHON_EXE)
        else:
            _WORKER_EXE_HASH_CACHE = "0" * 64
    return _WORKER_EXE_HASH_CACHE


def check_supervisor_health_for_issuance() -> None:
    """Verifies that the supervisor has zero unresolved kernel I/O, no failed handle closures, and is healthy."""
    if SUPERVISOR_HEALTH_COMPROMISED:
        raise SupervisorUnhealthyError("Supervisor instance is marked unhealthy or compromised")

    # 1. Check unresolved kernel I/O
    active_unresolved = RetainedPendingIOTracker.get_active_unresolved()
    if active_unresolved:
        raise SupervisorUnhealthyError(
            f"Cannot issue permit: {len(active_unresolved)} unresolved kernel I/O operations pending"
        )

    # 2. Check failed handle closures (CLOSE_FAILED)
    failed_handles = [
        e for e in RetainedPendingIOTracker.get_all_entries()
        if e.event_close_state == "CLOSE_FAILED" or e.pipe_close_state == "CLOSE_FAILED"
    ]
    if failed_handles:
        raise SupervisorUnhealthyError(
            f"Cannot issue permit: {len(failed_handles)} unretired failed handles (CLOSE_FAILED) exist"
        )


def issue_execution_permit(
    snapshot_root: Path,
    profile: ProfileType,
    target_nodes: list[str] | None = None,
    tree_digest: str | None = None,
    authorized_execution_vector: list[str] | None = None,
    authorized_pytest_args: list[str] | None = None,
    ttl_sec: float = 300.0,
    run_id: str | None = None,
    permit_id: str | None = None,
    _skip_health_check: bool = False,
) -> tuple[Path, bytes]:
    """Issues a cryptographically signed, scope-bound, single-use execution permit.

    Enforces:
    - Supervisor health validation prior to issuance.
    - Exact Option A profile-specific canonical node inventory and SHA-256 digest.
    - Full rejection of broad test-file paths in favor of exact pytest node IDs.
    - Snapshot root and composite tree digest binding.
    - Worker executable path, independent binary SHA-256 hash, and CPython version binding.
    - Exact ordered execution argument vector binding.
    - Expiration metadata with wall-clock Unix timestamps and maximum TTL bounds.
    - Cryptographic HMAC-SHA256 signature using an ephemeral 32-byte secret.
    - Atomic file creation via O_CREAT | O_EXCL to prevent duplicate concurrent supervisor issuance.
    """
    if not _skip_health_check:
        check_supervisor_health_for_issuance()

    # 1. Derive or validate canonical node inventory for profile
    dispatcher = ExecutionProfileDispatcher()
    canonical_profile_nodes = dispatcher.get_nodes_for_profile(profile)

    if target_nodes is None:
        target_nodes = list(canonical_profile_nodes)
    else:
        # Validate that all supplied nodes are exact pytest node IDs (containing '::')
        for node in target_nodes:
            if "::" not in node:
                raise ValueError(
                    f"Target node '{node}' is a broad file path; exact canonical pytest node ID required ('::' required)"
                )
        # Validate unique nodes (no duplicates)
        if len(set(target_nodes)) != len(target_nodes):
            raise ValueError("Duplicate test node IDs detected in target inventory")

    node_count = len(target_nodes)
    node_inv_hash = hashlib.sha256("\n".join(sorted(target_nodes)).encode("utf-8")).hexdigest()

    # 2. Timing and expiration constraints
    MAX_PERMIT_TTL = 3600.0
    if ttl_sec <= 0.0 or ttl_sec > MAX_PERMIT_TTL:
        raise ValueError(f"Permit TTL {ttl_sec}s invalid; must be between 0.0s and {MAX_PERMIT_TTL}s")

    permit_id = permit_id or str(uuid.uuid4())
    receipt_path = snapshot_root / "snapshot_integrity_receipt.json"
    if receipt_path.is_file():
        try:
            with receipt_path.open("r", encoding="utf-8") as f:
                r_data = json.load(f)
            if run_id is None:
                run_id = r_data.get("run_id")
            else:
                r_data["run_id"] = run_id
                with receipt_path.open("w", encoding="utf-8") as f:
                    json.dump(r_data, f, indent=2)
            if tree_digest is None:
                tree_digest = r_data.get("composite_snapshot_digest")
        except (OSError, json.JSONDecodeError):
            pass
    run_id = run_id or str(uuid.uuid4())
    issued_at = time.time()
    expires_at = issued_at + ttl_sec
    ephemeral_secret = secrets.token_bytes(32)

    if authorized_pytest_args is None:
        authorized_pytest_args = ["-v", "--strict-markers"]

    if authorized_execution_vector is None:
        authorized_execution_vector = ["-v", "--strict-markers"] + list(target_nodes)
    else:
        extracted_nodes = [arg for arg in authorized_execution_vector if "::" in arg]
        if extracted_nodes and extracted_nodes != target_nodes:
            raise ValueError(
                f"Execution vector nodes {extracted_nodes} do not match target_nodes {target_nodes}"
            )

    # Workstream D: Mutual exclusion between candidate test nodes and synthetic failure flags
    SYNTHETIC_FAILURE_FLAGS = frozenset({
        "--synthetic-fail-unlink",
        "--synthetic-fail-consumed-write",
        "--synthetic-timeout",
        "--synthetic-held-writer",
        "--synthetic-descendant",
        "--synthetic-ack-wrong-token",
        "--synthetic-ack-wrong-hash",
        "--synthetic-ack-skip",
        "--synthetic-ack-delay",
        "--synthetic-ack-wrong-pid",
        "--synthetic-ack-descendant-impersonation",
        "--synthetic-ack-duplicate",
    })
    has_candidate_nodes = any("::" in arg for arg in authorized_execution_vector)
    if has_candidate_nodes:
        failure_flags = [arg for arg in authorized_execution_vector if arg in SYNTHETIC_FAILURE_FLAGS]
        if failure_flags:
            raise ValueError(
                f"Synthetic failure flags {failure_flags} are strictly prohibited when targeting candidate test nodes."
            )

    if tree_digest is None:
        tree_digest = "0" * 64

    worker_exe_path = str(PYTHON_EXE.resolve())
    worker_exe_hash = get_worker_exe_hash()
    worker_version = f"{sys.version_info[0]}.{sys.version_info[1]}.{sys.version_info[2]}"

    payload = {
        "schema_version": 2,
        "permit_id": permit_id,
        "run_id": run_id,
        "scope": "Option A — 507 nodes",
        "profile": profile.value,
        "node_count": node_count,
        "node_inventory_hash": node_inv_hash,
        "target_nodes": target_nodes,
        "snapshot_root": str(snapshot_root.resolve()),
        "tree_digest": tree_digest,
        "worker_exe_path": worker_exe_path,
        "worker_exe_hash": worker_exe_hash,
        "worker_version": worker_version,
        "authorized_pytest_args": authorized_pytest_args,
        "authorized_execution_vector": authorized_execution_vector,
        "issued_at": issued_at,
        "expires_at": expires_at,
        "ttl_sec": ttl_sec,
    }

    # HMAC Signature over JSON payload (excluding hmac_signature)
    canonical_bytes = json.dumps(payload, sort_keys=True).encode("utf-8")
    sig = hmac.new(ephemeral_secret, canonical_bytes, hashlib.sha256).hexdigest()
    payload["hmac_signature"] = sig

    permit_file = snapshot_root / ".execution_permit.json"
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    fd = os.open(str(permit_file), flags)
    with open(fd, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)

    return permit_file, ephemeral_secret


# ---------------------------------------------------------------------------
# Worker Environment & Supervised Worker Launch (BLK-22)
# ---------------------------------------------------------------------------
def build_clean_worker_env(snapshot_root: Path) -> dict[str, str]:
    """Constructs the verified 12-variable minimal deterministic environment."""
    sys_root = os.environ.get("SystemRoot", r"C:\Windows")
    system32 = os.path.join(sys_root, "System32")
    wbem = os.path.join(system32, "Wbem")
    ps_dir = os.path.join(system32, "WindowsPowerShell", "v1.0")
    minimal_path = f"{system32};{sys_root};{wbem};{ps_dir}"

    snap_temp = str(snapshot_root / "temp")

    return {
        "SystemRoot": sys_root,
        "PATH": minimal_path,
        "PATHEXT": os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD;.VBS;.VBE;.JS;.JSE;.WSF;.WSH;.MSC;.CPL"),
        "COMSPEC": os.path.join(system32, "cmd.exe"),
        "TEMP": snap_temp,
        "TMP": snap_temp,
        "NUMBER_OF_PROCESSORS": os.environ.get("NUMBER_OF_PROCESSORS", "16"),
        "PROCESSOR_ARCHITECTURE": os.environ.get("PROCESSOR_ARCHITECTURE", "AMD64"),
        "PROCESSOR_IDENTIFIER": os.environ.get("PROCESSOR_IDENTIFIER", ""),
        "PROCESSOR_LEVEL": os.environ.get("PROCESSOR_LEVEL", "6"),
        "PROCESSOR_REVISION": os.environ.get("PROCESSOR_REVISION", ""),
        "JARVIS_TEST_MODE": "1",
    }


# ---------------------------------------------------------------------------
# Win32 Named Pipes with Overlapped Asynchronous I/O (BLK-22 Architecture)
# ---------------------------------------------------------------------------
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# Win32 Constants
PIPE_ACCESS_INBOUND = 0x00000001
FILE_FLAG_OVERLAPPED = 0x40000000
PIPE_TYPE_BYTE = 0x00000000
PIPE_READMODE_BYTE = 0x00000000
PIPE_WAIT = 0x00000000

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x00000001
OPEN_EXISTING = 3
FILE_ATTRIBUTE_NORMAL = 0x00000080
INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value

ERROR_ACCESS_DENIED = 5
ERROR_IO_INCOMPLETE = 996
ERROR_IO_PENDING = 997
ERROR_BROKEN_PIPE = 109
ERROR_PIPE_CONNECTED = 535
ERROR_OPERATION_ABORTED = 995
ERROR_INVALID_HANDLE = 6
ERROR_NOT_FOUND = 1168

WAIT_OBJECT_0 = 0x00000000
WAIT_TIMEOUT = 0x00000102
WAIT_FAILED = 0xFFFFFFFF

CREATE_SUSPENDED = 0x00000004
CREATE_NO_WINDOW = 0x08000000


class SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("nLength", wintypes.DWORD),
        ("lpSecurityDescriptor", wintypes.LPVOID),
        ("bInheritHandle", wintypes.BOOL),
    ]


class OVERLAPPED(ctypes.Structure):
    _fields_ = [
        ("Internal", ctypes.c_ulonglong),
        ("InternalHigh", ctypes.c_ulonglong),
        ("Offset", wintypes.DWORD),
        ("OffsetHigh", wintypes.DWORD),
        ("hEvent", wintypes.HANDLE),
    ]


# Win32 Function Signatures
_kernel32.CreateNamedPipeW.restype = wintypes.HANDLE
_kernel32.CreateNamedPipeW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
    wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(SECURITY_ATTRIBUTES)
]

_kernel32.CreateFileW.restype = wintypes.HANDLE
_kernel32.CreateFileW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(SECURITY_ATTRIBUTES),
    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE
]

_kernel32.ConnectNamedPipe.restype = wintypes.BOOL
_kernel32.ConnectNamedPipe.argtypes = [wintypes.HANDLE, ctypes.POINTER(OVERLAPPED)]

_kernel32.ReadFile.restype = wintypes.BOOL
_kernel32.ReadFile.argtypes = [
    wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(OVERLAPPED)
]

_kernel32.WriteFile.restype = wintypes.BOOL
_kernel32.WriteFile.argtypes = [
    wintypes.HANDLE, wintypes.LPCVOID, wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(OVERLAPPED)
]

kernel32 = _kernel32

_kernel32.GetOverlappedResult.restype = wintypes.BOOL
_kernel32.GetOverlappedResult.argtypes = [
    wintypes.HANDLE, ctypes.POINTER(OVERLAPPED),
    ctypes.POINTER(wintypes.DWORD), wintypes.BOOL
]

_kernel32.CancelIoEx.restype = wintypes.BOOL
_kernel32.CancelIoEx.argtypes = [wintypes.HANDLE, ctypes.POINTER(OVERLAPPED)]

_kernel32.CreateEventW.restype = wintypes.HANDLE
_kernel32.CreateEventW.argtypes = [ctypes.POINTER(SECURITY_ATTRIBUTES), wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]

_kernel32.SetEvent.restype = wintypes.BOOL
_kernel32.SetEvent.argtypes = [wintypes.HANDLE]

_kernel32.ResetEvent.restype = wintypes.BOOL
_kernel32.ResetEvent.argtypes = [wintypes.HANDLE]

_kernel32.CloseHandle.restype = wintypes.BOOL
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

_kernel32.WaitForMultipleObjects.restype = wintypes.DWORD
_kernel32.WaitForMultipleObjects.argtypes = [
    wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE), wintypes.BOOL, wintypes.DWORD
]

_kernel32.WaitForSingleObject.restype = wintypes.DWORD
_kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]


def _is_invalid_handle(h: Any) -> bool:
    """Safely checks if a Win32 handle is NULL, INVALID_HANDLE_VALUE, or closed."""
    if h is None:
        return True
    val = getattr(h, "value", h)
    return val in (INVALID_HANDLE_VALUE, None, 0, -1)


# ---------------------------------------------------------------------------
# Unresolved Pending I/O Resource Retention & Tracker (Phase 1H State Machine)
# ---------------------------------------------------------------------------
# Per-handle ownership states:
#   OWNED:          Valid Win32 handle actively owned by this entry.
#   CLOSING:        Closure operation currently in flight under entry lock.
#   CLOSED:         CloseHandle succeeded; stored handle set to INVALID_HANDLE_VALUE.
#   CLOSE_FAILED:   CloseHandle failed; original handle and error preserved. Terminal.
#   NEVER_ACQUIRED: Explicitly recorded that handle was never acquired. Accounted.
# ---------------------------------------------------------------------------

@dataclass
class RetainedPendingIO:
    """Retains strong references to Win32 and Python kernel I/O structures while an operation is pending."""
    stream_name: str
    ov: OVERLAPPED
    buf: ctypes.Array
    h_event: wintypes.HANDLE
    h_pipe: wintypes.HANDLE
    original_h_event: Any = None
    original_h_pipe: Any = None
    event_close_state: str = "OWNED"  # "OWNED", "CLOSING", "CLOSED", "CLOSE_FAILED", "NEVER_ACQUIRED"
    pipe_close_state: str = "OWNED"   # "OWNED", "CLOSING", "CLOSED", "CLOSE_FAILED", "NEVER_ACQUIRED"
    event_close_error: int = 0
    pipe_close_error: int = 0
    event_close_call_count: int = 0
    pipe_close_call_count: int = 0
    registered_at: float = field(default_factory=time.time)
    completed: bool = False
    completion_status: str | None = None
    bytes_transferred: int = 0
    handles_closed: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        if self.original_h_event is None:
            self.original_h_event = getattr(self.h_event, "value", self.h_event)
        if self.original_h_pipe is None:
            self.original_h_pipe = getattr(self.h_pipe, "value", self.h_pipe)
        if self.event_close_state == "NEVER_ACQUIRED":
            self.h_event = wintypes.HANDLE(INVALID_HANDLE_VALUE)
        if self.pipe_close_state == "NEVER_ACQUIRED":
            self.h_pipe = wintypes.HANDLE(INVALID_HANDLE_VALUE)


RETAINED_PENDING_IO_REGISTRY: list[RetainedPendingIO] = []
_RETAINED_IO_LOCK = threading.Lock()


class RetainedPendingIOTracker:
    """Ensures zero memory deallocation or handle closure while kernel I/O is unresolved."""

    @classmethod
    def clear_for_test(cls) -> None:
        """Resets registry state between isolated test scenarios, retiring any completed entries."""
        with _RETAINED_IO_LOCK:
            for entry in RETAINED_PENDING_IO_REGISTRY:
                cls._close_entry_handles(entry)
            RETAINED_PENDING_IO_REGISTRY.clear()

    @classmethod
    def register(
        cls,
        stream_name: str,
        ov: OVERLAPPED,
        buf: ctypes.Array,
        h_event: wintypes.HANDLE,
        h_pipe: wintypes.HANDLE,
        event_never_acquired: bool = False,
        pipe_never_acquired: bool = False,
    ) -> RetainedPendingIO:
        with _RETAINED_IO_LOCK:
            entry = RetainedPendingIO(
                stream_name=stream_name,
                ov=ov,
                buf=buf,
                h_event=h_event,
                h_pipe=h_pipe,
                event_close_state="NEVER_ACQUIRED" if event_never_acquired else "OWNED",
                pipe_close_state="NEVER_ACQUIRED" if pipe_never_acquired else "OWNED",
            )
            RETAINED_PENDING_IO_REGISTRY.append(entry)
            return entry

    @classmethod
    def get_active_unresolved(cls) -> list[RetainedPendingIO]:
        with _RETAINED_IO_LOCK:
            return [e for e in RETAINED_PENDING_IO_REGISTRY if not (e.completed and e.handles_closed)]

    @classmethod
    def get_all_entries(cls) -> list[RetainedPendingIO]:
        with _RETAINED_IO_LOCK:
            return list(RETAINED_PENDING_IO_REGISTRY)

    @classmethod
    def _close_entry_handles(
        cls,
        entry: RetainedPendingIO,
        _force_event_close_fail: bool = False,
        _force_pipe_close_fail: bool = False,
        _force_event_close_error: int = ERROR_INVALID_HANDLE,
        _force_pipe_close_error: int = ERROR_INVALID_HANDLE,
    ) -> bool:
        """Closes handles associated with an entry using strict per-handle ownership transitions.

        Serialized per-entry under entry._lock to eliminate concurrent double-close races.
        Returns True only if both handles are positively accounted for as either CLOSED or NEVER_ACQUIRED.
        Returns False if any closure operation failed or is un-accounted.
        """
        with entry._lock:
            # 1. Event Handle Ownership Transition
            if entry.event_close_state == "OWNED":
                entry.event_close_state = "CLOSING"
                entry.event_close_call_count += 1
                raw_event = entry.h_event

                if _force_event_close_fail:
                    res_event = 0
                    err_event = _force_event_close_error
                else:
                    res_event = _kernel32.CloseHandle(raw_event)
                    err_event = ctypes.get_last_error() if not res_event else 0

                if res_event != 0:
                    entry.event_close_state = "CLOSED"
                    entry.h_event = wintypes.HANDLE(INVALID_HANDLE_VALUE)
                    entry.event_close_error = 0
                else:
                    # Failed CloseHandle: retain failure state and original handle value
                    entry.event_close_state = "CLOSE_FAILED"
                    entry.event_close_error = err_event
                    # Original handle value in entry.h_event is NOT overwritten!
            elif entry.event_close_state in ("CLOSED", "NEVER_ACQUIRED"):
                # Already safely accounted; bypass redundant close
                pass
            elif entry.event_close_state == "CLOSE_FAILED":
                # Permanent failure state: never retry, never convert to ALREADY_INVALID or CLOSED
                pass

            # 2. Pipe Handle Ownership Transition
            if entry.pipe_close_state == "OWNED":
                entry.pipe_close_state = "CLOSING"
                entry.pipe_close_call_count += 1
                raw_pipe = entry.h_pipe

                if _force_pipe_close_fail:
                    res_pipe = 0
                    err_pipe = _force_pipe_close_error
                else:
                    res_pipe = _kernel32.CloseHandle(raw_pipe)
                    err_pipe = ctypes.get_last_error() if not res_pipe else 0

                if res_pipe != 0:
                    entry.pipe_close_state = "CLOSED"
                    entry.h_pipe = wintypes.HANDLE(INVALID_HANDLE_VALUE)
                    entry.pipe_close_error = 0
                else:
                    # Failed CloseHandle: retain failure state and original handle value
                    entry.pipe_close_state = "CLOSE_FAILED"
                    entry.pipe_close_error = err_pipe
                    # Original handle value in entry.h_pipe is NOT overwritten!
            elif entry.pipe_close_state in ("CLOSED", "NEVER_ACQUIRED"):
                # Already safely accounted; bypass redundant close
                pass
            elif entry.pipe_close_state == "CLOSE_FAILED":
                # Permanent failure state: never retry, never convert to ALREADY_INVALID or CLOSED
                pass

            both_accounted = (
                entry.event_close_state in ("CLOSED", "NEVER_ACQUIRED")
                and entry.pipe_close_state in ("CLOSED", "NEVER_ACQUIRED")
            )
            if both_accounted:
                entry.handles_closed = True
                return True
            else:
                entry.handles_closed = False
                if entry.event_close_state == "CLOSE_FAILED" or entry.pipe_close_state == "CLOSE_FAILED":
                    entry.completion_status = "HANDLE_CLOSE_FAILED"
                return False

    @classmethod
    def poll_and_drain_eventual_completions(
        cls,
        timeout: float = 0.0,
        _force_polling_exception: bool = False,
        _force_unexpected_completion_error: int = 0,
    ) -> list[RetainedPendingIO]:
        """Polls pending operations using GetOverlappedResult(..., bWait=False).

        Leaves operations in ERROR_IO_INCOMPLETE (996) registered and retained.
        Retires completed operations and safely closes handles exactly once.
        Fails closed on any unexpected exception or polling error.
        """
        completed_now: list[RetainedPendingIO] = []
        with _RETAINED_IO_LOCK:
            if _force_polling_exception:
                raise RuntimeError("Controlled synthetic tracker polling exception")

            for entry in RETAINED_PENDING_IO_REGISTRY:
                if entry.completed and entry.handles_closed:
                    continue

                if entry.completion_status in ("HANDLE_CLOSE_FAILED", "POLLING_EXCEPTION_CAPTURED"):
                    # Closure already failed or exception captured: preserve failed state fail-closed
                    continue

                if entry.event_close_state == "CLOSE_FAILED" or entry.pipe_close_state == "CLOSE_FAILED":
                    # Failed handle closure: preserve fail-closed
                    continue

                if entry.completion_status and entry.completion_status.startswith("UNEXPECTED_POLLING_ERROR"):
                    # Previous polling suffered unexpected error: preserve fail-closed
                    continue

                if (
                    (_is_invalid_handle(entry.h_pipe) or _is_invalid_handle(entry.h_event))
                    and entry.event_close_state != "NEVER_ACQUIRED"
                    and entry.pipe_close_state != "NEVER_ACQUIRED"
                ):
                    # Invalid handle before completion established:
                    # Do NOT call GetOverlappedResult on invalid handle!
                        # Fail closed: mark status and preserve in unresolved registry
                        if entry.completion_status != "INVALID_HANDLE_POLL_REJECTED":
                            entry.completion_status = "INVALID_HANDLE_POLL_REJECTED"
                        continue

                if _force_unexpected_completion_error > 0:
                    entry.completion_status = f"UNEXPECTED_POLLING_ERROR_{_force_unexpected_completion_error}"
                    continue

                bytes_ret = wintypes.DWORD(0)
                res = _kernel32.GetOverlappedResult(
                    entry.h_pipe,
                    ctypes.byref(entry.ov),
                    ctypes.byref(bytes_ret),
                    False,
                )
                if res:
                    entry.bytes_transferred = bytes_ret.value
                    closed_ok = cls._close_entry_handles(entry)
                    if closed_ok:
                        entry.completed = True
                        entry.completion_status = "COMPLETED"
                        completed_now.append(entry)
                    else:
                        entry.completed = False
                        entry.completion_status = "HANDLE_CLOSE_FAILED"
                else:
                    err = ctypes.get_last_error()
                    if err in (ERROR_IO_INCOMPLETE, ERROR_IO_PENDING):
                        # Operation is still in flight in kernel. Retain resources!
                        entry.completion_status = "PENDING_IN_KERNEL"
                        continue
                    elif err in (ERROR_OPERATION_ABORTED, ERROR_BROKEN_PIPE):
                        status_str = "CANCELLED" if err == ERROR_OPERATION_ABORTED else "BROKEN_PIPE"
                        closed_ok = cls._close_entry_handles(entry)
                        if closed_ok:
                            entry.completed = True
                            entry.completion_status = status_str
                            completed_now.append(entry)
                        else:
                            entry.completed = False
                            entry.completion_status = "HANDLE_CLOSE_FAILED"
                    elif err == ERROR_INVALID_HANDLE:
                        entry.completion_status = "INVALID_HANDLE_AT_KERNEL"
                        # Fail closed: retain entry, do not retire
                    else:
                        # Unexpected GetOverlappedResult error: fail closed, retain entry
                        entry.completion_status = f"UNEXPECTED_POLLING_ERROR_{err}"
        return completed_now


class OverlappedPipePair:
    """Inbound overlapped server pipe paired with an inheritable synchronous client pipe."""

    def __init__(self, name_prefix: str = "jarvis_worker", connect_client_first: bool = False):
        self.pipe_name = f"\\\\.\\pipe\\{name_prefix}_{uuid.uuid4().hex}"
        self.h_server: wintypes.HANDLE = wintypes.HANDLE(INVALID_HANDLE_VALUE)
        self.h_client: wintypes.HANDLE = wintypes.HANDLE(INVALID_HANDLE_VALUE)
        self.client_fd: int | None = None
        self.connection_status: str = "UNINITIALIZED"

        # 1. Create Server Pipe (Inbound, Overlapped, Non-inheritable)
        self.h_server = _kernel32.CreateNamedPipeW(
            self.pipe_name,
            PIPE_ACCESS_INBOUND | FILE_FLAG_OVERLAPPED,
            PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT,
            1,
            65536,
            65536,
            5000,
            None,
        )
        if self.h_server == INVALID_HANDLE_VALUE:
            raise OSError(f"CreateNamedPipeW failed: {ctypes.get_last_error()}")

        sa = SECURITY_ATTRIBUTES()
        sa.nLength = ctypes.sizeof(SECURITY_ATTRIBUTES)
        sa.lpSecurityDescriptor = None
        sa.bInheritHandle = True

        if connect_client_first:
            # Client connects before ConnectNamedPipe is invoked (race condition probe)
            self.h_client = _kernel32.CreateFileW(
                self.pipe_name,
                GENERIC_WRITE,
                0,
                ctypes.byref(sa),
                OPEN_EXISTING,
                FILE_ATTRIBUTE_NORMAL,
                None,
            )
            if self.h_client == INVALID_HANDLE_VALUE:
                err = ctypes.get_last_error()
                self.close_server()
                raise OSError(f"CreateFileW failed: {err}")

            conn_ov = OVERLAPPED()
            conn_ov.hEvent = _kernel32.CreateEventW(None, True, False, None)
            conn_res = _kernel32.ConnectNamedPipe(self.h_server, ctypes.byref(conn_ov))
            conn_err = ctypes.get_last_error() if not conn_res else 0

            if not conn_res and conn_err == ERROR_PIPE_CONNECTED:
                # Microsoft specification: client already connected, signal event and proceed
                # Do NOT call GetOverlappedResult
                _kernel32.SetEvent(conn_ov.hEvent)
                _kernel32.CloseHandle(conn_ov.hEvent)
                self.connection_status = "CONNECTED_RACE_CLIENT_FIRST"
                return
            else:
                _kernel32.CloseHandle(conn_ov.hEvent)
                self.close_client_in_supervisor()
                self.close_server()
                raise OSError(f"ConnectNamedPipe unexpected result on client-first connect: {conn_err}")

        # Canonical sequence: initiate overlapped server listening
        conn_ov = OVERLAPPED()
        conn_ov.hEvent = _kernel32.CreateEventW(None, True, False, None)
        if not conn_ov.hEvent:
            err = ctypes.get_last_error()
            self.close_server()
            raise OSError(f"CreateEventW failed: {err}")

        conn_res = _kernel32.ConnectNamedPipe(self.h_server, ctypes.byref(conn_ov))
        conn_err = ctypes.get_last_error() if not conn_res else 0

        if not conn_res and conn_err == ERROR_PIPE_CONNECTED:
            # Client connected immediately: signal event, do NOT call GetOverlappedResult
            _kernel32.SetEvent(conn_ov.hEvent)
            _kernel32.CloseHandle(conn_ov.hEvent)
            self.connection_status = "CONNECTED_RACE_CLIENT_FIRST"
            return
        elif not conn_res and conn_err == ERROR_IO_PENDING:
            # Genuine asynchronous pending connection
            pass
        else:
            _kernel32.CloseHandle(conn_ov.hEvent)
            self.close_server()
            raise OSError(f"ConnectNamedPipe failed with unexpected error: {conn_err}")

        # Open Client End (Synchronous write, Inheritable)
        self.h_client = _kernel32.CreateFileW(
            self.pipe_name,
            GENERIC_WRITE,
            0,
            ctypes.byref(sa),
            OPEN_EXISTING,
            FILE_ATTRIBUTE_NORMAL,
            None,
        )
        if self.h_client == INVALID_HANDLE_VALUE:
            err = ctypes.get_last_error()
            _kernel32.CancelIoEx(self.h_server, ctypes.byref(conn_ov))
            _kernel32.CloseHandle(conn_ov.hEvent)
            self.close_server()
            raise OSError(f"CreateFileW failed: {err}")

        # Connection was pending: await verified completion via GetOverlappedResult
        bytes_transferred = wintypes.DWORD(0)
        res = _kernel32.GetOverlappedResult(
            self.h_server, ctypes.byref(conn_ov), ctypes.byref(bytes_transferred), True
        )
        if not res:
            err = ctypes.get_last_error()
            _kernel32.CloseHandle(self.h_client)
            _kernel32.CloseHandle(conn_ov.hEvent)
            self.close_server()
            raise OSError(f"GetOverlappedResult failed on pipe connection: {err}")

        _kernel32.CloseHandle(conn_ov.hEvent)
        self.connection_status = "CONNECTED_OVERLAPPED_VERIFIED"

    def get_client_fd(self) -> int:
        if self.client_fd is None:
            self.client_fd = msvcrt.open_osfhandle(int(self.h_client), os.O_WRONLY)
        return self.client_fd

    def close_client_in_supervisor(self) -> None:
        """Closes client handle/fd in supervisor so only child process tree holds it."""
        if self.client_fd is not None:
            try:
                os.close(self.client_fd)
            except OSError:
                pass
            self.client_fd = None
            self.h_client = wintypes.HANDLE(INVALID_HANDLE_VALUE)
        elif self.h_client != INVALID_HANDLE_VALUE:
            _kernel32.CloseHandle(self.h_client)
            self.h_client = wintypes.HANDLE(INVALID_HANDLE_VALUE)

    def close_server(self, force: bool = False) -> None:
        if not _is_invalid_handle(self.h_server):
            # Check if server handle is currently referenced by unresolved pending I/O
            if not force and any(
                getattr(e.h_pipe, "value", e.h_pipe) == getattr(self.h_server, "value", self.h_server)
                for e in RetainedPendingIOTracker.get_active_unresolved()
            ):
                # Retain server handle in tracker; do not close yet
                return
            _kernel32.CloseHandle(self.h_server)
            self.h_server = wintypes.HANDLE(INVALID_HANDLE_VALUE)


@dataclass
class DrainerState:
    status: str = "INITIALIZED"  # "COMPLETED", "COMPLETED_RACE_WITH_CANCEL", "CANCELLED", "BUDGET_EXHAUSTED", "DEADLINE_EXHAUSTED", "INVALID_HANDLE", "API_ERROR", "STALLED", "UNRESOLVED_CANCELLATION", "UNEXPECTED_WAIT_FAILURE", "UNEXPECTED_COMPLETION_FAILURE"
    bytes_read: int = 0
    chunks: list[str] = field(default_factory=list)
    error_code: int = 0
    diagnostics: list[str] = field(default_factory=list)
    cancel_requested: bool = False
    cancel_result: bool | None = None
    cancel_error: int = 0


class OverlappedPipeDrainer:
    """Overlapped reader thread with kernel-level CancelIoEx cancellation and byte/deadline bounds."""

    def __init__(
        self,
        h_pipe: wintypes.HANDLE,
        stream_name: str = "stdout",
        max_bytes: int = 10 * 1024 * 1024,  # 10 MB budget
        deadline_sec: float | None = None,
        # Controlled test seams for synthetic verification:
        _force_wait_failure: bool = False,
        _force_completion_failure: bool = False,
        _force_unresolved_timeout: bool = False,
        _cancellation_timeout_ms: int = 500,
    ):
        self.h_pipe = h_pipe
        self.stream_name = stream_name
        self.max_bytes = max_bytes
        self.deadline_sec = deadline_sec
        self._force_wait_failure = _force_wait_failure
        self._force_completion_failure = _force_completion_failure
        self._force_unresolved_timeout = _force_unresolved_timeout
        self._cancellation_timeout_ms = _cancellation_timeout_ms

        self.h_stop_event = _kernel32.CreateEventW(None, True, False, None)
        self.state = DrainerState()
        self.thread: threading.Thread | None = None

        # Persistent resource attributes ensuring memory lifetime during pending I/O
        self._ov: OVERLAPPED | None = None
        self._buf: ctypes.Array | None = None
        self._h_ov_event: wintypes.HANDLE = wintypes.HANDLE(INVALID_HANDLE_VALUE)
        self._io_pending: bool = False
        self.retained_io: RetainedPendingIO | None = None

    def has_unresolved_pending_io(self) -> bool:
        return self._io_pending

    def start(self) -> None:
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        """Signals cancellation to the reader thread."""
        _kernel32.SetEvent(self.h_stop_event)

    def join(self, timeout: float = 0.5) -> bool:
        if self.thread:
            self.thread.join(timeout=timeout)
            is_alive = self.thread.is_alive()
            if is_alive:
                self.state.status = "STALLED"
                self.state.diagnostics.append(f"Drainer thread for {self.stream_name} failed to join within {timeout}s")
            return not is_alive
        return True

    def close(self) -> None:
        if not _is_invalid_handle(self.h_stop_event):
            _kernel32.CloseHandle(self.h_stop_event)
            self.h_stop_event = wintypes.HANDLE(INVALID_HANDLE_VALUE)
        if not self._io_pending and not _is_invalid_handle(self._h_ov_event):
            _kernel32.CloseHandle(self._h_ov_event)
            self._h_ov_event = wintypes.HANDLE(INVALID_HANDLE_VALUE)

    def _run(self) -> None:
        if self.h_pipe == INVALID_HANDLE_VALUE:
            self.state.status = "INVALID_HANDLE"
            self.state.error_code = ERROR_INVALID_HANDLE
            self.state.diagnostics.append(f"Drainer {self.stream_name} initialized with INVALID_HANDLE_VALUE")
            return

        # Retain references on self so memory is guaranteed alive even if thread exits
        self._ov = OVERLAPPED()
        self._ov.hEvent = _kernel32.CreateEventW(None, True, False, None)
        self._h_ov_event = self._ov.hEvent
        self._buf = ctypes.create_string_buffer(65536)
        bytes_transferred = wintypes.DWORD(0)

        handle_arr = (wintypes.HANDLE * 2)(self._h_ov_event, self.h_stop_event)
        start_time = time.monotonic()
        self._io_pending = False

        try:
            while True:
                # Check byte budget
                if self.state.bytes_read >= self.max_bytes:
                    self.state.status = "BUDGET_EXHAUSTED"
                    self.state.diagnostics.append(f"Byte budget of {self.max_bytes} bytes exhausted")
                    break

                # Check deadline
                if self.deadline_sec and (time.monotonic() - start_time) >= self.deadline_sec:
                    self.state.status = "DEADLINE_EXHAUSTED"
                    self.state.diagnostics.append(f"Deadline of {self.deadline_sec}s exhausted")
                    break

                _kernel32.ResetEvent(self._h_ov_event)
                read_budget = min(65536, self.max_bytes - self.state.bytes_read)
                success = _kernel32.ReadFile(
                    self.h_pipe,
                    self._buf,
                    read_budget,
                    ctypes.byref(bytes_transferred),
                    ctypes.byref(self._ov),
                )

                if not success:
                    err = ctypes.get_last_error()
                    if err == ERROR_IO_PENDING:
                        self._io_pending = True

                        if self._force_wait_failure:
                            self.state.status = "UNEXPECTED_WAIT_FAILURE"
                            self.state.error_code = 1  # Generic error
                            break

                        wait_res = _kernel32.WaitForMultipleObjects(2, handle_arr, False, 50)
                        if wait_res == WAIT_OBJECT_0:
                            self._io_pending = False
                            if self._force_completion_failure:
                                self.state.status = "UNEXPECTED_COMPLETION_FAILURE"
                                self.state.error_code = 31  # ERROR_GEN_FAILURE
                                break

                            res = _kernel32.GetOverlappedResult(
                                self.h_pipe, ctypes.byref(self._ov), ctypes.byref(bytes_transferred), False
                            )
                            if res:
                                n = bytes_transferred.value
                                if n > 0:
                                    self.state.bytes_read += n
                                    text = self._buf.raw[:n].decode("utf-8", "replace").replace("\r\n", "\n")
                                    self.state.chunks.append(text)
                            else:
                                get_err = ctypes.get_last_error()
                                if get_err == ERROR_BROKEN_PIPE:
                                    self.state.status = "COMPLETED"
                                    break
                                elif get_err == ERROR_OPERATION_ABORTED:
                                    self.state.status = "CANCELLED"
                                    break
                                else:
                                    self.state.status = "API_ERROR"
                                    self.state.error_code = get_err
                                    self.state.diagnostics.append(f"GetOverlappedResult failed with {get_err}")
                                    break
                        elif wait_res == WAIT_OBJECT_0 + 1:
                            # Stop signal received -> cancel outstanding overlapped read
                            self.state.cancel_requested = True
                            cancel_res = _kernel32.CancelIoEx(self.h_pipe, ctypes.byref(self._ov))
                            cancel_err = ctypes.get_last_error() if not cancel_res else 0
                            self.state.cancel_result = bool(cancel_res)
                            self.state.cancel_error = cancel_err

                            # Bounded wait for cancellation or completion
                            if self._force_unresolved_timeout:
                                wait_cancel = WAIT_TIMEOUT
                            else:
                                wait_cancel = _kernel32.WaitForSingleObject(
                                    self._h_ov_event, self._cancellation_timeout_ms
                                )

                            if wait_cancel == WAIT_TIMEOUT:
                                # Kernel operation has NOT completed.
                                # Must retain OVERLAPPED structure, buffer, event, and pipe handle!
                                self.state.status = "UNRESOLVED_CANCELLATION"
                                self.state.diagnostics.append(
                                    f"Pending I/O failed to resolve within {self._cancellation_timeout_ms}ms cancellation timeout"
                                )
                                self.retained_io = RetainedPendingIOTracker.register(
                                    self.stream_name,
                                    self._ov,
                                    self._buf,
                                    self._h_ov_event,
                                    self.h_pipe,
                                )
                                # self._io_pending remains True so handle/buffer aren't closed in finally
                                break

                            res = _kernel32.GetOverlappedResult(
                                self.h_pipe, ctypes.byref(self._ov), ctypes.byref(bytes_transferred), False
                            )
                            self._io_pending = False

                            if res:
                                # Cancellation raced with normal completion! Retain transferred bytes!
                                n = bytes_transferred.value
                                if n > 0:
                                    self.state.bytes_read += n
                                    text = self._buf.raw[:n].decode("utf-8", "replace").replace("\r\n", "\n")
                                    self.state.chunks.append(text)
                                self.state.status = "COMPLETED_RACE_WITH_CANCEL"
                                self.state.diagnostics.append(
                                    f"CancelIoEx raced with normal completion; retained {n} bytes."
                                )
                                break
                            else:
                                comp_err = ctypes.get_last_error()
                                if comp_err == ERROR_OPERATION_ABORTED:
                                    if self.state.bytes_read > 0:
                                        self.state.status = "COMPLETED_RACE_WITH_CANCEL"
                                        self.state.diagnostics.append(
                                            f"Data transferred ({self.state.bytes_read} bytes) before trailing read cancelled"
                                        )
                                    else:
                                        self.state.status = "CANCELLED"
                                        self.state.diagnostics.append(
                                            "I/O aborted via CancelIoEx (ERROR_OPERATION_ABORTED 995)"
                                        )
                                    break
                                elif comp_err == ERROR_BROKEN_PIPE:
                                    self.state.status = "COMPLETED"
                                    self.state.diagnostics.append("Pipe closed (broken pipe) during cancellation")
                                    break
                                else:
                                    self.state.status = "API_ERROR"
                                    self.state.error_code = comp_err
                                    self.state.diagnostics.append(f"GetOverlappedResult failed with {comp_err}")
                                    break
                        elif wait_res == WAIT_TIMEOUT:
                            continue
                        elif wait_res == WAIT_FAILED:
                            self.state.status = "UNEXPECTED_WAIT_FAILURE"
                            self.state.error_code = ctypes.get_last_error()
                            break
                        else:
                            self.state.status = "API_ERROR"
                            self.state.error_code = ctypes.get_last_error()
                            break
                    elif err == ERROR_BROKEN_PIPE:
                        self.state.status = "COMPLETED"
                        break
                    elif err == ERROR_INVALID_HANDLE:
                        self.state.status = "INVALID_HANDLE"
                        self.state.error_code = err
                        break
                    else:
                        self.state.status = "API_ERROR"
                        self.state.error_code = err
                        self.state.diagnostics.append(f"ReadFile failed with unexpected error {err}")
                        break
                else:
                    if self._force_completion_failure:
                        self.state.status = "UNEXPECTED_COMPLETION_FAILURE"
                        self.state.error_code = 31  # ERROR_GEN_FAILURE
                        break
                    if bytes_transferred.value > 0:
                        n = bytes_transferred.value
                        self.state.bytes_read += n
                        text = self._buf.raw[:n].decode("utf-8", "replace").replace("\r\n", "\n")
                        self.state.chunks.append(text)
        finally:
            if self._io_pending:
                # Operation is STILL PENDING in kernel:
                # DO NOT close the event handle or free buffers!
                pass
            else:
                if self._h_ov_event != INVALID_HANDLE_VALUE:
                    _kernel32.CloseHandle(self._h_ov_event)
                    self._h_ov_event = wintypes.HANDLE(INVALID_HANDLE_VALUE)
                    if self._ov:
                        self._ov.hEvent = wintypes.HANDLE(INVALID_HANDLE_VALUE)


# ---------------------------------------------------------------------------
# Win32 Process Token Inspection & Boundary Verification (BLK-18)
# ---------------------------------------------------------------------------
_advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
_user32 = ctypes.WinDLL("user32", use_last_error=True)

TOKEN_QUERY = 0x0008
TokenUser = 1
TokenSessionId = 12
TokenElevation = 20
TokenIntegrityLevel = 25
TokenIsAppContainer = 29
TokenCapabilities = 30
TokenAppContainerSid = 31

# Context A Hermetic Capability Allowlist
# Ambient sandbox capabilities required for sandbox workspace/IPC traversal
CONTEXT_A_CAPABILITY_ALLOWLIST: frozenset[str] = frozenset({
    "S-1-15-3-1024-530591407-3118584226-222893001-1659895842-537460181-133087082-1363364250-4100990745",
    "S-1-15-3-1024-132000074-4282653157-1538399343-3756252210-1772340707-3911835171-3004464742-3244162316",
})

UOI_NAME = 2

_advapi32.OpenProcessToken.restype = wintypes.BOOL
_advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]

_advapi32.GetTokenInformation.restype = wintypes.BOOL
_advapi32.GetTokenInformation.argtypes = [
    wintypes.HANDLE,
    ctypes.c_int,
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
]

_advapi32.GetSidSubAuthorityCount.restype = ctypes.POINTER(wintypes.BYTE)
_advapi32.GetSidSubAuthorityCount.argtypes = [ctypes.c_void_p]

_advapi32.GetSidSubAuthority.restype = ctypes.POINTER(wintypes.DWORD)
_advapi32.GetSidSubAuthority.argtypes = [ctypes.c_void_p, wintypes.DWORD]

_advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
_advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]

_user32.GetProcessWindowStation.restype = wintypes.HANDLE
_user32.GetProcessWindowStation.argtypes = []

_user32.GetUserObjectInformationW.restype = wintypes.BOOL
_user32.GetUserObjectInformationW.argtypes = [
    wintypes.HANDLE,
    ctypes.c_int,
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
]

_user32.GetThreadDesktop.restype = wintypes.HANDLE
_user32.GetThreadDesktop.argtypes = [wintypes.DWORD]


@dataclass(frozen=True)
class ProcessWitness:
    """Immutable evidence witness of actual operating-system child process isolation."""
    pid: int
    is_appcontainer: bool
    integrity_rid: int
    integrity_label: str
    is_elevated: bool
    session_id: int
    appcontainer_sid: str | None
    user_sid: str | None
    capabilities: tuple[str, ...]
    window_station: str | None
    desktop: str | None


def _get_user_object_name(h_obj: int | None) -> str | None:
    if not h_obj:
        return None
    needed = wintypes.DWORD(0)
    _user32.GetUserObjectInformationW(wintypes.HANDLE(h_obj), UOI_NAME, None, 0, ctypes.byref(needed))
    if needed.value == 0:
        return None
    buf = ctypes.create_unicode_buffer(needed.value // 2 + 1)
    if _user32.GetUserObjectInformationW(wintypes.HANDLE(h_obj), UOI_NAME, buf, needed.value, ctypes.byref(needed)):
        return buf.value
    return None


def query_process_witness(h_process: int, pid: int = 0, thread_id: int | None = None) -> ProcessWitness:
    """Queries child process token and window station to produce machine-readable ProcessWitness."""
    h_token = wintypes.HANDLE()
    if not _advapi32.OpenProcessToken(wintypes.HANDLE(h_process), TOKEN_QUERY, ctypes.byref(h_token)):
        last_err = ctypes.get_last_error()
        raise RuntimeError(f"OpenProcessToken failed for PID {pid} with Win32 error {last_err}")

    try:
        ret_len = wintypes.DWORD(0)

        # 1. TokenIsAppContainer
        is_ac = wintypes.DWORD(0)
        is_appcontainer = False
        if _advapi32.GetTokenInformation(h_token, TokenIsAppContainer, ctypes.byref(is_ac), ctypes.sizeof(is_ac), ctypes.byref(ret_len)):
            is_appcontainer = bool(is_ac.value != 0)

        # 2. TokenElevation
        is_elev = wintypes.DWORD(0)
        is_elevated = False
        if _advapi32.GetTokenInformation(h_token, TokenElevation, ctypes.byref(is_elev), ctypes.sizeof(is_elev), ctypes.byref(ret_len)):
            is_elevated = bool(is_elev.value != 0)

        # 3. TokenSessionId
        sess_id = wintypes.DWORD(0)
        session_id = -1
        if _advapi32.GetTokenInformation(h_token, TokenSessionId, ctypes.byref(sess_id), ctypes.sizeof(sess_id), ctypes.byref(ret_len)):
            session_id = int(sess_id.value)

        # 4. TokenIntegrityLevel
        integrity_rid = 0
        integrity_label = "Unknown"
        buf_integ = ctypes.create_string_buffer(64)
        if _advapi32.GetTokenInformation(h_token, TokenIntegrityLevel, buf_integ, 64, ctypes.byref(ret_len)):
            p_sid = ctypes.cast(buf_integ, ctypes.POINTER(ctypes.c_void_p))[0]
            cnt_ptr = _advapi32.GetSidSubAuthorityCount(p_sid)
            if cnt_ptr:
                cnt = cnt_ptr[0]
                rid_ptr = _advapi32.GetSidSubAuthority(p_sid, cnt - 1)
                if rid_ptr:
                    integrity_rid = int(rid_ptr[0])
                    if integrity_rid < 0x2000:
                        integrity_label = "Low"
                    elif integrity_rid < 0x3000:
                        integrity_label = "Medium"
                    elif integrity_rid < 0x4000:
                        integrity_label = "High"
                    else:
                        integrity_label = "System"

        # 5. TokenAppContainerSid
        appcontainer_sid: str | None = None
        if is_appcontainer:
            buf_ac = ctypes.create_string_buffer(128)
            if _advapi32.GetTokenInformation(h_token, TokenAppContainerSid, buf_ac, 128, ctypes.byref(ret_len)):
                p_ac_sid = ctypes.cast(buf_ac, ctypes.POINTER(ctypes.c_void_p))[0]
                str_sid_w = wintypes.LPWSTR()
                if _advapi32.ConvertSidToStringSidW(p_ac_sid, ctypes.byref(str_sid_w)):
                    appcontainer_sid = str(str_sid_w.value)
                    _kernel32.LocalFree(str_sid_w)

        # 6. TokenCapabilities (30)
        capabilities_list: list[str] = []
        needed_caps = wintypes.DWORD(0)
        _advapi32.GetTokenInformation(h_token, TokenCapabilities, None, 0, ctypes.byref(needed_caps))
        if needed_caps.value > 0:
            buf_caps = ctypes.create_string_buffer(needed_caps.value)
            if _advapi32.GetTokenInformation(h_token, TokenCapabilities, buf_caps, needed_caps.value, ctypes.byref(needed_caps)):
                group_count = ctypes.c_uint32.from_buffer_copy(buf_caps.raw[:4]).value
                offset = 8  # 4 bytes GroupCount + 4 bytes padding on 64-bit ABI
                for _ in range(group_count):
                    psid = ctypes.c_void_p.from_buffer_copy(buf_caps.raw[offset:offset+8]).value
                    str_sid_w = wintypes.LPWSTR()
                    if _advapi32.ConvertSidToStringSidW(psid, ctypes.byref(str_sid_w)):
                        capabilities_list.append(str(str_sid_w.value))
                        _kernel32.LocalFree(str_sid_w)
                    offset += 16  # 8 bytes PSID + 4 bytes Attributes + 4 bytes padding on 64-bit ABI

        # 7. TokenUser
        user_sid: str | None = None
        buf_user = ctypes.create_string_buffer(128)
        if _advapi32.GetTokenInformation(h_token, TokenUser, buf_user, 128, ctypes.byref(ret_len)):
            p_user_sid = ctypes.cast(buf_user, ctypes.POINTER(ctypes.c_void_p))[0]
            str_sid_w = wintypes.LPWSTR()
            if _advapi32.ConvertSidToStringSidW(p_user_sid, ctypes.byref(str_sid_w)):
                user_sid = str(str_sid_w.value)
                _kernel32.LocalFree(str_sid_w)

        # 8. Window Station & Desktop
        h_winsta = _user32.GetProcessWindowStation()
        winsta_name = _get_user_object_name(h_winsta)
        desktop_name = None
        if thread_id is not None:
            h_desk = _user32.GetThreadDesktop(thread_id)
            desktop_name = _get_user_object_name(h_desk)

        return ProcessWitness(
            pid=pid,
            is_appcontainer=is_appcontainer,
            integrity_rid=integrity_rid,
            integrity_label=integrity_label,
            is_elevated=is_elevated,
            session_id=session_id,
            appcontainer_sid=appcontainer_sid,
            user_sid=user_sid,
            capabilities=tuple(sorted(capabilities_list)),
            window_station=winsta_name,
            desktop=desktop_name,
        )
    finally:
        _kernel32.CloseHandle(h_token)


def validate_profile_boundary(
    profile: ProfileType,
    witness: ProcessWitness,
    snapshot_root: Path | None = None,
) -> tuple[bool, str]:
    """Validates observed child process token witness against requested profile invariants."""
    # 1. Non-elevation invariant across all profiles
    if witness.is_elevated:
        return False, f"Child process has elevated token (is_elevated={witness.is_elevated}); non-elevated required."

    if profile == ProfileType.CONTEXT_A:
        # Context A: Genuine AppContainer with exact Low Integrity token (RID 0x1000)
        if not witness.is_appcontainer:
            return False, f"Child token is not an AppContainer (TokenIsAppContainer={witness.is_appcontainer}); Context A requires genuine AppContainer."
        if witness.integrity_rid != 0x1000:
            return False, f"Child integrity RID {hex(witness.integrity_rid)} ({witness.integrity_label}) is not exact Low Integrity (0x1000); Context A requires exact Low Integrity."
        if not witness.appcontainer_sid or not witness.appcontainer_sid.startswith("S-1-15-2-"):
            return False, f"Child AppContainer SID '{witness.appcontainer_sid}' is missing or malformed."

        # Capability allowlist enforcement (Task 2)
        unauthorized_caps = sorted(set(witness.capabilities) - CONTEXT_A_CAPABILITY_ALLOWLIST)
        if unauthorized_caps:
            return False, f"Child process has unauthorized capabilities: {unauthorized_caps}"

        return True, "Context A AppContainer Low Integrity verified"

    elif profile in (ProfileType.CONTEXT_B1_ORDINARY, ProfileType.CONTEXT_B1_SNAPSHOT):
        # Context B1: Ordinary Non-Elevated Medium Integrity
        if witness.is_appcontainer:
            return False, f"Child token is an AppContainer (TokenIsAppContainer={witness.is_appcontainer}); Context B1 requires host Medium Integrity without AppContainer restriction."
        if witness.integrity_rid != 0x2000:
            return False, f"Child integrity RID {hex(witness.integrity_rid)} ({witness.integrity_label}) is not Medium Integrity (0x2000); Context B1 requires exact Medium Integrity."

        # Additional confinement checks for Context B1 Snapshot
        if profile == ProfileType.CONTEXT_B1_SNAPSHOT and snapshot_root is not None:
            snap_resolved = snapshot_root.resolve()
            snap_temp = (snap_resolved / "temp").resolve()
            if not snap_temp.is_dir():
                return False, f"Snapshot temporary directory missing: {snap_temp}"

        return True, f"{profile.value} Medium Integrity verified"

    elif profile == ProfileType.CONTEXT_B2_INTERACTIVE:
        # Context B2: Dedicated Disposable Desktop Station (prohibits everyday WinSta0\Default)
        if witness.window_station == "WinSta0" and witness.desktop in ("Default", None):
            return False, "Refusing execution on everyday desktop station WinSta0\\Default; Context B2 requires dedicated disposable station."
        return True, "Context B2 dedicated interactive station verified"

    return False, f"Unknown execution profile: {profile}"


class PROCESSENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_char * 260),
    ]


def _get_process_parent_pid(target_pid: int) -> int | None:
    """Returns the parent PID of target_pid using Toolhelp32 snapshot, or None if not found."""
    TH32CS_SNAPPROCESS = 0x00000002
    h_snap = _kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if h_snap == -1 or h_snap == wintypes.HANDLE(-1).value:
        return None
    try:
        pe = PROCESSENTRY32()
        pe.dwSize = ctypes.sizeof(PROCESSENTRY32)
        if not _kernel32.Process32First(h_snap, ctypes.byref(pe)):
            return None
        while True:
            if pe.th32ProcessID == target_pid:
                return int(pe.th32ParentProcessID)
            if not _kernel32.Process32Next(h_snap, ctypes.byref(pe)):
                break
        return None
    finally:
        _kernel32.CloseHandle(h_snap)


def _wait_for_worker_bootstrap_ack(
    proc: subprocess.Popen,
    h_job: int,
    d_out: OverlappedPipeDrainer,
    startup_ack_token: str,
    expected_hash: str,
    timeout_sec: float = 5.0,
) -> tuple[bool, str]:
    """Waits for authentic worker startup acknowledgment line from d_out.

    The worker script emits:
        __JARVIS_BOOTSTRAP_ACK__:<pid>:<token>:<hash>
    upon successfully loading under the supervisor's FILE_SHARE_READ lock.

    Returns:
        (True, "") on valid authenticated acknowledgment.
        (False, error_reason) on failure, mismatch, or timeout.
    """
    MAGIC_HEADER = "__JARVIS_BOOTSTRAP_ACK__:"
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        # Check accumulated output chunks from d_out first
        combined = "".join(d_out.state.chunks)
        if MAGIC_HEADER in combined:
            ack_count = combined.count(MAGIC_HEADER)
            if ack_count > 1:
                return False, f"Duplicate worker bootstrap acknowledgment detected ({ack_count} occurrences)"

            for line in combined.splitlines():
                if line.startswith(MAGIC_HEADER):
                    parts = line.split(":", 3)
                    if len(parts) >= 4:
                        emitted_pid_str, emitted_token, emitted_hash = parts[1], parts[2], parts[3].strip()
                        # 1. Validate private startup token
                        if not hmac.compare_digest(emitted_token, startup_ack_token):
                            return False, "Token mismatch in bootstrap acknowledgment"
                        # 2. Validate bootstrap SHA-256 digest
                        if not hmac.compare_digest(emitted_hash, expected_hash):
                            return False, f"Worker bootstrap hash mismatch in acknowledgment: expected {expected_hash}, got {emitted_hash}"
                        # 3. Validate process identity
                        try:
                            emitted_pid = int(emitted_pid_str)
                        except ValueError:
                            return False, f"Non-numeric PID in acknowledgment: {emitted_pid_str}"

                        if emitted_pid == proc.pid:
                            # Direct execution without trampoline: launched process is the worker
                            return True, ""

                        # Trampoline execution (e.g. Windows venv Scripts\python.exe launcher wrapper):
                        # The acknowledging process MUST be the immediate child of proc.pid
                        # AND assigned to the supervisor's Job Object.
                        h_child = _kernel32.OpenProcess(0x1000, False, emitted_pid)
                        if not h_child:
                            return False, f"Cannot open acknowledging process PID {emitted_pid} (process not found or access denied)"
                        try:
                            in_job = wintypes.BOOL(False)
                            _kernel32.IsProcessInJob(h_child, wintypes.HANDLE(h_job), ctypes.byref(in_job))
                            if not in_job.value:
                                return False, f"Acknowledging process PID {emitted_pid} is not assigned to supervisor Job Object"
                        finally:
                            _kernel32.CloseHandle(h_child)

                        # Verify that emitted_pid is the DIRECT child of proc.pid (not a descendant or grandchild)
                        parent_pid = _get_process_parent_pid(emitted_pid)
                        if parent_pid != proc.pid:
                            return False, (
                                f"Acknowledging process PID {emitted_pid} is a descendant (parent {parent_pid}), "
                                f"not the authorized worker process (expected parent {proc.pid})"
                            )

                        return True, ""
            return False, "Malformed acknowledgment line"

        # If child exited, wait for drainer thread to complete reading remaining output
        if proc.poll() is not None:
            if d_out.thread and d_out.thread.is_alive():
                d_out.thread.join(timeout=0.5)
            combined = "".join(d_out.state.chunks)
            if MAGIC_HEADER in combined:
                continue
            return False, f"Worker process terminated prematurely (exit code {proc.returncode}) without bootstrap acknowledgment"

        # Check if output pipe was closed prematurely
        if d_out.state.status in ("BROKEN_PIPE", "COMPLETED") and not d_out.state.chunks:
            return False, "Pipe closed before receiving worker bootstrap acknowledgment"

        time.sleep(0.01)

    return False, f"Worker startup timed out after {timeout_sec:.1f}s without bootstrap acknowledgment"


def run_supervised_worker(
    snapshot_root: Path,
    worker_args: list[str],
    timeout_sec: float = 30.0,
    max_output_bytes: int = 10 * 1024 * 1024,
    auth_secret: bytes | None = None,
) -> tuple[SupervisorResult, JobHandleHolder, list[threading.Thread]]:
    """Launches worker bootstrap under Job Object with strict overlapped drainer verification."""
    bootstrap_script = snapshot_root / "tools" / "test_worker_bootstrap.py"
    if not bootstrap_script.is_file() or bootstrap_script.is_symlink():
        dummy_h = JobHandleHolder(None)
        return (
            SupervisorResult(
                status="ERROR",
                exit_code=1,
                stdout="",
                stderr=f"Bootstrap script missing or invalid at {bootstrap_script}",
                duration_sec=0.0,
            ),
            dummy_h,
            [],
        )

    # Workstream B: Acquire FILE_SHARE_READ sharing lock to prevent concurrent tampering/replacement during launch
    h_bootstrap = _kernel32.CreateFileW(
        wintypes.LPCWSTR(str(bootstrap_script)),
        wintypes.DWORD(GENERIC_READ),
        wintypes.DWORD(FILE_SHARE_READ),
        None,
        wintypes.DWORD(OPEN_EXISTING),
        wintypes.DWORD(FILE_ATTRIBUTE_NORMAL),
        None,
    )
    if h_bootstrap == INVALID_HANDLE_VALUE or not h_bootstrap:
        dummy_h = JobHandleHolder(None)
        return (
            SupervisorResult(
                status="ERROR",
                exit_code=1,
                stdout="",
                stderr=f"Failed to acquire sharing lock on bootstrap script at {bootstrap_script}",
                duration_sec=0.0,
            ),
            dummy_h,
            [],
        )

    try:
        # Authenticate worker bootstrap SHA-256 before process execution under sharing lock
        expected_bootstrap_path = _REPO_ROOT / "tools" / "test_worker_bootstrap.py"
        expected_bootstrap_hash = compute_file_sha256(expected_bootstrap_path)
        actual_bootstrap_hash = compute_file_sha256(bootstrap_script)
        if not hmac.compare_digest(actual_bootstrap_hash, expected_bootstrap_hash):
            dummy_h = JobHandleHolder(None)
            return (
                SupervisorResult(
                    status="ERROR",
                    exit_code=131,
                    stdout="",
                    stderr=(
                        f"[SUPERVISOR SECURITY ERROR] Worker bootstrap SHA-256 hash mismatch:\n"
                        f"  Actual in Snapshot: {actual_bootstrap_hash}\n"
                        f"  Expected Source:    {expected_bootstrap_hash}\n"
                        "Untrusted or tampered bootstrap execution refused."
                    ),
                    duration_sec=0.0,
                ),
                dummy_h,
                [],
            )

        clean_env = build_clean_worker_env(snapshot_root)
        startup_ack_token = secrets.token_hex(16)
        clean_env["_JARVIS_BOOTSTRAP_ACK_TOKEN"] = startup_ack_token
        clean_env["_JARVIS_EXPECTED_BOOTSTRAP_HASH"] = expected_bootstrap_hash
        cmd = [str(PYTHON_EXE), "-I", "-S", str(bootstrap_script)] + worker_args

        # 1. Create Outer Job Object
        h_job = create_process_job_object(kill_on_close=True)
        if not h_job:
            dummy_h = JobHandleHolder(None)
            return (
                SupervisorResult(
                    status="ERROR",
                    exit_code=1,
                    stdout="",
                    stderr="Failed to create Win32 Job Object for worker containment",
                    duration_sec=0.0,
                ),
                dummy_h,
                [],
            )

        holder = JobHandleHolder(h_job)
        t_start = time.monotonic()
        diagnostics: list[str] = []

        # 2. Create Overlapped Named Pipes for stdout and stderr (BLK-22)
        pipe_out = OverlappedPipePair("snap_worker_out")
        pipe_err = OverlappedPipePair("snap_worker_err")
        fd_out = pipe_out.get_client_fd()
        fd_err = pipe_err.get_client_fd()

        # 3. Spawn suspended inside snapshot directory
        stdin_target = subprocess.PIPE if auth_secret is not None else subprocess.DEVNULL
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(snapshot_root),
                env=clean_env,
                stdin=stdin_target,
                stdout=fd_out,
                stderr=fd_err,
                creationflags=CREATE_SUSPENDED | CREATE_NO_WINDOW,
            )
        except OSError as exc:
            if h_bootstrap and h_bootstrap != INVALID_HANDLE_VALUE:
                _kernel32.CloseHandle(h_bootstrap)
                h_bootstrap = wintypes.HANDLE(INVALID_HANDLE_VALUE)
            pipe_out.close_client_in_supervisor()
            pipe_err.close_client_in_supervisor()
            pipe_out.close_server()
            pipe_err.close_server()
            holder.close()
            return (
                SupervisorResult(
                    status="ERROR",
                    exit_code=1,
                    stdout="",
                    stderr=f"Failed to spawn suspended worker process: {exc}",
                    duration_sec=0.0,
                ),
                holder,
                [],
            )

        # 4. Assign to Job Object
        if not assign_process_to_job(h_job, proc):
            proc.kill()
            if h_bootstrap and h_bootstrap != INVALID_HANDLE_VALUE:
                _kernel32.CloseHandle(h_bootstrap)
                h_bootstrap = wintypes.HANDLE(INVALID_HANDLE_VALUE)
            pipe_out.close_client_in_supervisor()
            pipe_err.close_client_in_supervisor()
            pipe_out.close_server()
            pipe_err.close_server()
            holder.close()
            return (
                SupervisorResult(
                    status="ERROR",
                    exit_code=1,
                    stdout="",
                    stderr="Failed to assign worker process to Win32 Job Object",
                    duration_sec=0.0,
                ),
                holder,
                [],
            )

        # Close supervisor's write handles so only child tree holds them
        pipe_out.close_client_in_supervisor()
        pipe_err.close_client_in_supervisor()

        # If auth_secret is provided: transfer through inherited private pipe and immediately close write handle
        if auth_secret is not None and proc.stdin is not None:
            try:
                proc.stdin.write(auth_secret)
                proc.stdin.flush()
            finally:
                proc.stdin.close()

        # 5. Resume child thread while STILL retaining bootstrap sharing lock
        if not _resume_process(proc):
            proc.kill()
            if h_bootstrap and h_bootstrap != INVALID_HANDLE_VALUE:
                _kernel32.CloseHandle(h_bootstrap)
                h_bootstrap = wintypes.HANDLE(INVALID_HANDLE_VALUE)
            pipe_out.close_server()
            pipe_err.close_server()
            holder.close()
            return (
                SupervisorResult(
                    status="ERROR",
                    exit_code=1,
                    stdout="",
                    stderr="Failed to resume worker process thread",
                    duration_sec=0.0,
                ),
                holder,
                [],
            )

        # 6. Start Overlapped Reader Threads (BLK-22)
        d_out = OverlappedPipeDrainer(pipe_out.h_server, "stdout", max_bytes=max_output_bytes, deadline_sec=timeout_sec)
        d_err = OverlappedPipeDrainer(pipe_err.h_server, "stderr", max_bytes=max_output_bytes, deadline_sec=timeout_sec)
        d_out.start()
        d_err.start()
        drainers = [d_out.thread, d_err.thread] if d_out.thread and d_err.thread else []

        # 6A. Authenticate Worker Startup Acknowledgment (Phase 1U)
        ack_ok, ack_err = _wait_for_worker_bootstrap_ack(
            proc, h_job, d_out, startup_ack_token, expected_bootstrap_hash, timeout_sec=min(timeout_sec, 5.0)
        )

        # Release bootstrap kernel sharing lock now that worker has authenticated its load
        if h_bootstrap and h_bootstrap != INVALID_HANDLE_VALUE:
            _kernel32.CloseHandle(h_bootstrap)
            h_bootstrap = wintypes.HANDLE(INVALID_HANDLE_VALUE)

        if not ack_ok:
            proc.kill()
            _terminate_and_drain(h_job, exit_code=1, drain_timeout=1.0)
            pipe_out.close_server()
            pipe_err.close_server()
            d_out.close()
            d_err.close()
            holder.close()
            return (
                SupervisorResult(
                    status="BOOTSTRAP_ACK_FAILED",
                    exit_code=131,
                    stdout="".join(d_out.state.chunks),
                    stderr=f"[SUPERVISOR SECURITY ERROR] Worker startup acknowledgment failed: {ack_err}",
                    duration_sec=time.monotonic() - t_start,
                ),
                holder,
                [],
            )

        # 7. Deadline & Process-Tree Supervision Loop (50ms polling)
        deadline = t_start + timeout_sec
        active_processes_at_timeout: int | None = None
        status = "COMPLETED"

        while True:
            ret = proc.poll()
            if ret is not None:
                # Drain job accounting to allow exiting worker process accounting to settle
                holder.drain(timeout=0.3)
                active_procs = holder.get_active_process_count()
                if active_procs is None:
                    diagnostics.append(
                        "[SUPERVISOR ERROR] Failed to query Job Object basic accounting information."
                    )
                    status = "PROCESS_ACCOUNTING_QUERY_FAILED"
                elif active_procs > 0:
                    diagnostics.append(
                        f"[SUPERVISOR] Surviving descendants detected ({active_procs} active procs); terminating process tree."
                    )
                    _terminate_and_drain(h_job, exit_code=0, drain_timeout=2.0)
                    holder.drain(timeout=0.3)
                    post_drain_active = holder.get_active_process_count()
                    if post_drain_active != 0:
                        diagnostics.append(
                            f"[SUPERVISOR ERROR] Post-drain active processes non-zero: {post_drain_active}"
                        )
                        status = "DESCENDANT_TERMINATION_FAILED"
                    else:
                        status = "SURVIVING_DESCENDANTS_TERMINATED"
                break

            now = time.monotonic()
            if now >= deadline:
                diagnostics.append(f"[SUPERVISOR] Execution exceeded wall-clock timeout of {timeout_sec:.1f}s.")
                active_processes_at_timeout = _active_process_count(h_job)
                diagnostics.append(f"[SUPERVISOR] Active processes in job at timeout: {active_processes_at_timeout}")

                # Terminate process tree (2.0s drain timeout)
                _terminate_and_drain(h_job, exit_code=124, drain_timeout=2.0)
                if proc.poll() is None:
                    proc.kill()

                status = "TIMEOUT"
                break

            time.sleep(0.05)

        duration = time.monotonic() - t_start

        # Signal drainers to cancel pending reads
        d_out.stop()
        d_err.stop()

        # 8. Drainer Thread Join & Stall Detection (BLK-22)
        join_timeout = 0.5 if status == "TIMEOUT" else 1.0
        out_joined = d_out.join(timeout=join_timeout)
        err_joined = d_err.join(timeout=join_timeout)

        drainer_stalled = False
        if not out_joined or not err_joined:
            drainer_stalled = True
            diagnostics.append("[SUPERVISOR ERROR] Drainer threads failed to terminate cleanly.")
            if status == "COMPLETED":
                status = "DRAINER_STALLED"

        # Propagate explicit drainer errors
        if d_out.state.status in ("INVALID_HANDLE", "API_ERROR", "UNEXPECTED_WAIT_FAILURE", "UNEXPECTED_COMPLETION_FAILURE") or d_err.state.status in ("INVALID_HANDLE", "API_ERROR", "UNEXPECTED_WAIT_FAILURE", "UNEXPECTED_COMPLETION_FAILURE"):
            status = "DRAINER_ERROR"
            diagnostics.append(f"[SUPERVISOR ERROR] Drainer error: stdout={d_out.state.status}, stderr={d_err.state.status}")
        elif d_out.state.status == "UNRESOLVED_CANCELLATION" or d_err.state.status == "UNRESOLVED_CANCELLATION":
            status = "UNRESOLVED_CANCELLATION"
            diagnostics.append("[SUPERVISOR ERROR] Drainer cancellation timed out (pending I/O unresolved)")
        elif d_out.state.status == "BUDGET_EXHAUSTED" or d_err.state.status == "BUDGET_EXHAUSTED":
            status = "OUTPUT_BUDGET_EXHAUSTED"
            diagnostics.append("[SUPERVISOR ERROR] Drainer byte budget exhausted")
        elif d_out.state.status == "DEADLINE_EXHAUSTED" or d_err.state.status == "DEADLINE_EXHAUSTED":
            status = "DRAIN_DEADLINE_EXHAUSTED"
            diagnostics.append("[SUPERVISOR ERROR] Drainer final-drain deadline exhausted")

        # Clean up server pipes and events
        pipe_out.close_server()
        pipe_err.close_server()
        d_out.close()
        d_err.close()

        # Production Call Site: Poll tracker for eventual completions that resolved during process termination
        if d_out.has_unresolved_pending_io() or d_err.has_unresolved_pending_io() or len(RetainedPendingIOTracker.get_active_unresolved()) > 0:
            drained = RetainedPendingIOTracker.poll_and_drain_eventual_completions()
            if drained:
                diagnostics.append(
                    f"[SUPERVISOR] Observed post-termination kernel completion for {len(drained)} retained I/O operations."
                )
                if len(RetainedPendingIOTracker.get_active_unresolved()) == 0 and status == "UNRESOLVED_CANCELLATION":
                    status = "COMPLETED"

        # 9. Job Object accounting drain (5 retries x 100ms)
        holder.drain(timeout=0.5)

        raw_stdout = "".join(d_out.state.chunks)
        err_text = "".join(d_err.state.chunks)
        exit_code = proc.returncode if proc.returncode is not None else 124

        if raw_stdout.count("__JARVIS_BOOTSTRAP_ACK__:") > 1:
            status = "BOOTSTRAP_ACK_FAILED"
            exit_code = 131
            err_text = f"[SUPERVISOR SECURITY ERROR] Worker emitted duplicate bootstrap acknowledgment during execution.\n{err_text}"

        out_text = "".join(
            line for line in raw_stdout.splitlines(keepends=True)
            if not line.startswith("__JARVIS_BOOTSTRAP_ACK__:")
        )

        result = SupervisorResult(
            status=status,
            exit_code=exit_code,
            stdout=out_text,
            stderr=err_text,
            duration_sec=duration,
            active_processes_at_timeout=active_processes_at_timeout,
            drainer_stalled=drainer_stalled,
            diagnostics=diagnostics,
        )

        return result, holder, drainers
    finally:
        if h_bootstrap and h_bootstrap != INVALID_HANDLE_VALUE:
            _kernel32.CloseHandle(h_bootstrap)
            h_bootstrap = wintypes.HANDLE(INVALID_HANDLE_VALUE)


def launch_profile_worker(
    profile: ProfileType,
    snapshot_root: Path,
    worker_args: list[str],
    auth_secret: bytes | None = None,
    timeout_sec: float = 30.0,
    max_output_bytes: int = 10 * 1024 * 1024,
    permit_data: dict | None = None,
    permit_path: Path | None = None,
    job_assignment_failure_inject: bool = False,
    token_mismatch_failure_inject: bool = False,
) -> tuple[SupervisorResult, JobHandleHolder, list[threading.Thread], ProcessWitness | None]:
    """Spawns, encloses, validates isolation witness, and supervises a worker under profile constraints."""
    # 1. Profile / Permit Matching Verification
    if permit_data is not None:
        permit_profile = permit_data.get("profile")
        if permit_profile != profile.value:
            dummy_h = JobHandleHolder(None)
            return (
                SupervisorResult(
                    status="ERROR",
                    exit_code=124,
                    stdout="",
                    stderr=f"PROFILE_MISMATCH: Permit profile '{permit_profile}' does not match requested launcher profile '{profile.value}'",
                    duration_sec=0.0,
                ),
                dummy_h,
                [],
                None,
            )

    # 2. Context B2 Interactive Gate (Preflight without execution)
    if profile == ProfileType.CONTEXT_B2_INTERACTIVE:
        h_winsta = _user32.GetProcessWindowStation()
        winsta_name = _get_user_object_name(h_winsta)
        if winsta_name == "WinSta0":
            dummy_h = JobHandleHolder(None)
            return (
                SupervisorResult(
                    status="ERROR",
                    exit_code=126,
                    stdout="",
                    stderr=(
                        "CONTEXT_B2_BLOCKED: No dedicated disposable interactive desktop station available; "
                        f"current window station is '{winsta_name}'. Everyday desktop WinSta0\\Default is prohibited."
                    ),
                    duration_sec=0.0,
                ),
                dummy_h,
                [],
                None,
            )

    # 3. Target bootstrap check
    bootstrap_script = snapshot_root / "tools" / "test_worker_bootstrap.py"
    if not bootstrap_script.is_file() or bootstrap_script.is_symlink():
        dummy_h = JobHandleHolder(None)
        return (
            SupervisorResult(
                status="ERROR",
                exit_code=1,
                stdout="",
                stderr=f"Bootstrap script missing or invalid at {bootstrap_script}",
                duration_sec=0.0,
            ),
            dummy_h,
            [],
            None,
        )

    # Workstream B: Acquire FILE_SHARE_READ sharing lock to prevent concurrent tampering/replacement during launch
    h_bootstrap = _kernel32.CreateFileW(
        wintypes.LPCWSTR(str(bootstrap_script)),
        wintypes.DWORD(GENERIC_READ),
        wintypes.DWORD(FILE_SHARE_READ),
        None,
        wintypes.DWORD(OPEN_EXISTING),
        wintypes.DWORD(FILE_ATTRIBUTE_NORMAL),
        None,
    )
    if h_bootstrap == INVALID_HANDLE_VALUE or not h_bootstrap:
        dummy_h = JobHandleHolder(None)
        return (
            SupervisorResult(
                status="ERROR",
                exit_code=1,
                stdout="",
                stderr=f"Failed to acquire sharing lock on bootstrap script at {bootstrap_script}",
                duration_sec=0.0,
            ),
            dummy_h,
            [],
            None,
        )

    try:
        # Authenticate worker bootstrap SHA-256 before process execution under sharing lock
        expected_bootstrap_path = _REPO_ROOT / "tools" / "test_worker_bootstrap.py"
        expected_bootstrap_hash = compute_file_sha256(expected_bootstrap_path)
        actual_bootstrap_hash = compute_file_sha256(bootstrap_script)
        if not hmac.compare_digest(actual_bootstrap_hash, expected_bootstrap_hash):
            dummy_h = JobHandleHolder(None)
            return (
                SupervisorResult(
                    status="ERROR",
                    exit_code=131,
                    stdout="",
                    stderr=(
                        f"[SUPERVISOR SECURITY ERROR] Worker bootstrap SHA-256 hash mismatch:\n"
                        f"  Actual in Snapshot: {actual_bootstrap_hash}\n"
                        f"  Expected Source:    {expected_bootstrap_hash}\n"
                        "Untrusted or tampered bootstrap execution refused."
                    ),
                    duration_sec=0.0,
                ),
                dummy_h,
                [],
                None,
            )

        clean_env = build_clean_worker_env(snapshot_root)
        startup_ack_token = secrets.token_hex(16)
        clean_env["_JARVIS_BOOTSTRAP_ACK_TOKEN"] = startup_ack_token
        clean_env["_JARVIS_EXPECTED_BOOTSTRAP_HASH"] = expected_bootstrap_hash
        cmd = [str(PYTHON_EXE), "-I", "-S", str(bootstrap_script)] + worker_args

        # 4. Create Outer Win32 Job Object
        h_job = create_process_job_object(kill_on_close=True)
        if not h_job:
            dummy_h = JobHandleHolder(None)
            return (
                SupervisorResult(
                    status="ERROR",
                    exit_code=1,
                    stdout="",
                    stderr="Failed to create Win32 Job Object for worker containment",
                    duration_sec=0.0,
                ),
                dummy_h,
                [],
                None,
            )

        holder = JobHandleHolder(h_job)
        t_start = time.monotonic()

        # 5. Create Overlapped Named Pipes
        pipe_out = OverlappedPipePair("snap_profile_out")
        pipe_err = OverlappedPipePair("snap_profile_err")
        fd_out = pipe_out.get_client_fd()
        fd_err = pipe_err.get_client_fd()

        stdin_target = subprocess.PIPE if auth_secret is not None else subprocess.DEVNULL

        # 6. Spawn suspended child process
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(snapshot_root),
                env=clean_env,
                stdin=stdin_target,
                stdout=fd_out,
                stderr=fd_err,
                creationflags=CREATE_SUSPENDED | CREATE_NO_WINDOW,
            )
        except OSError as exc:
            if h_bootstrap and h_bootstrap != INVALID_HANDLE_VALUE:
                _kernel32.CloseHandle(h_bootstrap)
                h_bootstrap = wintypes.HANDLE(INVALID_HANDLE_VALUE)
            pipe_out.close_client_in_supervisor()
            pipe_err.close_client_in_supervisor()
            pipe_out.close_server()
            pipe_err.close_server()
            holder.close()
            return (
                SupervisorResult(
                    status="ERROR",
                    exit_code=1,
                    stdout="",
                    stderr=f"Failed to spawn suspended worker process: {exc}",
                    duration_sec=0.0,
                ),
                holder,
                [],
                None,
            )

        # 7. Assign to Win32 Job Object BEFORE Resume
        if job_assignment_failure_inject or not assign_process_to_job(h_job, proc):
            proc.kill()
            if h_bootstrap and h_bootstrap != INVALID_HANDLE_VALUE:
                _kernel32.CloseHandle(h_bootstrap)
                h_bootstrap = wintypes.HANDLE(INVALID_HANDLE_VALUE)
            pipe_out.close_client_in_supervisor()
            pipe_err.close_client_in_supervisor()
            pipe_out.close_server()
            pipe_err.close_server()
            holder.close()
            return (
                SupervisorResult(
                    status="ERROR",
                    exit_code=1,
                    stdout="",
                    stderr="Failed to assign worker process to Win32 Job Object (killed before resume)",
                    duration_sec=time.monotonic() - t_start,
                ),
                holder,
                [],
                None,
            )

        pipe_out.close_client_in_supervisor()
        pipe_err.close_client_in_supervisor()

        # 8. Query Process Witness while suspended
        try:
            witness = query_process_witness(proc._handle, proc.pid)
        except (RuntimeError, OSError) as exc:
            proc.kill()
            if h_bootstrap and h_bootstrap != INVALID_HANDLE_VALUE:
                _kernel32.CloseHandle(h_bootstrap)
                h_bootstrap = wintypes.HANDLE(INVALID_HANDLE_VALUE)
            pipe_out.close_server()
            pipe_err.close_server()
            holder.close()
            return (
                SupervisorResult(
                    status="ERROR",
                    exit_code=1,
                    stdout="",
                    stderr=f"Failed to query child token witness: {exc}",
                    duration_sec=time.monotonic() - t_start,
                ),
                holder,
                [],
                None,
            )

        # Simulated token mismatch injection for testing fail-closed boundary
        if token_mismatch_failure_inject:
            witness = ProcessWitness(
                pid=witness.pid,
                is_appcontainer=False,
                integrity_rid=0x3000,
                integrity_label="High",
                is_elevated=True,
                session_id=witness.session_id,
                appcontainer_sid=None,
                user_sid=witness.user_sid,
                capabilities=(),
                window_station=witness.window_station,
                desktop=witness.desktop,
            )

        # 9. Profile Boundary Validation Gate
        boundary_ok, boundary_reason = validate_profile_boundary(profile, witness, snapshot_root)
        if not boundary_ok:
            # FAIL CLOSED: kill suspended process immediately. Never resume!
            proc.kill()
            if h_bootstrap and h_bootstrap != INVALID_HANDLE_VALUE:
                _kernel32.CloseHandle(h_bootstrap)
                h_bootstrap = wintypes.HANDLE(INVALID_HANDLE_VALUE)
            pipe_out.close_server()
            pipe_err.close_server()
            holder.close()
            return (
                SupervisorResult(
                    status="ERROR",
                    exit_code=125,
                    stdout="",
                    stderr=f"PROFILE_ISOLATION_REJECTED: {boundary_reason} (Process killed while suspended; never resumed)",
                    duration_sec=time.monotonic() - t_start,
                ),
                holder,
                [],
                witness,
            )

        # 10. Transfer auth secret over private stdin pipe
        if auth_secret is not None and proc.stdin is not None:
            try:
                proc.stdin.write(auth_secret)
                proc.stdin.flush()
            finally:
                proc.stdin.close()

        # 11. Resume child thread while STILL retaining bootstrap sharing lock
        if not _resume_process(proc):
            proc.kill()
            if h_bootstrap and h_bootstrap != INVALID_HANDLE_VALUE:
                _kernel32.CloseHandle(h_bootstrap)
                h_bootstrap = wintypes.HANDLE(INVALID_HANDLE_VALUE)
            pipe_out.close_server()
            pipe_err.close_server()
            holder.close()
            return (
                SupervisorResult(
                    status="ERROR",
                    exit_code=1,
                    stdout="",
                    stderr="Failed to resume worker process thread",
                    duration_sec=time.monotonic() - t_start,
                ),
                holder,
                [],
                witness,
            )

        # 12. Start Overlapped Reader Threads
        d_out = OverlappedPipeDrainer(pipe_out.h_server, "stdout", max_bytes=max_output_bytes, deadline_sec=timeout_sec)
        d_err = OverlappedPipeDrainer(pipe_err.h_server, "stderr", max_bytes=max_output_bytes, deadline_sec=timeout_sec)
        d_out.start()
        d_err.start()
        drainers = [d_out.thread, d_err.thread] if d_out.thread and d_err.thread else []

        # 12A. Authenticate Worker Startup Acknowledgment (Phase 1U)
        ack_ok, ack_err = _wait_for_worker_bootstrap_ack(
            proc, h_job, d_out, startup_ack_token, expected_bootstrap_hash, timeout_sec=min(timeout_sec, 5.0)
        )

        # Release bootstrap kernel sharing lock now that worker has authenticated its load
        if h_bootstrap and h_bootstrap != INVALID_HANDLE_VALUE:
            _kernel32.CloseHandle(h_bootstrap)
            h_bootstrap = wintypes.HANDLE(INVALID_HANDLE_VALUE)

        if not ack_ok:
            proc.kill()
            _terminate_and_drain(h_job, exit_code=1, drain_timeout=1.0)
            pipe_out.close_server()
            pipe_err.close_server()
            d_out.close()
            d_err.close()
            holder.close()
            return (
                SupervisorResult(
                    status="BOOTSTRAP_ACK_FAILED",
                    exit_code=131,
                    stdout="".join(d_out.state.chunks),
                    stderr=f"[SUPERVISOR SECURITY ERROR] Worker startup acknowledgment failed: {ack_err}",
                    duration_sec=time.monotonic() - t_start,
                ),
                holder,
                [],
                witness,
            )

        # 13. Supervision Loop
        deadline = t_start + timeout_sec
        active_processes_at_timeout: int | None = None
        status = "COMPLETED"

        while True:
            ret = proc.poll()
            if ret is not None:
                break
            if time.monotonic() > deadline:
                status = "TIMEOUT"
                active_processes_at_timeout = _active_process_count(h_job)
                _terminate_and_drain(h_job, timeout_sec=2.0)
                break
            time.sleep(0.05)

        duration = time.monotonic() - t_start
        d_out.stop()
        d_err.stop()
        d_out.join(timeout=3.0)
        d_err.join(timeout=3.0)

        pipe_out.close_server()
        pipe_err.close_server()

        raw_stdout = "".join(d_out.state.chunks)
        stderr_text = "".join(d_err.state.chunks)
        exit_code = proc.returncode if proc.returncode is not None else 1

        if raw_stdout.count("__JARVIS_BOOTSTRAP_ACK__:") > 1:
            status = "BOOTSTRAP_ACK_FAILED"
            exit_code = 131
            stderr_text = f"[SUPERVISOR SECURITY ERROR] Worker emitted duplicate bootstrap acknowledgment during execution.\n{stderr_text}"

        stdout_text = "".join(
            line for line in raw_stdout.splitlines(keepends=True)
            if not line.startswith("__JARVIS_BOOTSTRAP_ACK__:")
        )

        return (
            SupervisorResult(
                status=status,
                exit_code=exit_code,
                stdout=stdout_text,
                stderr=stderr_text,
                duration_sec=duration,
                active_processes_at_timeout=active_processes_at_timeout,
            ),
            holder,
            drainers,
            witness,
        )
    finally:
        if h_bootstrap and h_bootstrap != INVALID_HANDLE_VALUE:
            _kernel32.CloseHandle(h_bootstrap)
            h_bootstrap = wintypes.HANDLE(INVALID_HANDLE_VALUE)


# ---------------------------------------------------------------------------
# Preflight Verification Routine
# ---------------------------------------------------------------------------
def run_preflight() -> int:
    """Executes full Phase 1 setup preflight verification without running test suites."""
    print("=== PHASE 1B SUPERVISOR REMEDIATION PREFLIGHT GATES ===")
    print(f"Target Python:      {PYTHON_EXE}")
    print(f"Original Repo:      {ORIGINAL_REPO}")
    print(f"Approved Hash:      {APPROVED_ENV_EXAMPLE_SHA256}")

    # 1. Dispatcher Validation
    print("\n[Gate 1/5] Verifying four-profile dispatcher partitions...")
    dispatcher = ExecutionProfileDispatcher()
    all_507 = dispatcher.all_node_ids
    assert len(all_507) == 507, f"Expected 507 nodes, got {len(all_507)}"
    print(f"  Total Cohort Nodes:       {len(all_507)}")
    print(f"  Context A (AppContainer): {len(dispatcher.get_nodes_for_profile(ProfileType.CONTEXT_A))}")
    print(f"  Context B1 Ordinary:      {len(dispatcher.get_nodes_for_profile(ProfileType.CONTEXT_B1_ORDINARY))}")
    print(f"  Context B1 Snapshot:      {len(dispatcher.get_nodes_for_profile(ProfileType.CONTEXT_B1_SNAPSHOT))}")
    print(f"  Context B2 Interactive:   {len(dispatcher.get_nodes_for_profile(ProfileType.CONTEXT_B2_INTERACTIVE))}")

    # 2. Provisioning & Full Manifest Verification
    print("\n[Gate 2/5] Provisioning disposable test snapshot with full tree manifest...")
    snap_dir, manifest = provision_snapshot()
    print(f"  Snapshot created:         {snap_dir}")
    print(f"  Source Files Audited:     {manifest.source_file_count}")
    print(f"  Dest Files Audited:       {manifest.destination_file_count}")
    print(f"  1:1 Matched Hashes:       {manifest.matched_hashes_count}")
    print(f"  Composite Tree Digest:    {manifest.tree_digest}")
    print(f"  Reparse Points Found:     {manifest.reparse_points_detected}")
    print(f"  Fresh Dirs Confirmed:     {len(manifest.fresh_directory_assertions)}")

    holder = None
    try:
        # 3. Worker Bootstrap Preflight
        print("\n[Gate 3/5] Spawning worker bootstrap in preflight mode...")
        result, holder, drainers = run_supervised_worker(snap_dir, ["--preflight"], timeout_sec=15.0)

        print(f"  Supervisor Status: {result.status}")
        print(f"  Worker Exit Code:  {result.exit_code}")
        print(f"  Duration:          {result.duration_sec:.2f}s")
        print("\n--- Worker Output ---")
        print(result.stdout.strip())
        if result.stderr.strip():
            print("\n--- Worker Error ---")
            print(result.stderr.strip())

        assert result.status == "COMPLETED", f"Expected COMPLETED, got {result.status}"
        assert result.exit_code == 0, f"Worker exited with code {result.exit_code}"
        assert "=== WORKER BOOTSTRAP PREFLIGHT SUCCESS ===" in result.stdout

        # 4. Single-Use Permit Rejection Probe (BLK-21)
        print("\n[Gate 4/5] Testing fail-closed authorization permit gate...")
        unauthorized_res, unauth_holder, _ = run_supervised_worker(snap_dir, ["--target-run"], timeout_sec=5.0)
        unauth_holder.close()
        assert unauthorized_res.exit_code == 120, f"Expected code 120 on missing permit, got {unauthorized_res.exit_code}"
        assert "AUTHORIZATION ERROR" in unauthorized_res.stderr
        print("  Missing permit rejected with code 120 (Fail-closed verified)!")

        # 5. Conditional Cleanup Evaluation (BLK-20)
        print("\n[Gate 5/5] Evaluating conditional cleanup state machine...")
        cleanup_action = evaluate_cleanup_and_teardown(snap_dir, result, holder, drainers)
        print(f"  Cleanup Action:    {cleanup_action}")
        print(f"  Snapshot Cleaned:  {not snap_dir.exists()}")
        assert cleanup_action == "CLEANUP_PERMITTED_AND_EXECUTED"
        return 0
    finally:
        if holder is not None:
            holder.close()
        if snap_dir.exists():
            cleanup_snapshot(snap_dir)


if __name__ == "__main__":
    if "--preflight" in sys.argv:
        sys.exit(run_preflight())

    print("Snapshot Test Supervisor initialized.")
    print("Usage: python tools/snapshot_test_supervisor.py --preflight")
