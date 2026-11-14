"""Test Worker Bootstrap — Isolated Environment & Import Boundary.

Executed in worker process via:
    python.exe -I -S <snapshot_root>\\tools\\test_worker_bootstrap.py <pytest_args>

Invariants Enforced:
1. Exact CPython 3.12.10 interpreter pin.
2. Process CWD confined strictly inside snapshot root and outside original repo.
3. 12-variable minimal deterministic environment; TEMP/TMP confined to snapshot.
4. CPython import machinery validation (sys.meta_path, sys.path_hooks, sys.path_importer_cache).
5. pywin32 DLL directory registration under -S.
6. Zero unmanaged .pth file execution or unapproved path injection.
"""

from __future__ import annotations

import hashlib
import importlib.machinery
import os
import sys
import tempfile
import zipimport
from pathlib import Path

# ---------------------------------------------------------------------------
# 1. Exact CPython Version Pinning (BLK-04)
# ---------------------------------------------------------------------------
REQUIRED_VERSION = (3, 12, 10)
if sys.version_info[:3] != REQUIRED_VERSION:
    sys.stderr.write(
        f"[BOOTSTRAP ERROR] CPython {REQUIRED_VERSION} required; detected {sys.version_info[:3]}\n"
    )
    sys.exit(101)

# ---------------------------------------------------------------------------
# 2. Current Working Directory Confinement (BLK-08)
# ---------------------------------------------------------------------------
SNAPSHOT_ROOT = Path(__file__).resolve().parent.parent
ORIGINAL_ROOT = Path(r"C:\Users\Youssef\Projects\Jarvis_2.0").resolve()
current_cwd = Path.cwd().resolve()

if not current_cwd.is_relative_to(SNAPSHOT_ROOT):
    sys.stderr.write(
        f"[BOOTSTRAP ERROR] Working directory escape: {current_cwd} is outside snapshot {SNAPSHOT_ROOT}\n"
    )
    sys.exit(102)

if SNAPSHOT_ROOT != ORIGINAL_ROOT and current_cwd.is_relative_to(ORIGINAL_ROOT):
    sys.stderr.write(
        f"[BOOTSTRAP ERROR] Working directory escape: {current_cwd} is inside original repo {ORIGINAL_ROOT}\n"
    )
    sys.exit(103)

# ---------------------------------------------------------------------------
# 2A. Worker Startup Acknowledgment Protocol (Phase 1U)
# ---------------------------------------------------------------------------
_ack_token = os.environ.pop("_JARVIS_BOOTSTRAP_ACK_TOKEN", None)
if _ack_token:
    if "--synthetic-ack-delay" in sys.argv:
        import time as _t_ack
        _t_ack.sleep(0.15)

    if "--synthetic-ack-descendant-impersonation" in sys.argv:
        # Spawn a descendant process that attempts to impersonate the worker acknowledgment
        import subprocess as _sp_ack
        _own_script = Path(__file__).resolve()
        with _own_script.open("rb") as _f_own:
            _own_hash = hashlib.sha256(_f_own.read()).hexdigest().lower()
        _desc_code = (
            f"import os, sys; "
            f"sys.stdout.write('__JARVIS_BOOTSTRAP_ACK__:' + str(os.getpid()) + ':{_ack_token}:{_own_hash}\\n'); "
            f"sys.stdout.flush()"
        )
        _desc_proc = _sp_ack.Popen([sys.executable, "-c", _desc_code])
        _desc_proc.wait()
    elif "--synthetic-ack-skip" not in sys.argv:
        try:
            _own_script = Path(__file__).resolve()
            with _own_script.open("rb") as _f_own:
                _own_hash = hashlib.sha256(_f_own.read()).hexdigest().lower()

            _pid_to_emit = (os.getpid() + 999999) if "--synthetic-ack-wrong-pid" in sys.argv else os.getpid()
            _token_to_emit = ("deadbeef" * 4) if "--synthetic-ack-wrong-token" in sys.argv else _ack_token
            _hash_to_emit = ("0" * 64) if "--synthetic-ack-wrong-hash" in sys.argv else _own_hash

            _ack_line = f"__JARVIS_BOOTSTRAP_ACK__:{_pid_to_emit}:{_token_to_emit}:{_hash_to_emit}\n"
            sys.stdout.write(_ack_line)
            if "--synthetic-ack-duplicate" in sys.argv:
                sys.stdout.write(_ack_line)
            sys.stdout.flush()
        except OSError as _exc:
            sys.stderr.write(f"[BOOTSTRAP ERROR] Failed to emit startup acknowledgment: {_exc}\n")
            sys.exit(100)

# ---------------------------------------------------------------------------
# 3. TEMP / TMP Confinement & Reset (BLK-13)
# ---------------------------------------------------------------------------
snap_temp = (SNAPSHOT_ROOT / "temp").resolve()
if not snap_temp.is_dir():
    try:
        snap_temp.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        sys.stderr.write(f"[BOOTSTRAP ERROR] Failed to create snapshot temp directory {snap_temp}: {exc}\n")
        sys.exit(104)

os.environ["TEMP"] = str(snap_temp)
os.environ["TMP"] = str(snap_temp)
tempfile.tempdir = None  # Reset cached tempdir

evaluated_temp = Path(tempfile.gettempdir()).resolve()
if not evaluated_temp.is_relative_to(SNAPSHOT_ROOT):
    sys.stderr.write(
        f"[BOOTSTRAP ERROR] Temp directory escape: {evaluated_temp} is outside snapshot {SNAPSHOT_ROOT}\n"
    )
    sys.exit(105)

