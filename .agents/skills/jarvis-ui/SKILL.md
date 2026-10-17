---
name: jarvis-ui
description: >-
  Architectural specification, holographic design tokens, WebSocket event protocols, and runbook for the Jarvis 2.0 Web & Desktop UI interface.
---

# Jarvis 2.0 Holographic UI Interface Skill

This skill documents the design system, WebSocket event schema, and execution procedures for the **Jarvis 2.0 Holographic HUD Interface**.

---

## 1. Design System & Visual Tokens

The interface follows an authentic Stark Industries Mark-85 holographic aesthetic:

| Token | Hex Value | Usage |
| :--- | :--- | :--- |
| **Holo Cyan** | `#00f0ff` | Primary neon borders, standby rings, active HUD accents |
| **Holo Blue** | `#00b4d8` | Secondary circular orbital conduits, telemetry text |
| **Stark Amber/Gold**| `#ffaa00` | Processing state, neural model reasoning, warning gates |
| **Arc Emerald** | `#00e199` | Vocal delivery, system online confirmations, success badges |
| **Crimson Alert** | `#ff3366` | Microphone mute, emergency stops, safety gate confirmations |
| **Obsidian Deep** | `#070b12` | Glassmorphic background panels (`rgba(7, 11, 18, 0.85)`) |
| **Glow Shadow** | `0 0 15px rgba(0,240,255,0.4)` | Outer neon aura for buttons, rings, and telemetry |

### Typography
- **Headings & Badges**: `'Orbitron'`, `'Rajdhani'`, `'Segoe UI'`, sans-serif.
- **Code & Telemetry**: `'JetBrains Mono'`, `'Cascadia Code'`, `'Consolas'`, monospace.

---

## 2. Server Architecture & Endpoints

The UI service runs as a zero-dependency Starlette/Uvicorn HTTP & WebSocket daemon on `http://127.0.0.1:20129`:

- `GET /` — Serves the reactive holographic HUD single-page app ([`jarvis/ui/templates/hud.html`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/jarvis/ui/templates/hud.html)).
- `GET /api/status` — Returns instantaneous JSON telemetry (Battery %, GPU acceleration, Active LLM, Mute state, Uptime).
- `POST /api/command` — Executes a typed command through [`JarvisOrchestrator.handle_command`](file:///C:/Users/Youssef/Projects/Jarvis_2.0/jarvis/core/orchestrator.py).
- `WebSocket /ws` — Bidirectional real-time telemetry, audio visualization, state synchronization, and chat logs.

---

## 3. WebSocket Event Protocol

All messages are UTF-8 JSON payloads:

### Server -> Client Broadcasts:
```json
// State update
{ "type": "state", "state": "idle" | "listening" | "thinking" | "speaking" }

// Telemetry update
{
  "type": "telemetry",
  "battery": 95,
  "charging": true,
  "gpu": "RTX 3050 (CUDA FP16)",
  "model": "qwen/qwen3.8-27b",
  "tier": "Fast LPU",
  "cost": "$0.00",
  "is_muted": false
}

// Conversation message
{ "type": "chat", "sender": "user" | "jarvis", "text": "What is my battery level?", "timestamp": "20:45:00" }

// Speech audio wave level
{ "type": "audio_level", "level": 0.72 }
```

### Client -> Server Actions:
```json
// Execute command
{ "type": "command", "text": "Jarvis, set volume to 50 percent" }

// Quick actions
{ "type": "action", "action": "toggle_mute" | "screen_vision" | "status" }
```

---

## 4. Operational Runbook

1. **Launch Server**: Integrated automatically into `JarvisOrchestrator.start()`.
2. **Access URL**: Open `http://127.0.0.1:20129` in any modern web browser or click **"Open HUD Interface"** from the Windows System Tray icon.
3. **Hardware Health**: Telemetry queries native Win32 `GetSystemPowerStatus` and PyTorch/CUDA without blocking the event loop.
