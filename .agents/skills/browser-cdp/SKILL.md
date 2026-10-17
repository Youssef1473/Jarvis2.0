---
name: browser-cdp
description: >-
  Sub-5ms browser automation via Chrome DevTools Protocol (CDP) WebSocket control
  for Brave, Chrome, and Edge. Enables silent tab inspection, direct URL loading,
  and JavaScript execution without keystroke emulation or window stealing.
---

# Chrome DevTools Protocol (CDP) Browser Control

This skill enables ultra-low-latency, zero-keystroke control of Brave, Chrome, and Edge
by communicating directly with the browser's debugging port over WebSocket JSON-RPC.

---

## 1. Enabling Remote Debugging

Launch browser with remote debugging port:
```powershell
# Brave with debugging enabled on localhost:9222
& "C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe" --remote-debugging-port=9222
```

---

## 2. CDP Python Recipes (Using `urllib` & `websockets`)

### A. Discover All Open Tabs & Targets (<2ms)
```python
import urllib.request
import json

def get_open_tabs(port=9222) -> list[dict]:
    try:
        req = urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=0.2)
        return json.loads(req.read().decode())
    except Exception:
        return []
```

### B. Navigate a Tab Silently via WebSocket (<5ms)
```python
import asyncio
import json
import websockets

async def cdp_navigate(ws_url: str, target_url: str):
    async with websockets.connect(ws_url) as ws:
        # Send Page.navigate command
        msg = {
            "id": 1,
            "method": "Page.navigate",
            "params": {"url": target_url}
        }
        await ws.send(json.dumps(msg))
        reply = await ws.recv()
        return json.loads(reply)
```

### C. Activate / Focus an Existing Tab
```python
def activate_tab(target_id: str, port=9222):
    req = urllib.request.Request(f"http://127.0.0.1:{port}/json/activate/{target_id}", method="PUT")
    with urllib.request.urlopen(req, timeout=0.5):
        pass
```
