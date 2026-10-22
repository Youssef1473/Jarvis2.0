---
name: python-testing-patterns
description: >-
  Advanced unit testing guidelines, Arrange-Act-Assert (AAA) conventions, mock strategies for Win32 OS handles,
  and leak-free fixture lifecycle management for Jarvis 2.0.
  Activate when authoring, fixing, or optimizing unit tests in the tests/ directory.
---

# Python Testing Patterns & Mocking Standards for Jarvis 2.0

The **Python Testing Patterns** skill defines architectural testing conventions, mock boundaries, and fixture lifecycles across the `tests/` directory of Jarvis 2.0.

Tests in this repository must execute fast, remain deterministic across CPU loads, and never leak OS resources, threads, or file locks.

---

## 1. Arrange-Act-Assert (AAA) Standard

Every unit test must follow the AAA structure clearly separated by single blank lines:

```python
import unittest
from unittest.mock import MagicMock, patch

class TestVolumeAdjustment(unittest.TestCase):
    def test_adjust_volume_within_valid_range(self):
        # 1. Arrange: Setup inputs, mocks, and expected boundaries
        target_volume = 45
        mock_endpoint = MagicMock()
        mock_endpoint.GetMasterVolumeLevelScalar.return_value = 0.30
        
        # 2. Act: Execute the single method under test
        with patch("jarvis.skills.os_control.get_master_volume_endpoint", return_value=mock_endpoint):
            from jarvis.skills.os_control import adjust_volume
            result = adjust_volume(target_volume)
        
        # 3. Assert: Verify post-conditions, mock calls, and return values
        mock_endpoint.SetMasterVolumeLevelScalar.assert_called_once_with(0.45, None)
        self.assertIn("Volume adjusted to 45%", result)
```

---

## 2. Mocking Subsystems & OS Boundaries

### Pattern A: Mocking Win32 API Handles & Ctypes
Do not invoke raw Win32 desktop mutations (e.g., `SetForegroundWindow`, `mouse_event`, `keybd_event`) during unit tests.
* Patch at the `ctypes.windll` or module import level:
```python
@patch("ctypes.windll.user32.GetForegroundWindow", return_value=12345)
@patch("ctypes.windll.user32.GetWindowTextW")
def test_active_window_detection(self, mock_get_text, mock_get_hwnd):
    # Setup ctypes string buffer output
    def fill_buffer(hwnd, buf, max_count):
        buf.value = "Visual Studio Code"
        return len(buf.value)
    mock_get_text.side_effect = fill_buffer

    from jarvis.skills.windows_uia import get_active_window_title
    title = get_active_window_title()
    self.assertEqual(title, "Visual Studio Code")
```

---

### Pattern B: Mocking Subprocesses & Anonymous Pipes
When testing classes that manage background subprocesses (`AntigravityDaemon`, `ManagedServiceManager`):
* Always mock `subprocess.Popen` to prevent spawning real child processes.
* Provide mock pipe handles supporting standard integer fileno methods:
```python
def test_daemon_lifecycle(self):
    mock_proc = MagicMock()
    mock_proc.poll.return_value = None
    mock_proc.pid = 9999
    mock_proc.stdin = MagicMock()
    mock_proc.stdout = MagicMock()
    mock_proc.stdout.fileno.return_value = 100

    with patch("subprocess.Popen", return_value=mock_proc):
        # Test creation, health check, and termination
        pass
```

---

### Pattern C: Testing Starlette Async Routes & WebSockets
Test UI server endpoints and WebSockets synchronously using `starlette.testclient.TestClient`:
```python
from starlette.testclient import TestClient
from jarvis.ui.server import app

class TestHUDWebSocket(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_websocket_telemetry_stream(self):
        with self.client.websocket_connect("/ws/telemetry") as websocket:
            data = websocket.receive_json()
            self.assertEqual(data.get("type"), "system_init")
```

---

### Pattern D: Patching Properties Without Deleters
Never use `unittest.mock.patch.object` on a `@property` that does not define a deleter (`@prop.deleter`). During test unpatching, `mock` attempts `delattr()`, raising `AttributeError: property 'name' of 'Class' object has no deleter`.
* Use explicit assignment with `try...finally` restoration:

```python
orig_daemon = bridge.daemon
bridge.daemon = mock_daemon
try:
    result = bridge.execute_turn("query")
    self.assertIn("response", result)
finally:
    bridge.daemon = orig_daemon
```

---

### Pattern E: Network Isolation & Deterministic Test Doubles
Standard unit tests must never perform live HTTP queries or DNS lookups.
* Mock `requests.get` or external API clients using URL-matching dispatchers:

```python
def _mock_requests_get(url, *args, **kwargs):
    resp = MagicMock()
    if "open-meteo.com" in url:
        resp.json.return_value = {"current_weather": {"temperature": 15.0, "windspeed": 10.0}}
    else:
        resp.status_code = 200
        resp.text = "OK"
    return resp

with patch("requests.get", side_effect=_mock_requests_get):
    res = SkillRegistry.execute("get_weather", city="London")
    assert "Weather in London" in res
```
* Gate any truly live hardware/provider smoke tests behind environment variables (e.g. `JARVIS_RUN_LIVE_INTEGRATION=1`) and skip explicitly when unset.

---

## 3. Fixture Lifecycles & Resource Leak Prevention

### Use `addCleanup` Instead of `tearDown`
`addCleanup` guarantees that registered cleanup callbacks run even if `setUp()` raises an exception midway through setup.

```python
def setUp(self):
    # Setup temporary directory for test database
    self.test_dir = tempfile.mkdtemp()
    self.addCleanup(shutil.rmtree, self.test_dir, ignore_errors=True)
    
    # Setup database
    self.db_path = os.path.join(self.test_dir, "test.db")
    self.memory = MemoryManager(db_path=self.db_path)
    self.addCleanup(self.memory.close)
```

---

## 4. Real-World Failure Modes & Mitigations

### Failure Mode 1: Windows File Lock Contention (`WinError 32`)
* **Symptom:** `PermissionError: [WinError 32] The process cannot access the file because it is being used by another process` during `tempfile.TemporaryDirectory` teardown.
* **Root Cause:**
  1. An open SQLite connection exists in the main thread or an idle background worker thread.
  2. CPython on Windows has not yet garbage-collected connection handles before directory removal.
* **Mitigation:**
  - Background workers consuming from queues must connect per transaction and close handles upon completion rather than keeping idle connections open.
  - In tests, replace arbitrary `time.sleep()` waits with deterministic `queue.join()` on worker queues.
  - Always call `gc.collect()` before `temp_dir.cleanup()` in `tearDown()` to ensure handles are released by the OS:
  ```python
  def tearDown(self):
      self.worker_queue.join()
      self.memory.close()
      import gc
      gc.collect()
      self.temp_dir.cleanup()
  ```

### Failure Mode 2: Over-Mocking (False Green Tests)
* **Symptom:** Unit tests pass with 100% green status, but the feature crashes in production.
* **Root Cause:** The test mocked the internal helper method being tested rather than the external OS/network boundary, causing the test to verify only the mock's return value.
* **Mitigation:**
  - **Rule:** Never mock the method under test or internal private methods of the class being tested.
  - Only mock external boundaries: hardware (soundcard, microphone), OS APIs (`ctypes.windll`, `win32gui`), and network (HTTP requests, LLM cloud APIs).

### Failure Mode 3: Global State Cross-Contamination
* **Symptom:** Test A succeeds in isolation, but fails when Test B runs before it.
* **Root Cause:** Test B mutated a class-level variable, global dictionary (like `SkillRegistry._skills`), or environment variable without resetting it.
* **Mitigation:**
  - Save and restore global state in `setUp()` / `addCleanup()`.
  - For environment variables, use `unittest.mock.patch.dict(os.environ, {"KEY": "VALUE"})`.

### Failure Mode 4: Ambient Persistent State Assumptions
* **Symptom:** Tests pass on developer workstations with prior database history, but fail in clean CI checkouts with `AssertionError: Expected non-empty recent interactions`.
* **Root Cause:** Tests implicitly connect to `data/jarvis_memory.db` instead of an isolated temporary fixture.
* **Mitigation:**
  - Never allow tests to read from or write to `data/jarvis_memory.db`.
  - Always point `config["memory"]["db_path"]` to `tempfile.TemporaryDirectory()`.
  - Explicitly seed baseline records inside the test before asserting on query or recall outputs.

### Failure Mode 5: Hardware-Dependent Schema Incompleteness
* **Symptom:** Skills return full metadata on developer GPU workstations, but fail schema or key-existence assertions in headless CI runners.
* **Root Cause:** Skill return dictionaries omit optional fields when returning `unavailable` or `error`.
* **Mitigation:**
  - Standardize dictionary return schemas so all structural keys exist across all states:
  ```python
  # Always include default structures even on failure/unavailability
  return {
      "status": "unavailable",
      "reason": "GPU hardware not present",
      "vram_total_mb": 0.0,
      "compute_processes": [],
      "gpu_temp_c": None,
  }
  ```
