# Jarvis 3.0 — Personal AI Assistant

An Iron-Man inspired, voice-first personal AI assistant engineered for Windows 11 with an NVIDIA RTX GPU. Version 3.0 keeps Ollama as its local-first primary brain, adds explicitly requested cloud specialist consultations, and improves model visibility in the HUD.

---

## Features

- **Dual Voice Activation:**
  - **Push-to-Talk:** Hold the configured PTT key while speaking, then release to transcribe and process. The checked-in configuration uses `End` (`fin`).
  - **Wake Word:** Say *"Hey Jarvis"* anytime to trigger voice command listening in the background.
  - **Barge-in / Interruption:** Start speaking or press any key to immediately cut off Jarvis's speech.
- **Cognitive Brain (local-first, explicit routing):**
  - Local Qwen 3.5 2B via Ollama for normal conversation, screen vision, and tool use without API credits.
  - Optional Gemini, Grok, Claude, and GitHub Copilot CLI specialist consultations. Antigravity requests switch to Copilot CLI only on a detected quota/rate-limit failure and only when the CLI is available.
  - Multimodal desktop vision: Ask *"Jarvis, look at my screen"* or *"Explain this error"* to analyze your active desktop.
- **Hardware-Accelerated STT:** `faster-whisper` running locally on your NVIDIA GeForce RTX 3050 GPU using CUDA.
- **Procedural High-Tech Audio Cues:** Synthesized Stark Industries chimes for wake, thinking, done, and alerts.
- **Persistent Memory:** SQLite database (`data/jarvis_memory.db`) remembering your personal facts, preferences, habits, and tasks across sessions.
- **Safety Gate:** Blocks a set of recognized destructive PowerShell commands and aliases. This is a pattern-based guard, not a sandbox or a substitute for reviewing commands; other registered tools can still change data.
- **Tool Suite:**
  - **Desktop & OS Automation:** Launch apps, adjust volume, control media playback, show desktop.
  - **Web & Live Knowledge:** DuckDuckGo web search, Open-Meteo real-time global weather, Wikipedia.
  - **Developer Shell:** PowerShell execution and directory/file inspection.
  - **Productivity:** Task list, reminders, timestamps, facts storage.
  - **MCP Configuration:** Server configuration is present, but MCP connections and tool discovery are not implemented yet.
- **Windows System Tray:** Runs quietly in the background with an Arc-Reactor icon.

---

## Quick Start

### 1. Install Python dependencies
Create a virtual environment and install the project dependencies:
```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

### 2. Install the local model
Install [Ollama for Windows](https://ollama.com/download/windows), then download the configured model once:
```powershell
ollama pull qwen3.5:2b-q4_K_M
```
Jarvis starts Ollama's local server when needed. Inference and background memory extraction stay on this machine and do not consume API credits. The model is about 1.9 GB; its quality is below larger cloud models, and response speed depends on this PC. Jarvis requires Ollama and this model to be available locally.

### 3. Launch Jarvis
Activate the virtual environment and run:
```powershell
.\.venv\Scripts\python.exe run.py
```

### 4. Usage
- **Hold End:** Speak your request, then release End. Change `audio.ptt_trigger` in `config/config.yaml` to use another key.
- **Say "Hey Jarvis":** Jarvis wakes up, chimes, and listens.
- **Type in terminal:** Type your question directly in the `[Sir]:` prompt and hit Enter.
- **Request a specialist:** Say or type `consult gemini: <question>`, `consult groq: <question>`, `consult grok: <question>`, `consult claude: <question>`, or `consult copilot: <question>`. Only the named provider receives the prompt; Jarvis does not forward normal conversation history.

### Provider and cost behavior

- Ollama remains the configured primary model and handles ordinary conversation locally.
- Gemini consultation is opt-in per request and requires `GEMINI_API_KEY`. The selected Gemini 3.8 Flash model has a Google AI Studio free tier, but it has model-specific quotas and rate limits; a key attached to a billed project can incur charges.
- OpenRouter can be used for ordinary turns by setting `llm.provider` to `openrouter`, or consulted explicitly with `consult openrouter: <question>`. It requires `OPENROUTER_API_KEY` in the local `.env`; `llm.openrouter_model` selects the first model and the remaining configured `:free` endpoints are tried after model errors. These endpoints have changing availability and rate limits, and Jarvis does not fall back to a metered provider.
- Gemini Free-tier prompts are sent to Google and may be used to improve Google's products, per its API pricing terms. Do not include sensitive content in a free-tier consultation.
- Groq consultation is opt-in per request and requires `GROQ_API_KEY`; Groq documents free-plan quotas, which vary by model and account. A billed Developer-plan account can incur charges.
- GitHub Copilot CLI requires an active Copilot subscription and a one-time `copilot login` (`winget install GitHub.Copilot` on Windows). Jarvis invokes it in prompt mode using a custom agent with an empty tool allowlist, plus explicit shell/write denials. Copilot subscription usage limits apply. The retired GitHub Models inference API is not used.
- Grok and Claude API consultations are blocked by default. Their developer APIs are usage-billed, so enabling `llm.specialists.allow_metered_api_calls` is an explicit configuration change that can incur charges.
- The HUD lists the active primary provider and whether specialist credentials/permissions are configured. Cloud calls do not happen automatically when a local request fails, except the explicitly configured Antigravity quota/rate-limit handoff to GitHub Copilot CLI.

---

## Architecture & Customization

- Configuration settings: `config/config.yaml`
- Adding MCP servers: `config/mcp_servers.json`
- Adding new custom tools: add `@SkillRegistry.register` decorated functions in `jarvis/skills/`
- Running test suite:
  ```powershell
  .\.venv\Scripts\python.exe -m unittest discover tests
  ```

Audit and follow-up: [findings](./AUDIT_REPORT.md), [baseline](./BASELINE_REPORT.md), [enhancements](./ENHANCEMENTS.md), [roadmap](./ROADMAP.md), and [validation](./FINAL_VALIDATION.md).
