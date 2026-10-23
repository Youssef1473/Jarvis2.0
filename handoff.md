# Session Handoff: Jarvis 2.0 — CSP Hardening Phase 3 Deliverable 2 (Cryptographic CSP Nonces)

**Date:** 2026-10-23
**Host Environment:** Windows 11 x64 | Intel Core i5-12500H | NVIDIA RTX 3050 Laptop GPU (4GB VRAM) | Python 3.12
**Committed Baseline on `main`:** Commit [`219cbae`](https://github.com/Youssef1473/Jarvis2.0/commit/219cbae) (`refactor(security): eliminate inline HUD event handlers`) ahead of `origin/main` [`1351454`](https://github.com/Youssef1473/Jarvis2.0/commit/1351454cf9e63920990c8fe8338530ecc596532b)
**Local Test Suite Baseline:** 54/54 passed in `tests/test_hud_security_dsp.py`, 1/1 passed in `tests/test_ui_server.py`, fast parallel suite in progress.

---

## 1. Phase 3 Deliverable 2: Architecture & Implementation

### A. Cryptographic Nonce Generation (`jarvis/ui/server.py`)
- Implemented `generate_csp_nonce()`:
  - Generates 32 cryptographically secure random bytes via Python's `secrets.token_bytes(32)`.
  - Encodes bytes using standard Base64 into a 44-character ASCII string ending with `=`.
  - Zero module-global caching or deterministic derivation; each request receives an independent, unpredictable nonce.
- Implemented `render_template_with_nonce(template_text: str, nonce: str) -> str`:
  - Replaces explicit, author-controlled template placeholders (`nonce="{{ csp_nonce }}"`) with the per-request nonce.
  - Leaves untrusted, third-party, and dynamic content untouched.
  - When nonce is empty or unset, cleanly strips the placeholder attribute without leaving invalid HTML.

### B. Template & Unlock Interface Hardening
- **`jarvis/ui/templates/hud.html`:**
  - Added `nonce="{{ csp_nonce }}"` to the top-level application `<style>` tag (line 10).
  - Added `nonce="{{ csp_nonce }}"` to the primary interactive HUD `<script>` tag (line 2089).
- **`_get_unlock_html()` in `jarvis/ui/server.py`:**
  - Converted interface lock page from fragile f-string template to standard raw template processed via `render_template_with_nonce`.
  - Added `nonce="{{ csp_nonce }}"` to `<style>` and `<script>` tags.
  - Eliminated inline `onsubmit="unlock(event)"` on `#unlockForm`.
  - Added DOMContentLoaded event listener wiring: `document.getElementById('unlockForm').addEventListener('submit', unlock)`.
  - Preserved hash-based bootstrap token auto-exchange and password credential submission.

### C. Level 3 Content Security Policy Directives (`SecurityHeadersMiddleware`)
- **Per-Request Nonce Lifecycle:**
  - `SecurityHeadersMiddleware.dispatch` binds `request.state.csp_nonce = nonce`.
  - Downstream handlers (`_handle_index` and `_get_unlock_html`) read `request.state.csp_nonce` to render the matching HTML body.
  - Outgoing responses receive `Content-Security-Policy` and `Content-Security-Policy-Report-Only` headers containing `'nonce-{nonce}'`.
  - Guarantees 100% exact character-for-character match between response headers and `<style>`/`<script>` tags for every HTTP response.
- **CSP Directives Emitted:**
  - `script-src 'self' 'unsafe-inline' 'unsafe-eval' 'nonce-{nonce}';` (enforced) / without `unsafe-eval` (report-only).
  - `script-src-elem 'self' 'unsafe-inline' 'unsafe-eval' 'nonce-{nonce}';` (enforced) / without `unsafe-eval` (report-only).
  - `script-src-attr 'unsafe-inline';` (enforced policy for transition safety) / `script-src-attr 'none';` (report-only policy to flag inline event handler regressions).
  - `style-src 'self' 'unsafe-inline' https://fonts.googleapis.com;` (fallback without nonce in both policies to ensure Level 2 browsers do not block inline `style="..."` attributes).
  - `style-src-elem 'self' https://fonts.googleapis.com 'nonce-{nonce}';` (both policies).
  - `style-src-attr 'unsafe-inline';` (both policies, authorizing existing HUD dynamic inline styles).
  - Telemetry reporting endpoints preserved: `Reporting-Endpoints: csp-endpoint="<absolute_url>"`, `report-uri /api/csp-report; report-to csp-endpoint;`.
- **Anti-Caching Safeguards:**
  - Enforced `Cache-Control: no-cache, no-store, must-revalidate`, `Pragma: no-cache`, and `Expires: 0` on all HTML responses containing nonces (authenticated HUD, lock page, and missing-template 404).

---

## 2. Regression Test Coverage (`tests/test_hud_security_dsp.py`)

Added 8 comprehensive unit and integration test methods verifying the 9 required dimensions:
1. `test_csp_nonce_generation_uniqueness_and_format`:
   - Validates 44-character Base64 format (`^[A-Za-z0-9+/]{43}=$`).
   - Asserts zero collisions across 100 consecutive generations.
   - Asserts Shannon entropy > 5.0 bits/char across character distribution.
   - Verifies `render_template_with_nonce` behavior with active nonce and empty nonce.
2. `test_csp_nonce_authenticated_hud_body_and_headers_matching`:
   - Verifies authenticated GET `/` delivers matching nonces in `Content-Security-Policy`, `Content-Security-Policy-Report-Only`, and HTML `<style>`/`<script>` tags.
   - Verifies anti-caching headers (`Cache-Control`, `Pragma`, `Expires`).
   - Asserts zero unreplaced `{{ csp_nonce }}` placeholders in the response.
3. `test_csp_nonce_unauthenticated_unlock_page_and_headers_matching`:
   - Verifies unauthenticated GET `/` delivers matching nonces on lock page.
   - Verifies `#unlockForm` contains zero inline `onsubmit` attributes and registers `addEventListener('submit', unlock)`.
4. `test_unlock_flow_and_session_cookie_exchange`:
   - Rejects invalid tokens with 401.
   - Exchanges single-use bootstrap token for `jarvis_session` cookie on POST `/api/auth/unlock`.
   - Prevents replay of consumed bootstrap token.
   - Verifies authenticated client loads full HUD.
5. `test_csp_nonce_present_across_error_and_api_responses`:
   - Verifies well-formed CSP headers containing nonces on 401 Unauthorized, 403 Forbidden, and 200 JSON API responses.
6. `test_csp_enforced_vs_report_only_policy_directives`:
   - Confirms Enforced policy retains `'unsafe-eval'` while Report-Only policy strictly excludes it.
   - Verifies presence of Level 3 element and attribute directives (`script-src-elem`, `script-src-attr`, `style-src-elem`, `style-src-attr`).
   - Confirms fallback `style-src` contains `'unsafe-inline'` and omits nonce.
   - Confirms absolute reporting endpoint and reporting directives.
7. `test_csp_nonce_concurrency_isolation`:
   - Runs 24 concurrent requests across a 6-worker thread pool.
   - Verifies every concurrent request receives a distinct nonce (24 unique nonces).
   - Verifies in every response, body nonces match header nonces without cross-contamination.
8. `test_zero_inline_event_handlers_in_hud_and_unlock_templates`:
   - Scans `hud.html` and `_get_unlock_html()` for inline `on*=` handlers.
   - Asserts 0 matches in both templates.
9. `test_csp_missing_template_404_has_anti_cache_headers`:
   - Verifies missing template 404 HTML response also carries strict anti-caching headers (`Cache-Control: no-cache, no-store, must-revalidate`, `Pragma: no-cache`, `Expires: 0`).

---

## 3. Scope Boundaries & Verification Status

### Empirically Verified by Automated Test Suites
- `python tools/run_fast_tests.py tests/test_hud_security_dsp.py`: 55 passed in 4.76s (Exit code: 0).
- `python tools/run_fast_tests.py tests/test_ui_server.py`: 1 passed in 2.05s (Exit code: 0).
- `python tools/run_fast_tests.py tests/test_hud_zero_cls.py`: 6 passed in 0.05s (Exit code: 0).
- `python tools/run_fast_tests.py --fast -w 4`: 755 passed, 16 skipped, 2 subtests passed in 65.53s (Exit code: 0).
- `node --check` (HUD JavaScript): Clean syntax check with 0 syntax errors across 2,723 lines (Exit code: 0).
- `node --check` (Unlock JavaScript): Clean syntax check with 0 syntax errors (Exit code: 0).
- `git diff --check`: Clean, zero whitespace errors or trailing spaces.

---

## 4. Uncommitted Working-Tree Manifest

Local modifications ready for review (`git status --short`):

```text
 M handoff.md
 M jarvis/ui/server.py
 M jarvis/ui/templates/hud.html
 M tests/test_hud_security_dsp.py
 M tests/test_hud_zero_cls.py
```

`git diff --stat`:
```text
 handoff.md                     | 151 ++++++++++++----------
 jarvis/ui/server.py            | 102 ++++++++++++---
 jarvis/ui/templates/hud.html   |   4 +-
 tests/test_hud_security_dsp.py | 284 ++++++++++++++++++++++++++++++++++++++++-
 tests/test_hud_zero_cls.py     |   2 +-
 5 files changed, 450 insertions(+), 93 deletions(-)
```

---

## 5. Next Steps

1. Developer review of the Phase 3 Deliverable 2 diff.
2. Upon approval, stage and commit Phase 3 Deliverable 2 (`feat(security): implement per-response cryptographic CSP nonces`).
3. Keep unpushed on local `main` until user directs remote synchronization.
