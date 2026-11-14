---
trigger: model_decision
description: Security boundaries, DualKeyApprovalManager, TurnTaintTracker, path confinement, SSRF egress validation, and Win32 Job Object process containment.
---

# Security Boundaries & Authorization Invariants

These invariants govern all security-sensitive operations, data flow boundaries, and child processes across Jarvis 2.0.

## 1. Authorization & Tool Mutation Gates
- Route all mutating operations through `DualKeyApprovalManager` in `jarvis.core.security_boundaries`.
- Never execute shell commands with `shell=True` or format raw strings into commands. Use argument lists with `close_fds=True`.
- Validate filesystem paths using `validate_confined_path` before reading or writing.

## 2. Network Egress & SSRF Protection
- Validate every outbound URL using `validate_egress_url`. Fail-closed on private IPs, loopback addresses, link-local endpoints, and metadata services.
- Pin outbound socket connections to the pre-validated IP address to prevent DNS rebinding TOCTOU attacks.

## 3. Taint Tracking & Provenance Lifecycle
- Treat all external inputs (web content, ambient screen telemetry, audio speech data, subprocess stdout/stderr) as untrusted inert data via `TurnTaintTracker`.
- When an external tool returns DOM elements, clipboard contents, or process outputs, mark the active turn tainted immediately.
- **Persistent Provenance:** An untainted turn does *not* imply that stored data is `trusted_internal`. Always identify and record the actual source origin of facts, episodic lessons, and calendar entries before writing to storage.

## 4. Win32 Process Containment (Job Object Sandbox)
- Every sandboxed child process (`job_object_sandbox.py`, `task_manager.py`, `mcp_manager.py`) must be spawned suspended using `CREATE_SUSPENDED`.
- Assign the process to the Win32 Job Object configured with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` *before* execution begins, then resume via `NtResumeProcess` or `ResumeThread`.
- On any Job Object creation, limit configuration, or process assignment failure: immediately terminate the suspended process (`TerminateProcess`), close open handles, and fail closed (`RuntimeError`). Never allow an uncontained child to run.