# ---------------------------------------------------------------------------
# 4. sys.path Construction & pywin32 Registration (BLK-05, BLK-06)
# ---------------------------------------------------------------------------
# Locate virtual environment site-packages
base_prefix = Path(sys.base_prefix).resolve()
venv_candidates = [
    Path(r"C:\Users\Youssef\Projects\.venv"),
    Path(sys.prefix).resolve(),
]

site_packages: Path | None = None
for cand in venv_candidates:
    sp = cand / "Lib" / "site-packages"
    if sp.is_dir() and (sp / "pytest").is_dir():
        site_packages = sp.resolve()
        break

if site_packages is None:
    sys.stderr.write("[BOOTSTRAP ERROR] Virtualenv site-packages with pytest not found\n")
    sys.exit(106)

sp_win32 = site_packages / "win32"
sp_win32_lib = sp_win32 / "lib"
sp_pythonwin = site_packages / "Pythonwin"
pywin32_system32 = site_packages / "pywin32_system32"

# Preserve standard library directories from initial sys.path under -I -S
stdlib_paths = [p for p in sys.path if Path(p).resolve().is_relative_to(base_prefix)]

# Explicit sys.path configuration (no unmanaged .pth)
sys.path = [
    str(SNAPSHOT_ROOT),
    *stdlib_paths,
    str(site_packages),
    str(sp_win32),
    str(sp_win32_lib),
    str(sp_pythonwin),
]

# Register pywin32 DLL directory and retain cookie
_pywin32_dll_cookie = None
if pywin32_system32.is_dir():
    try:
        _pywin32_dll_cookie = os.add_dll_directory(str(pywin32_system32))
    except (OSError, AttributeError) as exc:
        sys.stderr.write(f"[BOOTSTRAP WARNING] os.add_dll_directory failed: {exc}\n")

# ---------------------------------------------------------------------------
# 5. CPython Import Machinery Validation (BLK-10)
# ---------------------------------------------------------------------------
# 5.1 sys.meta_path validation
EXPECTED_META = (
    importlib.machinery.BuiltinImporter,
    importlib.machinery.FrozenImporter,
    importlib.machinery.PathFinder,
)
if len(sys.meta_path) != len(EXPECTED_META):
    sys.stderr.write(
        f"[BOOTSTRAP ERROR] Unapproved meta_path count: expected {len(EXPECTED_META)}, found {len(sys.meta_path)}\n"
    )
    sys.exit(107)

for actual, expected in zip(sys.meta_path, EXPECTED_META):
    if actual is not expected:
        sys.stderr.write(f"[BOOTSTRAP ERROR] Unapproved finder: {actual} is not {expected}\n")
        sys.exit(108)

# 5.2 sys.path_hooks validation
if len(sys.path_hooks) != 2:
    sys.stderr.write(f"[BOOTSTRAP ERROR] Unapproved path_hooks count: {len(sys.path_hooks)}\n")
    sys.exit(109)

if sys.path_hooks[0] is not zipimport.zipimporter:
    sys.stderr.write(f"[BOOTSTRAP ERROR] Unapproved hook 0: {sys.path_hooks[0]}\n")
    sys.exit(110)

hook_1 = sys.path_hooks[1]
if (
    getattr(hook_1, "__qualname__", "") != "FileFinder.path_hook.<locals>.path_hook_for_FileFinder"
    or hook_1.__module__ not in ("_frozen_importlib_external", "importlib.machinery")
):
    sys.stderr.write(f"[BOOTSTRAP ERROR] Unapproved hook 1: {hook_1}\n")
    sys.exit(111)

# 5.3 sys.path_importer_cache allowlist validation
stdlib_lib = os.path.realpath(os.path.normcase(os.path.join(sys.base_prefix, "Lib")))
stdlib_dlls = os.path.realpath(os.path.normcase(os.path.join(sys.base_prefix, "DLLs")))
stdlib_zip = os.path.realpath(
    os.path.normcase(os.path.join(sys.base_prefix, f"python{sys.version_info[0]}{sys.version_info[1]}.zip"))
)
canon_snap = os.path.realpath(os.path.normcase(str(SNAPSHOT_ROOT)))
canon_sp = os.path.realpath(os.path.normcase(str(site_packages)))
canon_orig = os.path.realpath(os.path.normcase(str(ORIGINAL_ROOT)))

SUBTREE_APPROVED_ROOTS = {
    stdlib_lib,
    stdlib_dlls,
    canon_snap,
    canon_sp,
    os.path.realpath(os.path.normcase(str(sp_win32))),
    os.path.realpath(os.path.normcase(str(sp_win32_lib))),
    os.path.realpath(os.path.normcase(str(sp_pythonwin))),
}
EXACT_ONLY_ROOTS = {
    os.path.realpath(os.path.normcase(sys.base_prefix)),
    stdlib_zip,
}

for key, finder in list(sys.path_importer_cache.items()):
    canon_key = os.path.realpath(os.path.normcase(key))
    if SNAPSHOT_ROOT != ORIGINAL_ROOT and (canon_key == canon_orig or canon_key.startswith(canon_orig + os.sep)):
        sys.stderr.write(f"[BOOTSTRAP ERROR] Cache contains original repo path: {key}\n")
        sys.exit(112)

    is_subtree_approved = any(
        canon_key == root or canon_key.startswith(root + os.sep) for root in SUBTREE_APPROVED_ROOTS
    )
    is_exact_approved = canon_key in EXACT_ONLY_ROOTS
    if not (is_subtree_approved or is_exact_approved):
        sys.stderr.write(f"[BOOTSTRAP ERROR] Cache contains unapproved path: {key}\n")
        sys.exit(113)

    if finder is not None and type(finder) not in (importlib.machinery.FileFinder, zipimport.zipimporter):
        sys.stderr.write(f"[BOOTSTRAP ERROR] Unapproved finder type {type(finder)} for key {key}\n")
        sys.exit(114)

