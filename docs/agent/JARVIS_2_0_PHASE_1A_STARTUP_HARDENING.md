# JARVIS 2.0 — Phase 1A: Resilient Startup & Antigravity Discovery

**Milestone:** JARVIS-2.0 End-to-End Product Readiness  
**Phase:** Phase 1A — Resilient Startup & Antigravity Discovery  
**Date:** 2026-11-08  
**Working Directory:** `C:\Users\Youssef\Projects\Jarvis_2.0`  
**Git Branch:** `feat/engineering-maturity-milestone` at `be8d39f58e4ca78f173f7b8bf59fde49fc1c90db`  
**Gate 2 Status:** **STRICTLY UNAUTHORIZED** (0/507 candidate test nodes executed; 0 pytest collections)  
**Modified Tracked Files:** Exactly 63 baseline tracked files preserved  

---

## 1. Executive Summary

Phase 1A resolves the critical startup blocker identified during initial product evaluation where probing `%LOCALAPPDATA%\agy\bin\agy.exe` raised an unhandled `PermissionError: [WinError 5] Access is denied`, causing immediate process abort during `JarvisBrain` instantiation on `run.py`.

The startup subsystem has been hardened to:
1. Safely inspect candidate executables across PATH, `%LOCALAPPDATA%`, and `~/.gemini/` without crashing on filesystem or Windows security exceptions.
2. Maintain an explicit 4-way availability contract (`VALID`, `NOT_FOUND`, `INACCESSIBLE`, `INVALID`).
3. Make the Antigravity bridge entirely optional so `JarvisBrain` and `JarvisOrchestrator` initialize reliably in degraded or headless environments.
4. Provide structured provider availability inspection without conflating Antigravity CLI sessions with direct Gemini API credentials.
5. Guarantee that sub-10ms deterministic reflexes (time, date, battery, system vitals) remain fully operational when AI reasoning providers are offline.

All 11 hermetic unit tests pass with zero host environment leakage, health check reports `HEALTHY`, and live startup/shutdown probes confirm sub-5ms reflex dispatch and clean teardown.

---

## 2. Root Cause Analysis

### 2.1 The Failure Mechanism
During `JarvisBrain.__init__` initialization on Windows, `_init_antigravity()` invoked `find_agy_executable()`. The legacy implementation used:
```python
candidate = Path(local_app_data) / "agy" / "bin" / "agy.exe"
if candidate.exists() and validate_agy_executable(candidate):
    return str(candidate)
```
On Windows 11, `%LOCALAPPDATA%\agy\bin\agy.exe` is associated with restricted DACLs or AppExecutionAlias handles. When probed with standard `Path.exists()`, the Windows kernel returns `ERROR_ACCESS_DENIED` (WinError 5). Because `find_agy_executable()` lacked exception handlers for `PermissionError` and `OSError`, the exception propagated unhandled up through `JarvisBrain` and aborted application startup on `run.py` line 73.

### 2.2 System Invariant Defect
1. **Unconditional Bridge Coupling:** `JarvisBrain` tightly coupled cognitive initialization to the physical presence and launchability of `agy.exe`.
2. **Missing Discovery Isolation:** Probing candidate paths did not catch permission boundaries or corrupted filesystem attributes.
3. **Implicit Provider Assumption:** Absence of a reasoning provider resulted in unhandled exceptions rather than structured fallback and polite user-facing diagnostic explanations.

---

## 3. Implementation Details

### 3.1 Hardened Executable Discovery (`jarvis/core/antigravity_bridge.py`)
Introduced `inspect_candidate_executable(candidate: str | Path | None) -> tuple[bool, str, str | None]` to perform non-crashing inspection:
- Wraps `Path.exists()`, `Path.is_dir()`, `Path.is_file()`, and `Path.resolve()` in dedicated `try...except (PermissionError, FileNotFoundError, OSError)` blocks.
- Verifies Windows-specific executable extensions (`.exe`, `.cmd`, `.bat`) and rejects directories or invalid extensions.
- Probes read accessibility via `os.access(..., os.R_OK)` without executing binaries.
- Returns an explicit 3-tuple `(is_valid: bool, status: str, detail_or_path: str | None)`.

Updated `find_agy_executable(diagnostics: list[dict[str, Any]] | None = None) -> str | None`:
- Evaluates candidates in priority order:
  1. System `PATH` via `shutil.which("agy")` / `shutil.which("agy.exe")`.
  2. `%LOCALAPPDATA%/agy/bin/agy.exe`.
  3. `~/.gemini/antigravity-cli/bin/agy.exe`.
- Traverses candidates safely; if a candidate returns `INACCESSIBLE` or `INVALID`, it records detailed diagnostic records and proceeds to remaining candidates rather than aborting.

