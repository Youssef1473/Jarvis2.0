---
name: mcp-builder
description: >-
  Model Context Protocol (MCP) server development, stdio/SSE transport integration,
  and tool sandboxing for Jarvis 2.0. Guides building, connecting, and verifying
  MCP tool providers (GitHub, PostgreSQL, Docker, Filesystem, Voidtools) with zero subprocess leaks.
---

# Model Context Protocol (MCP) Server Development & Integration

This skill guides the design, implementation, and verification of **Model Context Protocol (MCP)** tool servers and clients within the Jarvis 2.0 architecture.

MCP provides an open, standardized JSON-RPC 2.0 interface connecting AI models to external tools, databases, and services without proprietary API wrappers.

---

## 1. Protocol Architecture & Transports

MCP separates concerns into **Clients** (Jarvis Brain / Antigravity) and **Servers** (Tool Providers):

```mermaid
flowchart LR
    Jarvis["Jarvis Core\n(MCP Client Gateway)"] <-->|JSON-RPC 2.0\n(stdio pipe)| MCPServer["MCP Tool Server\n(GitHub / Postgres / Docker)"]
    MCPServer <--> Target["External API / Local Service\n(Docker Daemon / SQLite / REST)"]
```

### Transports Supported
1. **`stdio` (Local Fast-Path):** Primary transport for local tools. The MCP server runs as a managed child subprocess; requests/responses flow over standard input/output.
2. **`SSE` (Server-Sent Events / HTTP):** Used for remote or multi-client tool servers hosted across a local network or cloud.

---

## 2. Invariant: Subprocess Lifecycle & Sandboxing

All local MCP servers must run with strict OS sandboxing and deterministic lifecycle management:

```python
import subprocess
import os

def spawn_mcp_server(cmd: list) -> subprocess.Popen:
    """Spawns an MCP server child process isolated in its own process group."""
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
        creation_flags |= subprocess.CREATE_NEW_PROCESS_GROUP

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        creationflags=creation_flags,
        close_fds=True,
    )
    return proc
```

### Zero Orphaned Processes Guarantee
- Every MCP child process must be registered with `ManagedServiceManager`.
- Implement clean termination hooks with a strict 2.0s deadline (`terminate()` $\to$ `wait(2.0)` $\to$ `kill()`).

---

## 3. Tool Schema Specification

MCP servers expose available tools via the `tools/list` JSON-RPC method. Every tool requires a strictly typed JSON Schema:

```json
{
  "jsonrpc": "2.0",
  "method": "tools/list",
  "result": {
    "tools": [
      {
        "name": "query_database",
        "description": "Executes read-only SQL query against the specified SQLite database.",
        "inputSchema": {
          "type": "object",
          "properties": {
            "query": {
              "type": "string",
              "description": "SQL SELECT statement to execute."
            },
            "database_path": {
              "type": "string",
              "description": "Absolute path to the target SQLite database."
            }
          },
          "required": ["query"]
        }
      }
    ]
  }
}
```

---

## 4. Inert Data Demarcation & Prompt Injection Defense

All output returned by MCP tools must be treated as untrusted external data.

- **Strict Rule:** Never feed raw MCP responses directly into prompt text without inert XML encapsulation.
- Format all tool output using `<reference_data>` wrappers:

```python
def format_mcp_tool_result(tool_name: str, raw_result: str) -> str:
    """Wraps untrusted MCP tool results inside inert demarcation tags."""
    sanitized = raw_result.replace("</reference_data>", "[ESCAPED_TAG]")
    return (
        f'<reference_data source="mcp:{tool_name}">\n'
        f'{sanitized}\n'
        f'</reference_data>'
    )
```

---

## 5. Minimal Python stdio MCP Server Pattern

For lightweight, zero-dependency tools in Jarvis, implement a pure Python stdio server:

```python
import sys
import json

def handle_rpc_request(request: dict) -> dict:
    method = request.get("method")
    req_id = request.get("id")

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "jarvis-local-mcp", "version": "1.0.0"}
            }
        }
    elif method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "tools": [
                    {
                        "name": "ping",
                        "description": "Health check probe returning pong.",
                        "inputSchema": {"type": "object", "properties": {}}
                    }
                ]
            }
        }
    elif method == "tools/call":
        params = request.get("params", {})
        tool_name = params.get("name")
        if tool_name == "ping":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": "pong"}]
                }
            }

    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": "Method not found"}
    }

def main():
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            req = json.loads(line)
            resp = handle_rpc_request(req)
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()
        except Exception as e:
            sys.stderr.write(f"RPC Error: {e}\n")
            sys.stderr.flush()

if __name__ == "__main__":
    main()
```

---

## 6. Verification & Inspection Checklist

When adding or testing a new MCP server:
1. Verify initialization handshake responds with `protocolVersion`.
2. Verify all tool execution arguments pass through `SafetyGate` validation.
3. Test abnormal termination: kill the MCP server process and verify Jarvis catches `BrokenPipeError` without hanging.
4. Verify all tests pass:
   ```powershell
   .venv\Scripts\python.exe -m unittest discover tests
   ```
