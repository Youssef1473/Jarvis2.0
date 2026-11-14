# Jarvis 2.0 — Engineering Lessons & Post-Mortem Registry

> [!IMPORTANT]
> **Evidence-Gated Entry Contract:** This document is an auditable repository of verified engineering failures, post-mortems, and codified prevention patterns.
> Speculative theories, unverified hunches, or unconfirmed fixes must **never** be recorded here.
> Every entry must strictly satisfy the 8-field structure:
> 1. `Lesson ID` (`LES-XXX`) & Title
> 2. `Subsystem Domain`
> 3. `Observed Problem`
> 4. `Reproduction Evidence`
> 5. `Verified Root Cause / Unresolved Mechanism`
> 6. `Prevention Rule`
> 7. `Supporting Files, Lines & Commands`
> 8. `Confidence & Applicability Boundaries`

---

## LES-001: Antigravity Sandbox Loopback Socket Limitation & Test Masking

- **Lesson ID:** `LES-001`
- **Subsystem Domain:** Testing / Execution Environment Boundaries / IPC Telemetry
- **Date Recorded:** 2026-11-04
- **Observed Problem:**
  Executing `pytest tests/test_milestone1_security_hardening.py -k TestMilestone1FlowStateBridgeListenerAuthentication` resulted in 2 failed tests out of 4:
  - `test_hmac_signed_packet_accepted` failed with `AssertionError: None != 'git status --short'`
  - `test_token_authenticated_packet_accepted` failed with `AssertionError: None != 'C:/legit_test_file.py'`
  - Conversely, `test_unauthenticated_packet_rejected` and `test_flow_state_mirror_format_quarantined_xml_alias` passed.
- **Reproduction Evidence:**
  The failure was reproduced 100% reliably (3 out of 3 serial runs) using:
  ```powershell
  C:\Users\Youssef\Projects\.venv\Scripts\pytest.exe tests/test_milestone1_security_hardening.py -k TestMilestone1FlowStateBridgeListenerAuthentication -vv
  ```
  Targeted diagnostic probes executed in the Python environment:
  1. *Raw UDP Loopback Probe:*
     A minimal script binding `127.0.0.1:39417` and sending datagrams from an independent client socket resulted in `socket.timeout` on `recvfrom` (packet never delivered).
  2. *Raw TCP Loopback Probe:*
     A minimal script attempting `connect(('127.0.0.1', 39419))` immediately failed with:
     `[WinError 10013] An attempt was made to access a socket in a way forbidden by its access permissions`.
  3. *In-Process Logic Probe:*
     Direct in-process execution of `FlowStateBridgeListener.sign_payload()`, HMAC signature verification, and `FlowStateMirror.update_snapshot()` passed without errors, proving the cryptographic and snapshot logic is structurally sound.
- **Verified Root Cause & Unresolved Mechanisms:**
  - *Verified Mechanism:* In the observed Antigravity Standard Sandbox execution context (`BypassSandbox: false`), local loopback network communication on `127.0.0.1` is restricted by Windows network isolation. UDP packets sent between separate socket handles are dropped, and TCP loopback connections are blocked with `WinError 10013`.
  - *Unresolved Mechanism:* The precise Windows OS enforcement layer (Windows Filtering Platform policy, AppContainer network isolation capability mask, or Windows Defender Firewall rule) was not conclusively identified from within the sandboxed environment.
  - *False-Negative Masking in Negative Tests:* In `test_unauthenticated_packet_rejected`, the assertion tested `self.assertNotEqual(updated_snap.active_file_path, "C:/forged_file.py")`. Because the socket dropped all packets, `updated_snap.active_file_path` remained `None`, causing the negative test to pass vacuously without proving that an unauthenticated packet was actually received and cryptographically rejected by the listener.
- **Prevention Rules:**
  1. **Decouple Unit Logic from Network Sockets:** Unit test suites evaluating serialization, signature verification, token comparison, and state mutation must operate in-process or inject mock transports rather than binding real OS network sockets.
  2. **Isolate OS Integration Tests:** Tests requiring real Windows kernel sockets, named pipes, or loopback endpoints must be partitioned into Tier 2 integration suites designed to run in an authorized host environment or dedicated CI runner with network loopback capabilities.
  3. **Assert Positive Reception in Negative Tests:** Negative security tests must assert that the transport actually delivered data (e.g. via packet counter or audit log) before asserting that the state was not modified, preventing dropped packets from masking assertion validity.
- **Supporting Files & Commands:**
  - `jarvis/context/bridges/pipe_listener.py` (`FlowStateBridgeListener`)
  - `jarvis/context/flow_state_mirror.py` (`FlowStateMirror`)
  - `tests/test_milestone1_security_hardening.py` (`TestMilestone1FlowStateBridgeListenerAuthentication`)
- **Confidence & Applicability Boundaries:**
  - *Confidence:* **HIGH** on the socket drop mechanism; **MEDIUM** on the exact OS policy name enforcing the block.
  - *Applicability:* Applies to all tests in `tests/` that bind `127.0.0.1` UDP or TCP sockets when executed inside Antigravity Standard Sandbox Mode.