### 3.2 Optional Antigravity Bridge (`jarvis/core/antigravity_bridge.py`)
Hardened `AntigravityBridge.__init__`:
- Accepts optional `executable_resolver` callback and `agy_path` override for dependency injection.
- If no valid executable candidate is discovered or if discovery raises `(OSError, PermissionError)`, `self.agy_path` is set to `None`, no background `AntigravityDaemon` is spawned, and the bridge sets:
  - `self.availability_status`: `"NOT_FOUND"`, `"INACCESSIBLE"`, or `"INVALID"`.
  - `self.availability_diagnostic`: Explanatory technical diagnostic string.
- `is_available()` returns `False` when `agy_path` is `None` or `availability_status != "AVAILABLE"`.
- `get_or_spawn_daemon()` and `generate_response()` fail closed safely returning structured error payloads (`status: "ERROR"`, `error_type: "UNAVAILABLE"`).

### 3.3 Provider Availability & Degradation Contract (`jarvis/core/brain.py` & `jarvis/core/brain_antigravity.py`)
1. **Separation of Reasoning vs Reflexes:**
   - Deterministic reflexes (time, date, battery, system status) handled by `ReflexDispatcher` execute at zero token cost and sub-10ms latency regardless of cloud provider status.
2. **Graceful Degraded Explanation:**
   - In `_process_antigravity_query()`, if `antigravity_bridge.is_available()` is `False`, Jarvis returns a polite conversational response explaining that the configured reasoning provider (`'antigravity'`) is unavailable along with the exact diagnostic status, preventing silent deadlocks.
3. **Structured Provider Status (`get_provider_status()`):**
   - Added `JarvisBrain.get_provider_status() -> dict[str, Any]` reporting individual operational readiness for `antigravity`, `gemini`, `github`, `ollama`, and `openrouter`.
   - Distinct credential separation: Antigravity CLI Pro subscription status and `GEMINI_API_KEY` status are audited and reported independently.

---

## 4. Verification & Empirical Results

### 4.1 Targeted Unit Test Suite (`tests/test_antigravity_executable_resolver.py`)
Executed hermetic unit test suite covering all 9 required verification scenarios with zero host access:

| Test ID | Test Case | Target Behavior | Empirical Result | Status |
|:---|:---|:---|:---|:---:|
| `test_1` | Valid Executable Discovered | PATH hit or LOCALAPPDATA fallback hit; returns resolved path | Discovered and validated | **PASSED** |
| `test_2` | Executable Not Found | Candidates missing; returns `None`, bridge sets `NOT_FOUND` | Clean unavailable state | **PASSED** |
| `test_3` | Path PermissionError | `PermissionError(WinError 5)` caught; status `INACCESSIBLE` | Zero crash, detailed diagnostic | **PASSED** |
| `test_4` | Inaccessible Parent Directory | Parent traversal error handled; status `INACCESSIBLE` | Zero crash, detailed diagnostic | **PASSED** |
| `test_5` | Invalid Candidates Rejected | Directories, non-executable extensions rejected; status `INVALID` | Rejected without exception | **PASSED** |
| `test_6` | Candidate Recovery | Earlier candidate fails with `PermissionError`, later candidate succeeds | Successfully recovers later valid | **PASSED** |
| `test_7` | Brain Degradation | Antigravity unavailable; reflexes execute, queries explain status | Reflexes work (<10ms), fallback active | **PASSED** |
| `test_8` | Normal Bridge Initialization | Valid executable sets `is_available=True`, status `AVAILABLE` | Bridge ready | **PASSED** |
| `test_9` | Multi-Provider Status | `get_provider_status()` reports status for all providers | Accurate structured dictionary | **PASSED** |
| `test_10` | Dependency Injection | Resolver injected via constructor or config dictionary | Custom resolver respected | **PASSED** |
| `test_11` | Unit Test Isolation | Injected resolver guarantees zero calls to host `shutil.which` or disk | Zero host disk access | **PASSED** |

**Execution Evidence:**
```powershell
Command: ..\.venv\Scripts\python.exe -m unittest tests/test_antigravity_executable_resolver.py
Ran 11 tests in 2.503s
OK (exit code 0)
```

### 4.2 System Health Verification
Executed canonical health check tool:
```powershell
Command: ..\.venv\Scripts\python.exe run.py --check-health
=================================================================
        JARVIS 2.0 LIVE OPERATIONAL SUBSYSTEM DIAGNOSTICS        
=================================================================
  [AUDIO HARDWARE ] : ONLINE (Zero buffer underruns)
    • Microphone  : [1] Headset Microphone (Realtek(R)  (16kHz stream OK)
    • Output Spk  : [6] Headphone (Realtek(R) Audio) (24kHz stream OK)
  [GPU ACCELERATOR] : ONLINE (RTX 3050 | 3808 MB free / 4096 MB | 49°C)
  [NEURAL WEIGHTS ] : VERIFIED (Kokoro-82M ONNX, BGE Embeddings, Audio SFX)
  [MEMORY DATABASE] : PRISTINE (WAL Mode | Integrity: PASSED | 57 Tables | sqlite-vec: vv0.1.9)
  [SKILLS REGISTRY] : VERIFIED (3/3 smoke checks passed | 146 tools registered)
  [DAEMON PORTS   ] : HUD (20129): STANDBY_READY | CDP (9222): STANDBY_OFFLINE | Ollama (11434): STANDBY_OFFLINE
  [COGNITIVE CORE ] : ONLINE (Provider: antigravity | Gemini Key: CONFIGURED | Tiering: fast/deep)
=================================================================
  OVERALL OPERATIONAL STATUS: HEALTHY (exit code 0)
=================================================================
```

