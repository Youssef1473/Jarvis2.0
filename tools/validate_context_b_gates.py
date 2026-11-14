"""Independent Acceptance Gate Validator for Context B Telemetry (ENG-OS-03C8).

Evaluates data/execution_evidence/env_probe_context_b.json against the five
mandatory acceptance criteria established in ENG-OS-03C8.
Strictly returns PASS, FAIL, or INCONCLUSIVE for each gate.
Enforces strict JSON data typing:
- Integers reject boolean, string, or float representations (type is int).
- Booleans require actual JSON booleans (type is bool).
- Strings require actual strings (type is str).
Never silently accepts missing fields, malformed data, or incorrect types.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Tuple

EXPECTED_PYTHON_EXE = r"C:\Users\Youssef\AppData\Local\Programs\Python\Python312\python.exe"
EXPECTED_PYTHON_VER = "3.12.10"
EXPECTED_MACHINE = "AMD64"
EXPECTED_POINTER_BITS = 64


def is_exact_int(val: Any) -> bool:
    """Returns True iff val is an int and NOT a bool."""
    return type(val) is int


def is_exact_bool(val: Any) -> bool:
    """Returns True iff val is a bool."""
    return type(val) is bool


def is_exact_str(val: Any) -> bool:
    """Returns True iff val is a str."""
    return type(val) is str


def evaluate_gate_1(tok: dict[str, Any]) -> Tuple[str, str]:
    """Gate 1: Container Status & Primary Token Identity."""
    if not isinstance(tok, dict):
        return "INCONCLUSIVE", f"Invalid token_security section type: {type(tok).__name__}"

    is_app = tok.get("TokenIsAppContainer")
    tok_type = tok.get("TokenType")
    thread_imp = tok.get("ThreadImpersonationTokenPresent")

    if is_app is None or tok_type is None:
        return "INCONCLUSIVE", f"Missing fields: TokenIsAppContainer={is_app}, TokenType={tok_type}"

    # Strict type checks
    if not is_exact_int(is_app):
        return "INCONCLUSIVE", f"Invalid type for TokenIsAppContainer: {type(is_app).__name__} (expected int)"

    if not is_exact_int(tok_type):
        return "INCONCLUSIVE", f"Invalid type for TokenType: {type(tok_type).__name__} (expected int)"

    if thread_imp is not None and not is_exact_bool(thread_imp):
        return "INCONCLUSIVE", f"Invalid type for ThreadImpersonationTokenPresent: {type(thread_imp).__name__} (expected bool)"

    # Target: Process primary token (type 1), TokenIsAppContainer == 0, no thread impersonation
    if is_app == 0 and tok_type == 1 and (thread_imp is False or thread_imp is None):
        return "PASS", f"Process Primary Token (TokenType=1, AppContainer=0, ThreadImpersonation={thread_imp})"

    if is_app != 0:
        return "FAIL", f"TokenIsAppContainer={is_app} (AppContainer SID: {tok.get('AppContainerSid')})"

    if tok_type != 1:
        return "FAIL", f"TokenType={tok_type} (Not Process Primary Token; Impersonation token detected)"

    if thread_imp is True:
        return "FAIL", "Thread impersonation token present; caller is not executing as process primary token"

    return "INCONCLUSIVE", f"Unrecognized combination: AppContainer={is_app}, TokenType={tok_type}"


def evaluate_gate_2(tok: dict[str, Any]) -> Tuple[str, str]:
    """Gate 2: Integrity Level (0x2000 Medium)."""
    if not isinstance(tok, dict):
        return "INCONCLUSIVE", f"Invalid token_security section type: {type(tok).__name__}"

    rid = tok.get("IntegrityLevelRID")
    name = tok.get("IntegrityLevelName", "Unknown")

    if rid is None:
        return "INCONCLUSIVE", "IntegrityLevelRID unmeasured or absent"

    # Strict type check: RID must be a string like "0x2000"
    if not is_exact_str(rid):
        return "INCONCLUSIVE", f"Invalid type for IntegrityLevelRID: {type(rid).__name__} (expected str)"

    if name is not None and not is_exact_str(name):
        return "INCONCLUSIVE", f"Invalid type for IntegrityLevelName: {type(name).__name__} (expected str)"

    if rid.upper() == "0X2000":
        return "PASS", f"{rid} ({name})"

    if rid.upper() in ("0X0000", "0X1000", "0X3000", "0X4000"):
        return "FAIL", f"Integrity level mismatch: {rid} ({name}) != 0x2000 (Medium)"

    return "INCONCLUSIVE", f"Unknown integrity RID: {rid} ({name})"


def evaluate_gate_3(tok: dict[str, Any]) -> Tuple[str, str]:
    """Gate 3: Direct Elevation State (TokenIsElevated == 0).

    Decisive condition: TokenIsElevated == 0.
    TokenElevationType is recorded as supplementary telemetry per Microsoft Learn
    (1 = Default [no linked token], 2 = Full [elevated], 3 = Limited [limited token]),
    without equating elevation type directly to user account membership.
    """
    if not isinstance(tok, dict):
        return "INCONCLUSIVE", f"Invalid token_security section type: {type(tok).__name__}"

    elev = tok.get("TokenIsElevated")
    elev_type = tok.get("TokenElevationType")
    elev_name = tok.get("TokenElevationTypeName", "Unknown")

    if elev is None:
        return "INCONCLUSIVE", "TokenIsElevated unmeasured or absent"

    # Strict type checks
    if not is_exact_int(elev):
        return "INCONCLUSIVE", f"Invalid type for TokenIsElevated: {type(elev).__name__} (expected int)"

    if elev_type is not None and not is_exact_int(elev_type):
        return "INCONCLUSIVE", f"Invalid type for TokenElevationType: {type(elev_type).__name__} (expected int)"

    if elev_name is not None and not is_exact_str(elev_name):
        return "INCONCLUSIVE", f"Invalid type for TokenElevationTypeName: {type(elev_name).__name__} (expected str)"

    if elev == 0:
        # Documented Microsoft definitions:
        if elev_type == 1:
            elev_desc = "TokenElevationTypeDefault (Token has no linked token)"
        elif elev_type == 3:
            elev_desc = "TokenElevationTypeLimited (Limited token)"
        else:
            elev_desc = f"TokenElevationType={elev_type} ({elev_name})"

        # Supplementary group and linked token evidence
        supp = []
        has_linked = tok.get("HasLinkedToken")
        if has_linked is not None:
            if not is_exact_bool(has_linked):
                return "INCONCLUSIVE", f"Invalid type for HasLinkedToken: {type(has_linked).__name__} (expected bool)"
            supp.append(f"LinkedToken={has_linked}")

        has_admin = tok.get("HasAdminGroupSid")
        if has_admin is not None:
            if not is_exact_bool(has_admin):
                return "INCONCLUSIVE", f"Invalid type for HasAdminGroupSid: {type(has_admin).__name__} (expected bool)"
            supp.append(f"AdminGroupSid={has_admin} [{tok.get('AdminGroupAttributes')}]")

        extra_str = f" | Supplementary: {', '.join(supp)}" if supp else ""
        return "PASS", f"TokenIsElevated=0 (Decisive non-elevation confirmed; ElevationType: {elev_desc}{extra_str})"

    if elev == 1:
        return "FAIL", f"TokenIsElevated=1 (Process is Elevated, ElevationType={elev_name})"

    return "INCONCLUSIVE", f"Unexpected TokenIsElevated value: {elev}"


def evaluate_gate_4(py: dict[str, Any]) -> Tuple[str, str]:
    """Gate 4: Exact Python Interpreter, Version, and Machine Architecture.

    Verifies:
    - sys.executable exact normalized path match
    - CPython 3.12.10 version match
    - Machine type == AMD64
    - Pointer bits == 64
    - 64-bit interpreter (sys.maxsize > 2**32)
    """
    if not isinstance(py, dict):
        return "INCONCLUSIVE", f"Invalid python section type: {type(py).__name__}"

    actual_exe = py.get("executable")
    ver = py.get("version")
    machine = py.get("machine")
    pointer_bits = py.get("pointer_bits")
    is_64bit = py.get("is_64bit_interpreter")

    if actual_exe is None or ver is None:
        return "INCONCLUSIVE", f"Missing Python metadata: exe={actual_exe}, ver={ver}"

    if not is_exact_str(actual_exe):
        return "INCONCLUSIVE", f"Invalid type for executable: {type(actual_exe).__name__} (expected str)"

    if not is_exact_str(ver):
        return "INCONCLUSIVE", f"Invalid type for version: {type(ver).__name__} (expected str)"

    # Machine architecture checks
    if machine is None or pointer_bits is None or is_64bit is None:
        return "INCONCLUSIVE", f"Missing machine architecture fields: machine={machine}, pointer_bits={pointer_bits}, is_64bit={is_64bit}"

    if not is_exact_str(machine):
        return "INCONCLUSIVE", f"Invalid type for machine: {type(machine).__name__} (expected str)"

    if not is_exact_int(pointer_bits):
        return "INCONCLUSIVE", f"Invalid type for pointer_bits: {type(pointer_bits).__name__} (expected int)"

    if not is_exact_bool(is_64bit):
        return "INCONCLUSIVE", f"Invalid type for is_64bit_interpreter: {type(is_64bit).__name__} (expected bool)"

    norm_actual = os.path.normcase(os.path.normpath(actual_exe))
    norm_expected = os.path.normcase(os.path.normpath(EXPECTED_PYTHON_EXE))

    path_ok = (norm_actual == norm_expected)
    ver_ok = EXPECTED_PYTHON_VER in ver
    mach_ok = (machine.upper() == EXPECTED_MACHINE)
    bits_ok = (pointer_bits == EXPECTED_POINTER_BITS)
    is64_ok = (is_64bit is True)

    if path_ok and ver_ok and mach_ok and bits_ok and is64_ok:
        return "PASS", f"CPython {EXPECTED_PYTHON_VER} {EXPECTED_MACHINE} (PointerBits={pointer_bits}, maxsize>2^32=True) at {actual_exe}"

    mismatches = []
    if not path_ok:
        mismatches.append(f"Executable: '{actual_exe}' != '{EXPECTED_PYTHON_EXE}'")
    if not ver_ok:
        mismatches.append(f"Version: '{ver}' does not contain '{EXPECTED_PYTHON_VER}'")
    if not mach_ok:
        mismatches.append(f"Machine: '{machine}' != '{EXPECTED_MACHINE}'")
    if not bits_ok:
        mismatches.append(f"PointerBits: {pointer_bits} != {EXPECTED_POINTER_BITS}")
    if not is64_ok:
        mismatches.append(f"is_64bit_interpreter: {is_64bit} != True")

    return "FAIL", "; ".join(mismatches)


def evaluate_gate_5(tok: dict[str, Any]) -> Tuple[str, str]:
    """Gate 5: Session, Window Station & Thread Desktop."""
    if not isinstance(tok, dict):
        return "INCONCLUSIVE", f"Invalid token_security section type: {type(tok).__name__}"

    sess = tok.get("SessionId")
    winsta = tok.get("WindowStation")
    desk = tok.get("Desktop")

    if sess is None or winsta is None or desk is None:
        return "INCONCLUSIVE", f"Missing environment values: SessionId={sess}, WindowStation={winsta}, Desktop={desk}"

    if not is_exact_int(sess):
        return "INCONCLUSIVE", f"Invalid type for SessionId: {type(sess).__name__} (expected int)"

    if not is_exact_str(winsta):
        return "INCONCLUSIVE", f"Invalid type for WindowStation: {type(winsta).__name__} (expected str)"

    if not is_exact_str(desk):
        return "INCONCLUSIVE", f"Invalid type for Desktop: {type(desk).__name__} (expected str)"

    sess_ok = (sess == 1)
    winsta_ok = (winsta.lower() == "winsta0")
    desk_ok = (desk.lower() == "default")

    if sess_ok and winsta_ok and desk_ok:
        return "PASS", f"Session {sess}, WindowStation '{winsta}', Desktop '{desk}'"

    mismatches = []
    if not sess_ok:
        mismatches.append(f"SessionId: {sess} != 1")
    if not winsta_ok:
        mismatches.append(f"WindowStation: '{winsta}' != 'WinSta0'")
    if not desk_ok:
        mismatches.append(f"Desktop: '{desk}' != 'Default'")

    return "FAIL", "; ".join(mismatches)


def audit_telemetry_file(json_path: Path | str) -> Tuple[str, list[Tuple[str, str, str]]]:
    """Audits telemetry JSON against all 5 gates.

    Returns (overall_verdict_string, results_list).
    """
    target = Path(json_path)
    if not target.exists():
        return "INCONCLUSIVE — Context B telemetry file does not exist.", [
            ("File Existence", "INCONCLUSIVE", f"Telemetry file absent: {target}")
        ]

    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except Exception as e:
        return "INCONCLUSIVE — Telemetry file is malformed JSON.", [
            ("JSON Parsing", "INCONCLUSIVE", f"Failed parsing JSON: {e}")
        ]

    if not isinstance(data, dict):
        return "INCONCLUSIVE — Telemetry root must be a JSON object.", [
            ("JSON Root Structure", "INCONCLUSIVE", f"Expected object, got {type(data).__name__}")
        ]

    tok = data.get("token_security", {})
    py = data.get("python", {})

    gates = [
        ("Gate 1: Container Status & Primary Token", *evaluate_gate_1(tok)),
        ("Gate 2: Integrity Level (0x2000 Medium)", *evaluate_gate_2(tok)),
        ("Gate 3: Elevation State (TokenIsElevated=0)", *evaluate_gate_3(tok)),
        ("Gate 4: Exact Python Identity & Architecture", *evaluate_gate_4(py)),
        ("Gate 5: Session & Desktop (1, WinSta0\\Default)", *evaluate_gate_5(tok)),
    ]

    any_fail = any(status == "FAIL" for _, status, _ in gates)
    any_inconclusive = any(status == "INCONCLUSIVE" for _, status, _ in gates)
    all_pass = all(status == "PASS" for _, status, _ in gates)

    if all_pass:
        verdict = "ACCEPTED — Telemetry meets all 5 control requirements"
    elif any_fail:
        verdict = "REJECTED — Telemetry violates one or more control conditions"
    else:
        verdict = "INCONCLUSIVE — Telemetry missing required fields, malformed, or invalid types"

    return verdict, gates


def main() -> int:
    parser = argparse.ArgumentParser(description="Context B Acceptance Gate Validator")
    parser.add_argument(
        "json_path",
        nargs="?",
        default="data/execution_evidence/env_probe_context_b.json",
        help="Path to context telemetry JSON file",
    )
    args = parser.parse_args()

    target = Path(args.json_path)
    print("=" * 80)
    print(f" CONTEXT B TELEMETRY ACCEPTANCE AUDIT: {target}")
    print("=" * 80)

    verdict, results = audit_telemetry_file(target)

    for name, status, detail in results:
        print(f"  [{status:12}] {name}")
        print(f"                 Evidence: {detail}")

    print("-" * 80)
    print(f"FINAL AUDIT VERDICT: {verdict}")
    print("=" * 80)

    if verdict.startswith("ACCEPTED"):
        return 0
    elif verdict.startswith("REJECTED"):
        return 1
    else:
        return 2


if __name__ == "__main__":
    sys.exit(main())
