# ADR-006: Egress URL Validation and Outbound IP Pinning

## Status
PROPOSED

## Date
2026-11-02

## Scope
Outbound HTTP/HTTPS network communication, Server-Side Request Forgery (SSRF) defense, Chrome DevTools Protocol (CDP) browser fetch paths, and HTTP connection handlers in [`security_boundaries.py`](../../jarvis/core/security_boundaries.py).

---

## Context and Problem Statement
Jarvis 2.0 autonomous agents frequently perform web searches, retrieve external documentation, and navigate web pages via browser automation. When processing untrusted user prompts or indirect prompt injections, an attacker can coerce the assistant into sending HTTP requests to malicious URLs.

Critical threat vectors include:
1. **Intranet & Cloud Metadata Exploitation (SSRF):** Attacking local network endpoints (e.g. router admin interfaces `192.168.1.1`, cloud metadata services `169.254.169.254`, or local loopback debug ports `127.0.0.1:9222`).
2. **DNS Rebinding (TOCTOU):** The attacker controls a dynamic DNS nameserver. During the initial URL validation check, the domain resolves to an authorized public IP address (passing validation). Milliseconds later, when the HTTP client establishes the actual socket connection, the nameserver responds with `127.0.0.1` (with TTL=0), allowing the client to bypass firewall boundaries and access internal services.
3. **Redirect Downgrade & Credential Leakage:** An authorized external URL redirects the client to an internal endpoint while attempting to forward sensitive session tokens or downgrading from HTTPS to plaintext HTTP.

---

## Decision
Enforce fail-closed egress URL validation, multi-hop redirect verification, and socket-level IP destination pinning in [`security_boundaries.py`](../../jarvis/core/security_boundaries.py).

1. **Pre-Resolution & Egress Filtering (`validate_egress_url`):**
   - Restrict protocol schemes strictly to `http` and `https`. Schemes such as `file://`, `chrome://`, `brave://`, and `gopher://` are rejected.
   - Denylist dangerous ports (e.g. SMB, RDP, SSH, database listeners).
   - Perform fail-closed DNS pre-resolution across all returned IP addresses (`socket.getaddrinfo`), blocking destinations that fall into non-global IP spaces:
     - Loopback (`127.0.0.0/8`, `::1`) unless explicitly whitelisted for local MCP servers.
     - Private RFC 1918 networks (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`).
     - Link-Local and Cloud Metadata ranges (`169.254.0.0/16`, `fe80::/10`).
     - Carrier-Grade NAT (`100.64.0.0/10`).
     - Multicast and Reserved ranges (`224.0.0.0/4`, `ff00::/8`, `0.0.0.0/8`, `::`).
2. **Outbound Socket IP Pinning (`PinnedHTTPHandler`, `PinnedHTTPSHandler`):**
   - To mitigate DNS rebinding races on custom request paths, Jarvis provides custom `urllib` handlers (`_pinned_opener`).
   - The socket connection routine connects directly to the pre-validated IP address (`_pinned_ip`), bypassing secondary DNS resolution at the operating system socket layer.
   - The original host header and TLS Server Name Indication (SNI) are preserved to maintain standard certificate validation and virtual host routing.
3. **Safe Multi-Hop Redirect Validation (`SafeRedirectHandler`):**
   - Each intermediate redirect hop is individually intercepted and subjected to the complete SSRF validation suite (bounded to a maximum of 4 hops).
   - Disallow protocol downgrade from HTTPS to HTTP across redirect hops.
   - Cross-origin redirects strip sensitive authentication headers (`Authorization`, `Cookie`).
4. **Environment Proxy Neutralization (`NoProxyHandler`):**
   - Automatically disables system/environment proxy auto-detection (`HTTP_PROXY`, `HTTPS_PROXY`) to prevent proxy injection bypasses.

---

## Scope & Boundaries of Demonstrated Protection
The security boundaries enforced by this mechanism must be distinguished across implementation layers:

- **Mitigated Request Paths:** The pinned HTTP/HTTPS handlers connect using the IP address selected during validation, mitigating DNS re-resolution races on those specific request paths (such as `_pinned_opener` in `security_boundaries.py` and the CDP proxy in `browser_cdp.py`).
- **Unpinned Network Clients:** Coverage of other network clients, third-party libraries (e.g. `requests`, `httpx`, `aiohttp`), raw socket callers, and external SDK connections must be evaluated independently. Unpinned clients that perform independent DNS resolution remain subject to standard OS DNS lookups and do not inherit socket IP pinning automatically.
- **Testing Scope:** Unit tests in `test_milestone1_security_hardening.py` verify pre-resolution rejection, mock DNS returns, and redirect hop filtering. Live network integration tests verify IP connection behavior only for callers explicitly configured to use `_pinned_opener`.

---

## Alternatives Considered
- **Standard HTTP Clients (Unpinned `urllib` or `requests`):** Rejected because they execute a second DNS lookup at connection time, leaving a critical TOCTOU window for DNS rebinding.
- **Hostname Blacklists:** Easily bypassed via hexadecimal/octal IP representations, wildcard DNS services (`xip.io`), or dynamic domain re-pointing.
- *(Historical alternatives from initial pre-M-01 architecture: Historical rationale unavailable).*

---

## Consequences and Trade-Offs
- **Positive:**
  - Robust, fail-closed mitigation of SSRF, metadata service enumeration, and DNS rebinding on pinned HTTP handlers.
  - Safe redirect tracking preserves credential privacy on cross-origin redirects.
- **Negative / Costs:**
  - Legitimate local web service queries (e.g. testing local web apps) fail by default unless explicitly configured with `allow_localhost=True`.
  - External third-party libraries that do not accept custom `urllib` openers require a local validating forward proxy.

---

## Binding Invariants
- HTTP/HTTPS connections executed through `security_boundaries.py` or browser automation must pass through `validate_egress_url` and utilize pinned destination sockets.
- Redirect hops must never bypass validation or downgrade from HTTPS to HTTP.

---

## Implementation References
- [`security_boundaries.py:273`](../../jarvis/core/security_boundaries.py#L273) (`validate_egress_url`)
- [`security_boundaries.py:295`](../../jarvis/core/security_boundaries.py#L295) (`SafeRedirectHandler`)
- [`security_boundaries.py:421`](../../jarvis/core/security_boundaries.py#L421) (`PinnedHTTPHandler`)
- [`security_boundaries.py:453`](../../jarvis/core/security_boundaries.py#L453) (`PinnedHTTPSHandler`)

---

## Production Call Sites
- `jarvis/skills/browser_agent.py` (Browser web navigation)
- `jarvis/skills/browser_cdp.py` (CDP automation fetch and proxy)
- `jarvis/skills/web_search.py` (Web search result link verification)
- `jarvis/core/mcp_manager.py` (Model Context Protocol network transport)

---

## Verification Evidence
- [`test_milestone1_security_hardening.py`](../../tests/test_milestone1_security_hardening.py) (16 of 16 tests passed on 2026-11-02).
- [`test_security_boundaries.py`](../../tests/test_security_boundaries.py) (SSRF filtering and redirect hop tests).

---

## Known Limitations and Unresolved Questions
- Sites utilizing multi-CDN geo-routing with short TTLs might fail if the pinned IP differs from regional load balancers; however, connection-level pinning guarantees zero DNS rebinding exposure on pinned paths.
- Third-party packages running outside the custom opener do not receive socket IP pinning unless intercepted at the socket or proxy layer.

---

## Related ADRs
- [ADR-004](ADR-004-dual-key-tool-authorization-turn-taint.md) (Tool authorization and turn taint).