### 4.3 Live Startup, Query & Shutdown Probe
Executed probe script instantiating `JarvisBrain` and `JarvisOrchestrator` under live runtime conditions:
```powershell
Command: ..\.venv\Scripts\python.exe -c "<probe_script>"
Antigravity CLI bridge unavailable (INACCESSIBLE): Antigravity candidate was inaccessible: Access denied (WinError 5 / PermissionError): [WinError 5] Access is denied: 'C:\Users\Youssef\AppData\Local\agy\bin\agy.exe'
Antigravity CLI ('agy') is unavailable (INACCESSIBLE: Antigravity candidate was inaccessible: Access denied (WinError 5 / PermissionError): [WinError 5] Access is denied: 'C:\Users\Youssef\AppData\Local\agy\bin\agy.exe').
[PROBE] Config loaded, provider: antigravity
[PROBE] Initializing JarvisBrain...
[PROBE] JarvisBrain initialized successfully!
[PROBE] Bridge available: False
[PROBE] Bridge status: INACCESSIBLE
[PROBE] Bridge diagnostic: Antigravity candidate was inaccessible: Access denied (WinError 5 / PermissionError): [WinError 5] Access is denied: 'C:\Users\Youssef\AppData\Local\agy\bin\agy.exe'
[PROBE] Provider status active: antigravity
[PROBE] Provider details: {
  "antigravity": {
    "available": false,
    "status": "INACCESSIBLE",
    "diagnostic": "Antigravity candidate was inaccessible: Access denied (WinError 5 / PermissionError): [WinError 5] Access is denied: 'C:\\Users\\Youssef\\AppData\\Local\\agy\\bin\\agy.exe'"
  },
  "gemini": {
    "available": true,
    "status": "CONFIGURED"
  },
  "github": {
    "available": true,
    "status": "AVAILABLE"
  },
  "ollama": {
    "available": false,
    "status": "OFFLINE"
  },
  "openrouter": {
    "available": false,
    "status": "NO_API_KEY"
  }
}
[PROBE] Testing deterministic reflex...
[PROBE] Reflex latency: 4.51ms
[PROBE] Reflex response: Today is Sunday, the 8th of November, Sir. The time is seven oh 2 in the evening.
[PROBE] Testing reasoning query fallback...
[PROBE] Reasoning response: My apologies, Sir. My primary reasoning provider ('antigravity') is unavailable (INACCESSIBLE). Antigravity candidate was inaccessible: Access denied (WinError 5 / PermissionError): [WinError 5] Access is denied: 'C:\Users\Youssef\AppData\Local\agy\bin\agy.exe'
[PROBE] Testing brain shutdown...
[PROBE] Shutdown complete! (exit code 0)
```

Probe verifying full orchestrator initialization and teardown:
```powershell
Command: ..\.venv\Scripts\python.exe -c "<orchestrator_probe>"
[PROBE] Initializing JarvisOrchestrator...
[PROBE] Orchestrator initialized successfully!
[PROBE] Brain provider status: INACCESSIBLE
[PROBE] Stopping orchestrator...
[PROBE] Orchestrator stopped cleanly! (exit code 0)
```

---

## 5. Working Tree & Gate Status

- **Gate 2 Status:** **STRICTLY UNAUTHORIZED**. Candidate test nodes executed: `0/507`. Pytest discovery invocations: `0`.
- **Git Modifications:** Exactly 63 baseline tracked files preserved. Zero staging, commits, branches, or resets executed.
- **Untracked Additions:**
  - `tests/test_antigravity_executable_resolver.py` (Hermetic unit test suite).
  - `docs/agent/JARVIS_2_0_PHASE_1A_STARTUP_HARDENING.md` (This deliverable report).

---

## 6. Next Phase: Phase 2 Readiness

With the application startup crash resolved and cognitive degradation cleanly managed, the path is clear for **Phase 2: Acoustic Loop & Live Sensory Verification**:
1. **Kokoro TTS ONNX Pipeline:** Diagnose and fix the voice synthesis failure (`AttributeError: 'NoneType' object has no attribute 'shape'` / tokenizer initialization) identified during preliminary assessment.
2. **End-to-End Audio Loop:** Validate microphone input (16kHz WASAPI stream) through openWakeWord ("Hey Jarvis"), faster-whisper transcription, reflex/cognitive dispatch, and Kokoro speech output (24kHz WASAPI playback).
3. **Live Gemini Provider Integration:** Verify live inference through the configured `GEMINI_API_KEY` when Antigravity CLI is unavailable.