# ---------------------------------------------------------------------------
# 6. Preflight Diagnostic Mode or Pytest Delegation
# ---------------------------------------------------------------------------
def _run_preflight_diagnostics() -> int:
    """Non-mutating self-verification of worker environment and imports."""
    print("=== WORKER BOOTSTRAP PREFLIGHT START ===")
    print(f"CPython Version:    {sys.version.splitlines()[0]}")
    print(f"Snapshot Root:      {SNAPSHOT_ROOT}")
    print(f"Working Directory:  {Path.cwd()}")
    print(f"Temp Directory:     {tempfile.gettempdir()}")
    print(f"sys.path entries:   {len(sys.path)}")

    # Verify pytest imports cleanly
    import pytest
    print(f"pytest version:     {pytest.__version__}")

    # Verify win32 imports cleanly
    try:
        import win32api
        print(f"win32api module:    {win32api.__name__} (loaded cleanly)")
    except (ImportError, OSError) as exc:
        print(f"win32api import:    FAILED ({exc})")
        return 1

    # Verify jarvis imports from snapshot root
    try:
        import jarvis
        jarvis_path = Path(jarvis.__file__).resolve()
        assert jarvis_path.is_relative_to(SNAPSHOT_ROOT), f"jarvis loaded from {jarvis_path}"
        print(f"jarvis module:      {jarvis_path} (confined to snapshot)")
    except (ImportError, AssertionError, OSError) as exc:
        print(f"jarvis import:      FAILED ({exc})")
        return 1

    print("=== WORKER BOOTSTRAP PREFLIGHT SUCCESS ===")
    return 0


