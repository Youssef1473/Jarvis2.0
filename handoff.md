# Session Handoff: Jarvis 2.0 — Sprint 1 / Item H1 (MCP HTTP SSRF Hardening)

**Date:** 2026-10-23
**Host Environment:** Windows 11 x64 | Intel Core i5-12500H | NVIDIA RTX 3050 Laptop GPU (4GB VRAM) | Python 3.12
**Committed Baseline on `main`:** Commit [`be126f1`](https://github.com/Youssef1473/Jarvis2.0/commit/be126f1fd9ee834a37c621d944ebe67358a6be2e) (`feat(security): implement per-response cryptographic CSP nonces`) synchronized with `origin/main`
**Test Suite Verification:** Full fast parallel test suite: **797 passed, 16 skipped, 47 subtests passed** (167.26s, exit code 0)

---

## 1. Vulnerabilities Closed (Sprint 1 / Item H1)

1. **MCP HTTP Transport SSRF & Redirect Bypass:**
   - Previously, `MCPClientSession._send_http_request` validated the initial endpoint URL with `validate_egress_url(..., allow_local_mcp=True)`, but executed the HTTP request using standard `urllib.request.urlopen()`.
   - `urllib.request.urlopen()` uses default redirect handlers that follow HTTP 30x redirects without any egress filtering, allowing external MCP servers to pivot directly into loopback (`127.0.0.1`, `::1`) or internal networks (`10.0.0.0/8`, `192.168.0.0/16`, `169.254.169.254`).
   - Closed by routing all HTTP MCP requests through `build_safe_url_opener(allow_local_mcp=True)`.

2. **DNS Rebinding Connection Pinning (SEC-H1-01):**
   - Previously, `validate_egress_url` performed DNS resolution at validation time, but the connection layer in `urllib` made an independent, second OS-level DNS lookup via `socket.create_connection((hostname, port))`.
   - Furthermore, `MCPClientSession._send_http_request` called `validate_egress_url` (which discarded the validated IP) and did not attach `_pinned_ip` to the request object. Inside `PinnedHTTPHandler.http_open`, `getattr(req, "_pinned_ip", None)` returned `None`, triggering a redundant second DNS lookup that permitted loopback rebinding under `allow_local_mcp=True`.
   - Closed by resolving and pinning the IP directly at initial validation (`clean_url, _, pinned_ip = resolve_and_validate_destination(self.endpoint_url, allow_local_mcp=True)`), assigning `req._pinned_ip = pinned_ip`, and pinning the TCP socket connect call (`(pinned_ip, port)`).
   - For HTTPS, the original hostname is preserved for TLS SNI (`server_hostname`) and certificate verification, and the HTTP `Host` header continues to emit the original authority.
   - For hostnames with mixed public and private/loopback DNS answers, the destination fails closed immediately.

3. **Unsafe URL Protocol Schemes Blocked (SEC-H1-02):**
   - Previously, `build_safe_url_opener` delegated to stdlib `urllib.request.build_opener()`, which implicitly registered `FileHandler`, `FTPHandler`, and `DataHandler`. This permitted direct local file disclosure via `file:///` URLs.
   - Closed by instantiating an explicit `urllib.request.OpenerDirector()` registering exclusively allowlisted HTTP/HTTPS handlers (`NoProxyHandler`, `SafeRedirectHandler`, `PinnedHTTPHandler`, `PinnedHTTPSHandler`, `HTTPErrorProcessor`, `HTTPDefaultErrorHandler`, `UnknownHandler`), ensuring all non-HTTP schemes fail closed with `URLError`.

4. **Local-MCP Destination Scope Narrowing & Destination Integrity:**
   - Previously, `resolve_and_validate_destination` computed `permit_loopback = allow_local_mcp or allow_localhost`. If `allow_local_mcp=True`, any arbitrary non-local hostname (such as `rebind.attacker.com`) whose first DNS lookup returned `127.0.0.1` or `::1` was accepted under `if permit_loopback and ip_obj.is_loopback:`.
   - Furthermore, if `localhost` resolved to a public IP, it was accepted as a valid global destination, creating potential data leakage risks.
   - Closed by strictly restricting loopback permission to explicitly configured local destinations (`is_explicit_local: hostname_lower == 'localhost'` or loopback IP literal `ip_parsed.is_loopback`). For any explicitly local destination, **all** resolved IP answers must be loopback addresses. Any non-loopback answer (whether private or public) is rejected with `SecurityBoundaryViolation("Local destination '...' resolved to non-loopback address '...'")`, and mixed answers fail closed immediately. Arbitrary remote hostnames resolving to loopback via DNS now fail closed with `SecurityBoundaryViolation("SSRF Protection: Host '...' resolved to non-global address '...'")`.

5. **Per-Request Redirect Hop Accounting & Concurrency Isolation:**
   - Previously, `SafeRedirectHandler` maintained mutable instance state (`self.hops = 0`), which was corrupted by concurrent requests and caused false hop-limit rejections when an opener was reused across requests.
   - Closed by attaching request-scoped state (`_jarvis_hops`, `_pinned_ip`) directly to the `urllib.request.Request` instances during hop resolution.

6. **Redirect Scheme Preservation, Relative Redirects, and Credential Stripping:**
   - Relative redirects (`/path`) are correctly joined against `req.full_url` before validation.
   - Redirects attempting to downgrade from HTTPS to HTTP are blocked with a `SecurityBoundaryViolation`.
   - Cross-origin redirects strip sensitive authentication headers (`Authorization`, `Proxy-Authorization`, `Cookie`, `Cookie2`, `X-Api-Key`, `X-Auth-Token`).
   - Redirect hops strictly disallow loopback/private destinations, even if the initial request was an authorized local-MCP endpoint.

7. **Implicit Environment Proxy Bypass Prevention:**
   - `NoProxyHandler` (inheriting from `urllib.request.ProxyHandler(proxies={})`) is integrated into `build_safe_url_opener`, ensuring that ambient environment proxies (`HTTP_PROXY`, `HTTPS_PROXY`) cannot intercept or bypass the IP-pinning guarantee.

---

## 2. Test Evidence (RED → GREEN Protocol)

### A. Initial H1 RED Checkpoint (Before Production Fixes)
Command: `python -m unittest tests/test_mcp_security.py`
Output: **FAILED (failures=8, errors=5)** across 24 tests.

### B. SEC-H1-01 & SEC-H1-02 RED Checkpoint (Before Blocker Fixes)
Command:
```powershell
python -m unittest tests.test_mcp_security.TestMCPTransportIntegration.test_mcp_client_dns_rebinding_to_loopback_blocked tests.test_mcp_security.TestSafeOpenerSchemes
```
Output: **FAILED (failures=4)**:
- `test_mcp_client_dns_rebinding_to_loopback_blocked`: `AssertionError: ['rebind.attacker.com', 'rebind.attacker.com'] != ['rebind.attacker.com'] : DNS should only be resolved once!`
- `test_safe_opener_blocks_file_scheme_directly`: `AssertionError: URLError not raised` (file content successfully read by `FileHandler`)
- `test_safe_opener_blocks_data_and_ftp_schemes_directly`: `AssertionError: URLError not raised` (`data:` URL handled)
- `test_safe_opener_handlers_exclude_file_ftp_data`: `AssertionError: 'FileHandler' unexpectedly found in [...]`

### C. Local-MCP Destination Integrity RED Checkpoint (Before Destination Integrity Fix)
Command:
```powershell
python -m unittest tests.test_mcp_security.TestLocalMCPDestinationScope
```
Output: **FAILED (failures=4, errors=1)**:
- `test_explicit_localhost_resolving_to_public_ipv4_rejected`: `AssertionError: SecurityBoundaryViolation not raised` (returned `93.184.216.34`)
- `test_explicit_localhost_resolving_to_public_ipv6_rejected`: `AssertionError: SecurityBoundaryViolation not raised` (returned `2606:2800:220:1:248:1893:25c8:1946`)
- `test_explicit_localhost_with_mixed_loopback_first_fails_closed`: `AssertionError: SecurityBoundaryViolation not raised` (accepted both answers without failing closed)
- `test_explicit_localhost_with_mixed_public_first_fails_closed`: `AssertionError: SecurityBoundaryViolation not raised` (accepted both answers without failing closed)
- `test_mcp_client_session_rejects_localhost_resolving_to_public_ip`: Intercepted by `mock_create_conn` with `AssertionError: Socket connection attempted for forbidden destination!`

### D. GREEN Verification (After Scope & Destination Integrity Fixes)
1. **Local-MCP Scope & Integrity Tests (14 tests):**
   ```powershell
   python -m unittest tests.test_mcp_security.TestLocalMCPDestinationScope
   ```
   Result: **14 passed in 0.021s (Exit code: 0)**

2. **Combined Focused Security Test Suites (83 tests):**
   ```powershell
   python -m unittest tests/test_mcp_security.py tests/test_mcp_client_transport.py tests/test_plugin_integrations.py tests/test_security_boundaries.py
   ```
   Result: **83 passed in 1.77s (Exit code: 0)**

3. **Full Fast Parallel Test Suite:**
   ```powershell
   python tools/run_fast_tests.py --fast -w 4
   ```
   Result: **797 passed, 16 skipped, 47 subtests passed in 167.26s (Exit code: 0)**

---

## 3. Working-Tree Status

`git status --short`:
```text
 M handoff.md
 M jarvis/core/mcp_manager.py
 M jarvis/core/security_boundaries.py
 M tests/test_mcp_client_transport.py
 M tests/test_plugin_integrations.py
?? tests/test_mcp_security.py
```

`git diff --stat` (tracked modifications against `be126f1`):
```text
 handoff.md                         | 211 +++++++++++++------------
 jarvis/core/mcp_manager.py         |  11 +-
 jarvis/core/security_boundaries.py | 306 +++++++++++++++++++++++++++++++++----
 tests/test_mcp_client_transport.py |  22 +--
 tests/test_plugin_integrations.py  |   6 +-
 5 files changed, 409 insertions(+), 147 deletions(-)
```

Untracked files:
```text
 tests/test_mcp_security.py         | 775 lines (untracked new test suite)
```

Total net footprint: 6 files, 1,184 insertions, 147 deletions.

---

## 4. Next Steps

1. Developer final review of Sprint 1 / Item H1 security diff and regression test suite.
2. Await explicit confirmation to stage and commit H1.
3. Advance to Sprint 1 / Item H2 (`TurnTaintTracker` request-scoped concurrency isolation).
