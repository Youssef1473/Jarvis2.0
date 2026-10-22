# Session Handoff: Jarvis 2.0 — CSP Hardening Phase 2 (Observational Report-Only)

**Date:** 2026-10-22
**Host Environment:** Windows 11 x64 | Intel Core i5-12500H | NVIDIA RTX 3050 Laptop GPU (4GB VRAM) | Python 3.12
**Committed Baseline on `main`:** Commit [`22a8c84`](https://github.com/Youssef1473/Jarvis2.0/commit/22a8c84d3110548ec45891b63496cbf905e95eae) pushed to `origin/main` (Verified green in GitHub Actions Run [#39863943280](https://github.com/Youssef1473/Jarvis2.0/actions/runs/39863943280))
**Local Test Suite Baseline:** All fast parallel test suites passed (`744 passed, 16 skipped, 2 subtests passed in 64.47s`, exit code 0)

---

## 1. Prior Baseline (`22a8c84`)

Commit `22a8c84` closed the remaining CI test failure:
1. Null-daemon safe extraction and graceful fallback in `AntigravityBridge.generate_response`.
2. Safe daemon pool invalidation on `bridge.daemon = None`.
3. Defensive directory existence checks in `SkillSandbox.validate_and_promote` and `create_snapshot`.
4. Dedicated regression test `test_antigravity_batch_generation_with_null_daemon`.
5. `.gitkeep` tracking for `data/evolution_scratch/` and `data/evolution_snapshots/`.

---

## 2. Phase 2 Implementation: Observational CSP Report-Only

### A. Dual CSP Header Deployment (`jarvis/ui/server.py`)
- **Active Enforced Policy (`Content-Security-Policy`):**
  Maintains complete backwards compatibility for the current HUD without tightening active enforcement, while wiring reporting directives:
  `report-uri /api/csp-report; report-to csp-endpoint;`
- **Prospective Policy (`Content-Security-Policy-Report-Only`):**
  Deployed alongside the enforced policy to observe real-world violations without blocking running client code:
  - Tests elimination of `'unsafe-eval'`.
  - Scopes `connect-src` strictly to loopback (`'self' ws://127.0.0.1:* ws://localhost:* http://127.0.0.1:* http://localhost:*`) plus trusted secure tunnel origin if `JARVIS_SECURE_TUNNEL=1`.
  - Drops unused `blob:` from `img-src`.
  - Dispatches reports to `/api/csp-report` via both `report-uri` (CSP Level 2) and `report-to` (Reporting API Level 3).
- **Absolute Reporting API Endpoint Mapping:**
  Emits `Reporting-Endpoints: csp-endpoint="<absolute_url>"` derived via `derive_absolute_reporting_endpoint()`. Rejects relative paths as mandated by the W3C Reporting API spec, resolving to `http://<host>:<port>/api/csp-report` for loopback and `https://<tunnel_host>[:port]/api/csp-report` when `JARVIS_SECURE_TUNNEL=1`.

### B. Telemetry Ingestion Endpoint (`/api/csp-report`)
- **Route & Content-Type Gate:** Starlette POST route accepting `application/csp-report`, `application/reports+json`, and `application/json`; rejects unsupported types with HTTP 415.
- **Security Boundary:** Exempt from session cookie authentication in `SecurityHeadersMiddleware`, but strictly protected by Host and Origin validation (`validate_request_host_and_origin`). Untrusted cross-origin POSTs fail closed with HTTP 403.
- **Streaming Size Guard (Anti-DoS):**
  - Enforces a 32 KB ceiling while consuming chunks from `request.stream()` (aborts immediately with HTTP 413 without buffering unbounded bodies into RAM).
  - Validates `Content-Length` header (rejects >32KB with 413, rejects negative or non-integer with 400).
- **JSON Structure & Malformation Validation:**
  - Rejects primitive JSON types (integers, strings, booleans) with HTTP 400.
  - Rejects non-dictionary `csp-report` values with HTTP 400.
- **Batch-Aware Rate Limiting & Deduplication:**
  - Rate limiter strictly accounts for individual reports within `application/reports+json` batches, enforcing a global maximum of 60 individual reports per minute across the server instance (HTTP 429).
  - 10-second deduplication cache on `(violated_directive, effective_directive, blocked_uri, document_uri)`.
- **Inert Data Sanitization (Log Injection Defense):**
  - Strips carriage returns, line feeds, and ANSI escape sequences using strict printable ASCII filtering (`32 <= ord(c) <= 126`).
  - Bounds individual field strings to 256 characters.
  - Bounded in-memory telemetry ring buffer (`collections.deque(maxlen=50)`).
  - Returns HTTP 204 No Content.

---

## 3. Scope Boundaries & Verification Status

### Empirically Verified by Automated Test Suites
- Dual CSP header emission (`Content-Security-Policy`, `Content-Security-Policy-Report-Only`, `Reporting-Endpoints`) across HTTP responses and early 401 error responses.
- `Reporting-Endpoints` emits absolute same-origin URLs across local loopback and secure-tunnel configurations, rejecting relative URL paths.
- Ingestion of legacy CSP Level 2 (`application/csp-report`), modern Reporting API Level 3 (`application/reports+json`), and JSON payloads.
- Rejection of unsupported content types (HTTP 415).
- Rejection of oversized payloads (>32KB) both via Content-Length header and streaming chunk limits (HTTP 413).
- Rejection of invalid Content-Length, empty bodies, malformed JSON, primitive JSON, and non-dict `csp-report` payloads (HTTP 400).
- Cross-origin rejection under Host/Origin validation (HTTP 403).
- Batch-level rate limiter accounting, proving an effective maximum of 60 individual reports per minute (HTTP 429).
- Deduplication suppressing duplicate telemetry entries within 10s.
- CRLF and ANSI escape stripping from stored telemetry fields.
- Dynamic tunnel origin injection into `connect-src` and reporting URLs when `JARVIS_SECURE_TUNNEL=1` is configured.
- Live HTTP socket lifecycle verification.

### Real Browser Behavior (Observational Horizon)
- In a live browser session (Chrome, Edge, Brave), the browser will now silently dispatch prospective CSP violations to `/api/csp-report`.
- End-to-end browser collection during real HUD usage remains to be observed once the HUD is accessed by the user, providing the empirical dataset for Phase 3 refactoring.

---

## 4. Uncommitted Working-Tree Manifest

Local modifications ready for review (`git status --short`):

```text
 M handoff.md
 M jarvis/ui/server.py
 M tests/test_hud_security_dsp.py
 M tests/test_ui_server.py
```

`git diff --stat`:
```text
 handoff.md                     | 136 ++++++++--------
 jarvis/ui/server.py            | 223 ++++++++++++++++++++++++--
 tests/test_hud_security_dsp.py | 350 +++++++++++++++++++++++++++++++++++++++++
 tests/test_ui_server.py        |  23 +++
 4 files changed, 657 insertions(+), 75 deletions(-)
```

---

## 5. Next Steps

1. Review and approve the Phase 2 observational CSP implementation.
2. Monitor real-world CSP violation reports in HUD sessions to identify any latent `eval` or dynamic code requirements.
3. Proceed to Phase 3: Event-listener refactoring and nonce generation for top-level scripts.
