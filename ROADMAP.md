# Hardening roadmap

## P0 — close confirmed security gaps

1. Gate direct `execute_powershell()` calls with the existing `SafetyGate`; keep the `BackgroundTaskManager` check on the background route. Add tests proving blocked commands never invoke the subprocess.
2. Apply fail-closed HTTP(S) URL validation to the headless browser and deep-research fetch paths. Reject any hostname whose resolved set includes a non-global address, validate each redirect destination, and test DNS failure/mixed answers/redirects with mocked resolution.
3. Confine screenshot outputs to the fixed data root after path resolution; reject traversal and symlink escapes before starting Playwright. Add mocked tests for valid and invalid destinations.
4. Escape untrusted source text before inserting it into structured references and retain an explicit trusted instruction that retrieved content is untrusted. Do not claim this alone prevents model prompt injection.
5. For robust SSRF protection beyond these application checks, apply outbound network policy that blocks private/link-local destinations; URL validation alone cannot prevent DNS rebinding or browser subresource requests.

## P1 — lifecycle and concurrency correctness

1. Ensure timer/watchdog COM initialization is paired with `CoUninitialize()` in `finally`, and wait for SAPI completion while the voice object remains alive.
2. Marshal HUD visibility and shutdown operations to the Tk-owning event-loop thread; replace sleep-based smoke coverage with thread-affinity tests.
3. Remove unnecessary `shell=True` from the argument-list process launch in `project_manager.py` and add a mocked path-with-spaces test.

## P2 — define and validate future integrations

1. Specify MCP server configuration, transport/session ownership, tool discovery, invocation, cancellation, timeout, and shutdown behavior before replacing the current stub.
2. Live-test the Copilot CLI fallback only in an authenticated environment, with quota handoff and non-quota failure cases kept distinct.
3. Re-run platform-specific audio, UIA, Tk, COM, browser, and network egress checks on supported Windows hardware; unit tests do not establish those physical postconditions.
