# ADR-004: Dual-Key Mutating Tool Authorization and Turn-Taint Tracking

## Status
PROPOSED

## Date
2026-11-02

## Scope
Tool execution dispatch (`SkillRegistry.execute`), tool orchestrator authorization gateway ([`tool_orchestrator.py`](../../jarvis/core/pipeline/tool_orchestrator.py)), indirect prompt injection defense, and conversational turn taint tracking.

---

## Context and Problem Statement
Autonomous AI assistants with physical and desktop actuation tools (e.g. executing PowerShell scripts, writing arbitrary files, making outbound network calls, mutating system settings) are vulnerable to **indirect prompt injection**. When an assistant reads untrusted external data (such as web pages, third-party emails, retrieved documents, or external logs), an attacker can embed instructions that trick the LLM into invoking destructive tools without the user's explicit consent.

Traditional security measures (such as naive keyword blacklists or permanent permission grants) fail because prompts can be rephrased or obfuscated, and static grants allow injected payloads to replay previously granted permissions.

---

## Decision
Implement a two-tier defense combining single-use cryptographic authorization nonces (`DualKeyApprovalManager`) and dynamic turn-level data taint propagation (`TurnTaintTracker`) in [`security_boundaries.py`](../../jarvis/core/security_boundaries.py).

1. **Dual-Key Approval Nonce Lifecycle (`DualKeyApprovalManager`):**
   - Tools performing stateful or destructive modifications (e.g. `execute_powershell`, filesystem writes, process kills) are categorized as **mutating tools**.
   - Invoking a mutating tool requires a cryptographically random authorization nonce generated via `secrets.token_urlsafe()`.
   - **Single-Use Semantics:** Nonces are bound to a specific tool and action, carry a bounded time-to-live (`ttl`, default 60s), and are **immediately consumed and purged from memory** (`pop()`) upon validation.
   - Replaying a previously used nonce or presenting an expired nonce fails closed with a `SecurityBoundaryViolation`.
2. **Turn-Taint Propagation (`TurnTaintTracker`):**
   - Conversational turns begin in an untainted state when direct user input is received.
   - Any registered ingestion tool classified as `KNOWN_UNTRUSTED` or `UNKNOWN` (e.g. web search, URL fetching, reading unconfined files, clipboard queries) immediately marks the active conversational turn as **tainted** (`turn_tainted = True`) via `record_ingestion()`.
   - Execution Gateway Behavior (`evaluate_tool_execution`):
     - **Critical Risk (Level 2 — `CRITICAL_PRIVILEGE_TOOLS`):** Modifies the host environment or live Python runtime. Requires explicit user confirmation nonce **unconditionally**, regardless of turn taint state.
     - **High Privilege (Level 1 — `HIGH_PRIVILEGE_MUTATION_TOOLS`):** Requires an authorization nonce **if the turn is tainted** (`is_tainted_mutation`).
     - **Read-Only (Level 0 — `KNOWN_READ_ONLY_TOOLS`):** Allowed without a human authorization nonce.
3. **Turn-Taint Reset Boundary:**
   - Turn taint state is strictly scoped to the active user interaction and is reset (`reset_turn()`) only when the orchestrator transitions to a brand-new turn initiated by direct, verified user speech or UI input.
4. **End-to-End Data Provenance Invariant (Hardened in `be8d39f`):**
   - An untainted turn does **not** infer trusted provenance for persistent writes. Mutating operations must independently validate path confinement (`validate_confined_path`) and authorization regardless of taint status.

---

## Alternatives Considered
- **Heuristic String Filtering (Blacklisting):** Rejected as fragile; easily circumvented by token splitting, Unicode normalization, or indirect roleplay phrasing.
- **Session-Wide Permanent Authorization:** Rejected because once granted, any subsequent prompt injection within the session could reuse the capability to execute unauthorized commands.
- *(Historical alternatives from initial pre-M-01 architecture: Historical rationale unavailable).*

---

## Consequences and Trade-Offs
- **Positive:**
  - Robust protection against prompt injection exploits: attackers cannot fabricate or replay authorization nonces.
  - Fail-closed default: untrusted ambient data ingestion automatically blocks unconfirmed mutating capabilities.
- **Negative / Costs:**
  - Requires user interaction or deliberate approval mechanisms for legitimate multi-step mutating workflows.
  - Tool classification must be maintained accurately; misclassifying a mutating tool as read-only creates a security hole.

---

## Binding Invariants
- Mutating tools executed through the tool gateway must never run without validating and consuming a valid, unexpired single-use authorization nonce when required by risk level or taint state.
- Ingestion of unverified external data through classified tools must unconditionally taint the active turn.
- A tainted turn must fail closed on all mutating tool execution attempts unless explicitly re-authorized with a fresh nonce.
- Nonces must be invalidated immediately upon consumption (zero replay tolerance).

---

## Implementation References
- [`security_boundaries.py:774`](../../jarvis/core/security_boundaries.py#L774) (`DualKeyApprovalManager`)
- [`security_boundaries.py:1484`](../../jarvis/core/security_boundaries.py#L1484) (`TurnTaintTracker`)
- [`security_boundaries.py:1945`](../../jarvis/core/security_boundaries.py#L1945) (`evaluate_tool_execution`)
- [`tool_orchestrator.py`](../../jarvis/core/pipeline/tool_orchestrator.py) (`ToolOrchestrator`)

---

## Production Call Sites
- `jarvis/core/pipeline/tool_orchestrator.py` (Tool gateway evaluation)
- `jarvis/core/orchestrator.py` (Approval dialog management and dispatch)
- `jarvis/core/brain.py` (Turn-level context taint checks)
- `jarvis/core/mcp_manager.py` (Model Context Protocol tool execution gating)

---

## Verification Evidence
- [`test_milestone1_security_hardening.py`](../../tests/test_milestone1_security_hardening.py) (16 of 16 tests passed on 2026-11-02).
- [`test_milestone6_residual_hardening.py`](../../tests/test_milestone6_residual_hardening.py) (29 of 29 tests passed on 2026-11-02).

---

## Known Limitations and Unresolved Questions
- **Direct Function Calls Outside Gateway:** The dual-key and taint-tracking protections apply strictly to tools invoked via `SkillRegistry.execute` and `ToolOrchestrator`. Direct in-process Python calls that do not route through the gateway bypass these checks.
- **Unclassified External Ingestion:** External data that enters through channels not registered as ingestion tools (e.g. ambient voice transcribed via Whisper or direct internal socket reads) does not automatically trigger `record_ingestion` unless explicitly hooked.
- Read-only tools are exempt from dual-key nonces to maintain conversational fluidity.

---

## Related ADRs
- [ADR-002](ADR-002-win32-job-object-containment.md) (Job Object subprocess containment).
- [ADR-006](ADR-006-egress-validation-ip-pinning.md) (Egress URL validation).