if __name__ == "__main__":
    cli_args = sys.argv[1:]

    if "--preflight" in cli_args:
        sys.exit(_run_preflight_diagnostics())

    if "--synthetic-normal" in cli_args:
        sys.stdout.write("SYNTHETIC_WORKER_NORMAL_OUTPUT\n")
        sys.stdout.flush()
        sys.exit(0)

    if "--synthetic-descendant" in cli_args:
        import subprocess
        subprocess.Popen([
            sys.executable, "-c",
            "import time, sys; time.sleep(2.0); sys.stdout.write('from descendant\\n'); sys.stdout.flush()"
        ])
        sys.stdout.write("parent exiting\n")
        sys.stdout.flush()
        sys.exit(0)

    if "--synthetic-timeout" in cli_args:
        import time
        time.sleep(5.0)
        sys.exit(0)

    if "--synthetic-held-writer" in cli_args:
        import subprocess
        import time
        subprocess.Popen([
            sys.executable, "-c",
            "import time, sys; time.sleep(3.0); sys.stdout.write('held output\\n'); sys.stdout.flush()"
        ])
        time.sleep(5.0)
        sys.exit(0)

    # ---------------------------------------------------------------------------
    # Fail-Closed Execution Permit Authorization Gate (BLK-21)
    # ---------------------------------------------------------------------------
    import ctypes
    import hashlib
    import hmac
    import json
    import secrets
    import time
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ERROR_FILE_NOT_FOUND = 2
    ERROR_FILE_EXISTS = 80
    ERROR_ALREADY_EXISTS = 183

    def _quarantine_permit(claimed_path: Path, reason: str) -> None:
        """Permanently moves a claimed permit to quarantine to preserve evidence and prevent reuse."""
        quarantine_target = claimed_path.with_name(f".execution_permit.quarantined.{reason}")
        try:
            os.replace(claimed_path, quarantine_target)
        except OSError:
            pass

    def _is_legitimate_permit_lifecycle_file(filename: str) -> bool:
        """Strictly validates canonical execution permit lifecycle files.

        Only legitimate operational permit states are excluded from codebase hashing:
        - The original unconsumed permit: .execution_permit.json
        - An in-flight claimed permit: .execution_permit.claimed.<claim_id>
        - A quarantined permit: .execution_permit.quarantined.<reason>_<claim_id>
        - A finalized consumption receipt: .execution_permit.consumed.<permit_id>
        - A failed unlink marker: .execution_permit.unlink_failed.<claim_id>

        Any other file (e.g. .execution_permit.py, .execution_permit_implant) is treated
        as untrusted codebase content and included in destination inventory hashing.
        """
        if filename == ".execution_permit.json":
            return True
        valid_prefixes = (
            ".execution_permit.claimed.",
            ".execution_permit.quarantined.",
            ".execution_permit.consumed.",
            ".execution_permit.unlink_failed.",
        )
        return any(filename.startswith(pfx) for pfx in valid_prefixes)

    def _read_auth_secret() -> bytes:
        """Reads the ephemeral 32-byte authorization secret from inherited private pipe stdin."""
        try:
            return sys.stdin.buffer.read(32)
        except (OSError, ValueError):
            return b""
        finally:
            try:
                sys.stdin.close()
            except OSError:
                pass
            sys.stdin = open(os.devnull, "r", encoding="utf-8")  # noqa: SIM115

    permit_arg = None
    clean_pytest_args: list[str] = []
    for arg in cli_args:
        if arg.startswith("--permit="):
            permit_arg = arg.split("=", 1)[1]
        else:
            clean_pytest_args.append(arg)

    if permit_arg is None:
        sys.stderr.write(
            "[BOOTSTRAP AUTHORIZATION ERROR] Missing supervisor-issued execution permit (--permit=<path>). "
            "Accidental worker invocation is fail-closed blocked.\n"
        )
        sys.exit(120)

    permit_path = Path(permit_arg).resolve()

    # 1. Canonical Permit Path Validation
    # Prohibit using leftover claimed or quarantined permits as new inputs
    if permit_path.name != ".execution_permit.json":
        sys.stderr.write(
            f"[BOOTSTRAP AUTHORIZATION ERROR] Non-canonical permit path rejected: '{permit_path.name}'. "
            "Claimed or quarantined permits cannot be used as new original permits.\n"
        )
        sys.exit(127)

    if permit_path.parent.resolve() != SNAPSHOT_ROOT.resolve():
        sys.stderr.write(
            f"[BOOTSTRAP AUTHORIZATION ERROR] Permit parent directory mismatch: {permit_path.parent.resolve()} != {SNAPSHOT_ROOT.resolve()}\n"
        )
        sys.exit(124)

    if not permit_path.is_file():
        sys.stderr.write(
            f"[BOOTSTRAP AUTHORIZATION ERROR] Permit file not found or inaccessible: {permit_path}\n"
        )
        sys.exit(120)

    # 2. Strict No-Overwrite Atomic Single-Use Claim (Win32 MoveFileExW dwFlags=0)
    claim_id = f"{os.getpid()}_{time.time_ns()}_{secrets.token_hex(4)}"
    claimed_permit_path = permit_path.with_name(f".execution_permit.claimed.{claim_id}")

    move_res = _k32.MoveFileExW(
        wintypes.LPCWSTR(str(permit_path)),
        wintypes.LPCWSTR(str(claimed_permit_path)),
        wintypes.DWORD(0),  # Strictly NO MOVEFILE_REPLACE_EXISTING
    )
    if not move_res:
        win_err = ctypes.get_last_error()
        if win_err in (ERROR_FILE_EXISTS, ERROR_ALREADY_EXISTS):
            sys.stderr.write(
                "[BOOTSTRAP AUTHORIZATION ERROR] Claim destination collision: destination file already exists (no-overwrite enforced).\n"
            )
            sys.exit(127)
        elif win_err == ERROR_FILE_NOT_FOUND:
            sys.stderr.write(
                "[BOOTSTRAP AUTHORIZATION ERROR] Permit already claimed, missing, or replayed (atomic claim failed).\n"
            )
            sys.exit(127)
        else:
            sys.stderr.write(
                f"[BOOTSTRAP AUTHORIZATION ERROR] Atomic permit claim failed with Win32 error {win_err}.\n"
            )
            sys.exit(127)

    # 3. Secret Transfer from Inherited Private Pipe
    secret = _read_auth_secret()
    if len(secret) < 32:
        _quarantine_permit(claimed_permit_path, f"secret_transfer_fail_{claim_id}")
        sys.stderr.write(
            f"[BOOTSTRAP AUTHORIZATION ERROR] Secret transfer failed: received {len(secret)} bytes, expected 32. "
            "Inherited private pipe transfer incomplete or unavailable.\n"
        )
        sys.exit(125)

    # 4. Parse JSON Permit Data
    try:
        with claimed_permit_path.open("r", encoding="utf-8") as f:
            permit_data = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        _quarantine_permit(claimed_permit_path, f"malformed_json_{claim_id}")
        sys.stderr.write(f"[BOOTSTRAP AUTHORIZATION ERROR] Failed to parse permit file: {exc}\n")
        sys.exit(121)

    # 5. Schema Version Validation
    if permit_data.get("schema_version") != 2:
        _quarantine_permit(claimed_permit_path, f"schema_mismatch_{claim_id}")
        sys.stderr.write(
            f"[BOOTSTRAP AUTHORIZATION ERROR] Unsupported permit schema version: {permit_data.get('schema_version')}\n"
        )
        sys.exit(122)

    # 6. Cryptographic Signature Verification (HMAC-SHA256 via hmac.compare_digest)
    provided_sig = permit_data.get("hmac_signature")
    if not provided_sig or not isinstance(provided_sig, str):
        _quarantine_permit(claimed_permit_path, f"missing_sig_{claim_id}")
        sys.stderr.write("[BOOTSTRAP AUTHORIZATION ERROR] Missing or malformed HMAC signature in permit.\n")
        sys.exit(126)

    payload_to_verify = {k: v for k, v in permit_data.items() if k != "hmac_signature"}
    canonical_bytes = json.dumps(payload_to_verify, sort_keys=True).encode("utf-8")
    expected_sig = hmac.new(secret, canonical_bytes, hashlib.sha256).hexdigest()

    if not hmac.compare_digest(expected_sig, provided_sig):
        _quarantine_permit(claimed_permit_path, f"invalid_hmac_{claim_id}")
        sys.stderr.write(
            "[BOOTSTRAP AUTHORIZATION ERROR] Cryptographic signature verification failed (tampered payload or invalid secret).\n"
        )
        sys.exit(126)

    # Discard ephemeral secret references from memory as early as possible
    del secret
    del canonical_bytes
    del expected_sig

    # 7. Expiration & Wall-Clock Unix Timestamp Validation
    MAX_PERMIT_TTL = 3600.0  # 1 hour maximum
    CLOCK_SKEW_TOLERANCE = 30.0  # 30 seconds clock skew tolerance

    now = time.time()
    issued_at = permit_data.get("issued_at", 0.0)
    expires_at = permit_data.get("expires_at", 0.0)
    ttl_sec = permit_data.get("ttl_sec", 0.0)

    # 1. Structural timestamp relationship check
    if issued_at >= expires_at:
        _quarantine_permit(claimed_permit_path, f"invalid_expiry_{claim_id}")
        sys.stderr.write(
            f"[BOOTSTRAP AUTHORIZATION ERROR] Permit invalid timestamps: issued_at {issued_at:.2f} >= expires_at {expires_at:.2f}.\n"
        )
        sys.exit(128)

    # 2. Maximum TTL bounds check
    if ttl_sec > MAX_PERMIT_TTL or (expires_at - issued_at) > (MAX_PERMIT_TTL + 1.0):
        _quarantine_permit(claimed_permit_path, f"excessive_ttl_{claim_id}")
        sys.stderr.write(
            f"[BOOTSTRAP AUTHORIZATION ERROR] Permit TTL exceeds maximum authorized duration ({MAX_PERMIT_TTL}s).\n"
        )
        sys.exit(128)

    # 3. Future-dated check
    if issued_at > now + CLOCK_SKEW_TOLERANCE:
        _quarantine_permit(claimed_permit_path, f"future_dated_{claim_id}")
        sys.stderr.write(
            f"[BOOTSTRAP AUTHORIZATION ERROR] Permit future-dated: issued_at {issued_at:.2f} > current time {now:.2f}.\n"
        )
        sys.exit(128)

    # 4. Expiration check
    if now > expires_at:
        _quarantine_permit(claimed_permit_path, f"expired_{claim_id}")
        sys.stderr.write(
            f"[BOOTSTRAP AUTHORIZATION ERROR] Permit expired: valid until {expires_at:.2f}, current time {now:.2f}.\n"
        )
        sys.exit(128)

    # 8. Scope Validation
    if permit_data.get("scope") != "Option A — 507 nodes":
        _quarantine_permit(claimed_permit_path, f"scope_mismatch_{claim_id}")
        sys.stderr.write(
            f"[BOOTSTRAP AUTHORIZATION ERROR] Unauthorized scope: {permit_data.get('scope')}\n"
        )
        sys.exit(123)

    # 9. Snapshot Root & Independent Receipt Validation (P1-B)
    expected_snap = Path(permit_data.get("snapshot_root", "")).resolve()
    if expected_snap != SNAPSHOT_ROOT.resolve():
        _quarantine_permit(claimed_permit_path, f"snapshot_root_mismatch_{claim_id}")
        sys.stderr.write(
            f"[BOOTSTRAP AUTHORIZATION ERROR] Snapshot root mismatch: {SNAPSHOT_ROOT} != {expected_snap}\n"
        )
        sys.exit(124)

    receipt_file = SNAPSHOT_ROOT / "snapshot_integrity_receipt.json"
    if not receipt_file.is_file():
        _quarantine_permit(claimed_permit_path, f"missing_receipt_{claim_id}")
        sys.stderr.write(
            f"[BOOTSTRAP AUTHORIZATION ERROR] Snapshot integrity receipt missing: {receipt_file}\n"
        )
        sys.exit(124)

    try:
        with receipt_file.open("r", encoding="utf-8") as f:
            receipt_data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        _quarantine_permit(claimed_permit_path, f"malformed_receipt_{claim_id}")
        sys.stderr.write(
            f"[BOOTSTRAP AUTHORIZATION ERROR] Malformed snapshot integrity receipt: {exc}\n"
        )
        sys.exit(124)

    REQUIRED_RECEIPT_FIELDS = frozenset({
        "run_id", "snapshot_id", "source_root", "snapshot_root", "created_at",
        "source_before_digest", "source_after_digest", "destination_digest",
        "composite_snapshot_digest", "approved_file_count", "excluded_path_count",
        "reparse_points_detected", "env_example_sha256", "verification_status"
    })
    missing_receipt_fields = REQUIRED_RECEIPT_FIELDS - set(receipt_data.keys())
    if missing_receipt_fields:
        _quarantine_permit(claimed_permit_path, f"receipt_schema_mismatch_{claim_id}")
        sys.stderr.write(
            f"[BOOTSTRAP AUTHORIZATION ERROR] Snapshot receipt missing required schema fields: {sorted(missing_receipt_fields)}\n"
        )
        sys.exit(124)

    if receipt_data.get("snapshot_id") != SNAPSHOT_ROOT.name:
        _quarantine_permit(claimed_permit_path, f"receipt_snapshot_id_mismatch_{claim_id}")
        sys.stderr.write(
            f"[BOOTSTRAP AUTHORIZATION ERROR] Snapshot receipt ID mismatch: {receipt_data.get('snapshot_id')} != {SNAPSHOT_ROOT.name}\n"
        )
        sys.exit(124)

    if Path(receipt_data.get("snapshot_root", "")).resolve() != SNAPSHOT_ROOT.resolve():
        _quarantine_permit(claimed_permit_path, f"receipt_snapshot_root_mismatch_{claim_id}")
        sys.stderr.write(
            f"[BOOTSTRAP AUTHORIZATION ERROR] Snapshot receipt root mismatch: {receipt_data.get('snapshot_root')} != {SNAPSHOT_ROOT}\n"
        )
        sys.exit(124)

    if receipt_data.get("verification_status") != "VERIFIED_IDENTICAL":
        _quarantine_permit(claimed_permit_path, f"unverified_receipt_status_{claim_id}")
        sys.stderr.write(
            f"[BOOTSTRAP AUTHORIZATION ERROR] Snapshot receipt verification status unverified: {receipt_data.get('verification_status')}\n"
        )
        sys.exit(124)

    # 9A. Assert fresh runtime subtrees are 100% empty (cannot hide unhashed implants)
    for fresh_sub in ("jarvis/skills/custom", "data/evolution_scratch", "data/snapshots", "data/learning_candidates"):
        fresh_path = SNAPSHOT_ROOT / fresh_sub
        if fresh_path.is_dir():
            non_empty_files = [f for _, _, sub_files in os.walk(fresh_path) for f in sub_files]
            if non_empty_files:
                _quarantine_permit(claimed_permit_path, f"fresh_dir_not_empty_{claim_id}")
                sys.stderr.write(
                    f"[BOOTSTRAP AUTHORIZATION ERROR] Fresh runtime directory '{fresh_sub}' contains unapproved files: {non_empty_files[:5]}\n"
                )
                sys.exit(124)

    # 9B. Independent Actual Snapshot Content & Raw Byte Hashing
    fresh_prefixes = tuple(
        f"{SNAPSHOT_ROOT.as_posix().lower()}/{d.lower()}"
        for d in ("jarvis/skills/custom", "data/evolution_scratch", "data/snapshots", "data/learning_candidates", "temp")
    )
    actual_inventory: dict[str, tuple[int, str]] = {}
    for root, _, files in os.walk(SNAPSHOT_ROOT, followlinks=False):
        root_posix = Path(root).as_posix().lower()
        if any(root_posix == pfx or root_posix.startswith(f"{pfx}/") for pfx in fresh_prefixes):
            continue
        for f in files:
            # Exclude manifests and canonical execution permit lifecycle files
            if f in ("snapshot_manifest.json", "snapshot_integrity_receipt.json") or _is_legitimate_permit_lifecycle_file(f):
                continue
            df = Path(root) / f
            rel_file = df.relative_to(SNAPSHOT_ROOT).as_posix()
            h = hashlib.sha256()
            try:
                with df.open("rb") as fp:
                    while chunk := fp.read(65536):
                        h.update(chunk)
                actual_inventory[rel_file] = (df.stat().st_size, h.hexdigest().lower())
            except OSError as exc:
                _quarantine_permit(claimed_permit_path, f"snapshot_read_failed_{claim_id}")
                sys.stderr.write(
                    f"[BOOTSTRAP AUTHORIZATION ERROR] Failed reading snapshot file {df}: {exc}\n"
                )
                sys.exit(124)

    # Assert destination file count matches approved_file_count in receipt
    approved_count = receipt_data.get("approved_file_count")
    if approved_count is not None and len(actual_inventory) != approved_count:
        _quarantine_permit(claimed_permit_path, f"destination_file_count_mismatch_{claim_id}")
        sys.stderr.write(
            f"[BOOTSTRAP AUTHORIZATION ERROR] Snapshot file count mismatch: actual {len(actual_inventory)} != approved {approved_count}\n"
        )
        sys.exit(124)

    # Compute actual destination inventory digest
    hasher = hashlib.sha256()
    for rel_path in sorted(actual_inventory.keys()):
        sz, sha = actual_inventory[rel_path]
        hasher.update(f"{rel_path}:{sz}:{sha}\n".encode())
    actual_destination_digest = hasher.hexdigest().lower()

    # Compare computed actual destination digest against receipt destination_digest
    if not hmac.compare_digest(receipt_data.get("destination_digest", ""), actual_destination_digest):
        _quarantine_permit(claimed_permit_path, f"destination_digest_mismatch_{claim_id}")
        sys.stderr.write(
            f"[BOOTSTRAP AUTHORIZATION ERROR] Snapshot file tampering detected: destination digest mismatch:\n"
            f"  Actual on disk: {actual_destination_digest}\n"
            f"  In receipt:     {receipt_data.get('destination_digest')}\n"
        )
        sys.exit(124)

    # Recompute composite snapshot digest from components using actual computed destination digest
    src_before_dig = receipt_data.get("source_before_digest", "")
    snap_id = receipt_data.get("snapshot_id", "")
    recomputed_receipt_composite = hashlib.sha256(
        f"{src_before_dig}:{actual_destination_digest}:{snap_id}".encode()
    ).hexdigest().lower()

    if not hmac.compare_digest(receipt_data.get("composite_snapshot_digest", ""), recomputed_receipt_composite):
        _quarantine_permit(claimed_permit_path, f"receipt_composite_tampered_{claim_id}")
        sys.stderr.write(
            "[BOOTSTRAP AUTHORIZATION ERROR] Snapshot receipt composite digest internally inconsistent or tampered.\n"
        )
        sys.exit(124)

    # Cryptographically bind permit tree_digest to verified receipt composite digest
    expected_digest = permit_data.get("tree_digest")
    if not expected_digest or not hmac.compare_digest(expected_digest, recomputed_receipt_composite):
        _quarantine_permit(claimed_permit_path, f"permit_receipt_digest_mismatch_{claim_id}")
        sys.stderr.write(
            f"[BOOTSTRAP AUTHORIZATION ERROR] Permit tree_digest does not match verified snapshot receipt:\n"
            f"  Permit Tree Digest:  {expected_digest}\n"
            f"  Receipt Composite:   {recomputed_receipt_composite}\n"
        )
        sys.exit(124)

    # Bind permit run_id to receipt run_id
    if permit_data.get("run_id") and receipt_data.get("run_id") and permit_data.get("run_id") != receipt_data.get("run_id"):
        _quarantine_permit(claimed_permit_path, f"receipt_run_id_mismatch_{claim_id}")
        sys.stderr.write(
            f"[BOOTSTRAP AUTHORIZATION ERROR] Permit run_id does not match snapshot receipt run_id: {permit_data.get('run_id')} != {receipt_data.get('run_id')}\n"
        )
        sys.exit(124)

    # 10. Execution Profile Validation
    valid_profiles = {
        "Context A (AppContainer / Low Integrity)",
        "Context B1 Ordinary (Non-Elevated Medium Integrity)",
        "Context B1 Snapshot (Disposable Working-Tree Snapshot)",
        "Context B2 Interactive (Dedicated Desktop Station)",
    }
    profile = permit_data.get("profile")
    if profile not in valid_profiles:
        _quarantine_permit(claimed_permit_path, f"invalid_profile_{claim_id}")
        sys.stderr.write(
            f"[BOOTSTRAP AUTHORIZATION ERROR] Invalid execution profile: {profile}\n"
        )
        sys.exit(129)

    # 11. Worker Executable Identity Validation (Path, Version, and Binary SHA-256 Hash)
    expected_exe = permit_data.get("worker_exe_path", "")
    if os.path.realpath(sys.executable).lower() != os.path.realpath(expected_exe).lower():
        _quarantine_permit(claimed_permit_path, f"exe_mismatch_{claim_id}")
        sys.stderr.write(
            f"[BOOTSTRAP AUTHORIZATION ERROR] Worker executable path mismatch: {sys.executable} != {expected_exe}\n"
        )
        sys.exit(131)

    def _compute_exe_sha256(exe_path: Path) -> str:
        h = hashlib.sha256()
        with exe_path.open("rb") as f:
            while chunk := f.read(65536):
                h.update(chunk)
        return h.hexdigest()

    actual_exe_hash = _compute_exe_sha256(Path(sys.executable))
    expected_exe_hash = permit_data.get("worker_exe_hash", "")
    if not expected_exe_hash or not hmac.compare_digest(actual_exe_hash, expected_exe_hash):
        _quarantine_permit(claimed_permit_path, f"exe_hash_mismatch_{claim_id}")
        sys.stderr.write(
            "[BOOTSTRAP AUTHORIZATION ERROR] Worker executable SHA-256 binary digest mismatch.\n"
        )
        sys.exit(131)

    # 12. Independent Canonical Node Inventory Validation (P1-A)
    PINNED_CANONICAL_PROFILE_DIGESTS = {
        "Context A (AppContainer / Low Integrity)": {
            "node_count": 69,
            "inventory_hash": "979c5292ddc8770034a4e78c2fea14ae62735bca90c619dddc11f1749ba2eb4f",
        },
        "Context B1 Ordinary (Non-Elevated Medium Integrity)": {
            "node_count": 420,
            "inventory_hash": "f1e90f7461861e62c785ef07776a510cc8cb07e77303e20159f7424577d7f637",
        },
        "Context B1 Snapshot (Disposable Working-Tree Snapshot)": {
            "node_count": 17,
            "inventory_hash": "e927cebf2fd1a53710116705bf3820f77e1c12dded5f258555a1cb60dbe1d0b6",
        },
        "Context B2 Interactive (Dedicated Desktop Station)": {
            "node_count": 1,
            "inventory_hash": "3ac44a2f1843ce96d22169c5ca2acdc8800020d1be13868284abc0048bdbe248",
        },
    }

    target_nodes = permit_data.get("target_nodes", [])
    node_count = permit_data.get("node_count")

    if node_count != len(target_nodes):
        _quarantine_permit(claimed_permit_path, f"node_count_mismatch_{claim_id}")
        sys.stderr.write(
            f"[BOOTSTRAP AUTHORIZATION ERROR] Node count mismatch: declared {node_count}, actual {len(target_nodes)}.\n"
        )
        sys.exit(135)

    if len(set(target_nodes)) != len(target_nodes):
        _quarantine_permit(claimed_permit_path, f"duplicate_nodes_{claim_id}")
        sys.stderr.write(
            "[BOOTSTRAP AUTHORIZATION ERROR] Duplicate node IDs found in permit inventory.\n"
        )
        sys.exit(136)

    for n in target_nodes:
        if "::" not in n:
            _quarantine_permit(claimed_permit_path, f"broad_file_path_{claim_id}")
            sys.stderr.write(
                f"[BOOTSTRAP AUTHORIZATION ERROR] Target '{n}' is a broad file path; exact canonical pytest node ID required ('::' required).\n"
            )
            sys.exit(137)

    actual_inv_hash = hashlib.sha256("\n".join(sorted(target_nodes)).encode("utf-8")).hexdigest()
    expected_inv_hash = permit_data.get("node_inventory_hash", "")
    if not expected_inv_hash or not hmac.compare_digest(actual_inv_hash, expected_inv_hash):
        _quarantine_permit(claimed_permit_path, f"inventory_hash_mismatch_{claim_id}")
        sys.stderr.write(
            "[BOOTSTRAP AUTHORIZATION ERROR] Node inventory SHA-256 digest mismatch with permit.\n"
        )
        sys.exit(134)

    # Independent verification against pinned canonical partition digest and node count
    pinned_info = PINNED_CANONICAL_PROFILE_DIGESTS.get(profile)
    if pinned_info is not None:
        if node_count != pinned_info["node_count"]:
            _quarantine_permit(claimed_permit_path, f"pinned_node_count_mismatch_{claim_id}")
            sys.stderr.write(
                f"[BOOTSTRAP AUTHORIZATION ERROR] Node count mismatch with pinned canonical partition: declared {node_count}, expected {pinned_info['node_count']}.\n"
            )
            sys.exit(135)
        if not hmac.compare_digest(actual_inv_hash, pinned_info["inventory_hash"]):
            _quarantine_permit(claimed_permit_path, f"pinned_inventory_hash_mismatch_{claim_id}")
            sys.stderr.write(
                f"[BOOTSTRAP AUTHORIZATION ERROR] Node inventory does not match pinned canonical digest for profile {profile}.\n"
            )
            sys.exit(134)

    # 13. Exact Execution Argument Vector Validation (No Generic Synthetic Bypass)
    KNOWN_SYNTHETIC_FLAGS = frozenset({
        "--synthetic-auth-probe",
        "--synthetic-normal",
        "--synthetic-descendant",
        "--synthetic-timeout",
        "--synthetic-held-writer",
        "--synthetic-fail-unlink",
        "--synthetic-fail-consumed-write",
        "--synthetic-ack-wrong-token",
        "--synthetic-ack-wrong-hash",
        "--synthetic-ack-skip",
        "--synthetic-ack-delay",
        "--synthetic-ack-wrong-pid",
        "--synthetic-ack-descendant-impersonation",
        "--synthetic-ack-duplicate",
    })

    # Reject any unrecognized synthetic argument
    for arg in clean_pytest_args:
        if arg.startswith("--synthetic-") and arg not in KNOWN_SYNTHETIC_FLAGS:
            _quarantine_permit(claimed_permit_path, f"unknown_synthetic_arg_{claim_id}")
            sys.stderr.write(
                f"[BOOTSTRAP AUTHORIZATION ERROR] Unrecognized synthetic argument rejected: {arg}\n"
            )
            sys.exit(133)

    # Enforce strict boundary between synthetic failure flags and candidate execution
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
    has_candidate_nodes = any("::" in arg for arg in clean_pytest_args)
    if has_candidate_nodes:
        failure_flags_in_args = [arg for arg in clean_pytest_args if arg in SYNTHETIC_FAILURE_FLAGS]
        if failure_flags_in_args:
            _quarantine_permit(claimed_permit_path, f"synthetic_failure_flag_in_candidate_mode_{claim_id}")
            sys.stderr.write(
                f"[BOOTSTRAP AUTHORIZATION ERROR] Synthetic failure flags {failure_flags_in_args} strictly prohibited in candidate execution mode.\n"
            )
            sys.exit(133)

    expected_vector = permit_data.get("authorized_execution_vector")
    if expected_vector is not None:
        if clean_pytest_args != expected_vector:
            _quarantine_permit(claimed_permit_path, f"vector_mismatch_{claim_id}")
            sys.stderr.write(
                f"[BOOTSTRAP AUTHORIZATION ERROR] Execution argument vector mismatch:\n"
                f"  Expected: {expected_vector}\n"
                f"  Actual:   {clean_pytest_args}\n"
            )
            sys.exit(133)

        # Invariant: ExtractNodes(execution_vector) == target_nodes
        extracted_nodes = [arg for arg in clean_pytest_args if "::" in arg]
        if extracted_nodes and extracted_nodes != target_nodes:
            _quarantine_permit(claimed_permit_path, f"vector_nodes_mismatch_{claim_id}")
            sys.stderr.write(
                f"[BOOTSTRAP AUTHORIZATION ERROR] Nodes in execution vector do not match authorized target_nodes:\n"
                f"  Extracted: {extracted_nodes}\n"
                f"  Target:    {target_nodes}\n"
            )
            sys.exit(132)
    else:
        # Fallback vector verification
        authorized_args = set(permit_data.get("authorized_pytest_args", []))
        target_nodes_set = set(target_nodes)
        for arg in clean_pytest_args:
            if arg in KNOWN_SYNTHETIC_FLAGS:
                continue
            if "::" in arg:
                if arg not in target_nodes_set:
                    _quarantine_permit(claimed_permit_path, f"unauthorized_node_{claim_id}")
                    sys.stderr.write(
                        f"[BOOTSTRAP AUTHORIZATION ERROR] Unauthorized test node: {arg}\n"
                    )
                    sys.exit(132)
            else:
                if arg not in authorized_args:
                    _quarantine_permit(claimed_permit_path, f"unauthorized_extra_arg_{claim_id}")
                    sys.stderr.write(
                        f"[BOOTSTRAP AUTHORIZATION ERROR] Unauthorized extra argument: {arg}\n"
                    )
                    sys.exit(133)

    # 14. Finalize Consumption (Fail-Closed Receipt & Clean Up Claimed Permit) (P1-C)
    consumed_receipt_path = permit_path.with_name(f".execution_permit.consumed.{permit_data['permit_id']}")
    receipt_data = {
        "permit_id": permit_data["permit_id"],
        "run_id": permit_data.get("run_id"),
        "consumed_by_pid": os.getpid(),
        "consumed_at": time.time(),
        "status": "CONSUMED",
    }
    try:
        if "--synthetic-fail-consumed-write" in cli_args:
            raise OSError("[WinError 5] Synthetic injected failure writing consumption receipt")
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        fd = os.open(str(consumed_receipt_path), flags)
        with open(fd, "w", encoding="utf-8") as f:
            json.dump(receipt_data, f, indent=2)
    except OSError as exc:
        _quarantine_permit(claimed_permit_path, f"consumed_marker_write_failed_{claim_id}")
        sys.stderr.write(
            f"[BOOTSTRAP AUTHORIZATION ERROR] Failed to write consumption receipt: {exc}\n"
        )
        sys.exit(138)

    try:
        if "--synthetic-fail-unlink" in cli_args:
            raise OSError("[WinError 32] Synthetic injected file lock on claimed permit")
        claimed_permit_path.unlink()
    except OSError as exc:
        marker_warn = permit_path.with_name(f".execution_permit.unlink_failed.{claim_id}")
        try:
            marker_warn.write_text(f"UNLINK_FAILED={exc}\nCLAIMED_PATH={claimed_permit_path}\n", encoding="utf-8")
        except OSError:
            pass
        sys.stderr.write(
            f"[BOOTSTRAP AUTHORIZATION ERROR] Failed to unlink claimed permit: {exc}\n"
        )
        sys.exit(139)

    # 15. Execution Delegation
    if "--synthetic-auth-probe" in cli_args:
        sys.stdout.write("SYNTHETIC_WORKER_AUTH_VERIFIED\n")
        sys.stdout.flush()
        sys.exit(0)

    # Candidate execution path: fail closed while Gate 2 is unauthorized
    sys.stderr.write("[GATE 2 BLOCKED] Candidate test execution is strictly UNAUTHORIZED.\n")
    sys.exit(200)
