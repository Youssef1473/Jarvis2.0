# Jarvis 2.0 — Sovereign Voice-First AI Desktop Operating System

[![Jarvis 2.0 Sovereign Test Gate](https://github.com/Youssef1473/Jarvis2.0/actions/workflows/ci.yml/badge.svg)](https://github.com/Youssef1473/Jarvis2.0/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.12](https://img.shields.io/badge/Python-3.12-brightgreen.svg)](https://www.python.org/downloads/release/python-3120/)
[![Platform: Windows 11](https://img.shields.io/badge/Platform-Windows%2011%2064--bit-0078D6.svg)](https://www.microsoft.com/windows)
[![CUDA 12.x](https://img.shields.io/badge/CUDA-RTX%203050%20(4GB)-76B900.svg)](https://developer.nvidia.com/cuda-toolkit)

An autonomous, full-duplex desktop AI operating system daemon engineered from the bare metal up for **Windows 11**. Jarvis 2.0 combines an authentic cinematic J.A.R.V.I.S. persona (voiced by Paul Bettany via Kokoro-82M ONNX latent blending), hardware-pinned MMCSS audio workers, low-latency barge-in cancellation (<1ms), a 4-tier vector/lexical memory pyramid, and closed-loop OS automation via Chrome DevTools Protocol (CDP) and Windows UI Automation (pywinauto).

---

## 1. System Architecture & Data Flow

Jarvis 2.0 operates as a deterministic, real-time operating system service with strict hardware-level isolation across five sovereign pipeline stages:

```
[1. INPUT]
  WASAPI Mic & Loopback / Push-to-Talk (End key)
  └──► MMCSS "Pro Audio" Scheduling & P-Core Thread Pinning (Mask: 0xFF)
  └──► Lock-Free C-Level AudioRingBuffer (120s Circular Buffer)

[2. INGESTION]
  Unified Barge-In & Acoustic Gate (~0.74µs epoch cut + 2.5ms cos^2 anti-click)
  ├──► faster-whisper CUDA INT8 Transcription (with DXGI/NVML VRAM Arbiter)
  ├──► ContextOrchestrator (<1.5KB prompt budget & dynamic token bounding)
  └──► LIFO ReasoningBookmarkStack (clause snapshotting on speech interruption)

[3. INFERENCE]
  Tri-Tier Cognitive Routing & Antigravity Sidecar
  ├──► Tier 0 (<15ms): ReflexDispatcher & DynamicReflexEngine (regex/habits)
  ├──► Tier 1 (CPU):   ONNX Local Intent Classifier (sub-10ms neural routing)
  └──► Tier 2 (LLM):   Antigravity Bridge (Claude Sonnet 3.5 / Gemini 3.8 Flash)
                       └──► StreamingToolParser (O(1) state-machine tool chunking)

[4. EXECUTION]
  Closed-Loop Desktop & Browser Automation
  ├──► Chrome DevTools Protocol (CDP 127.0.0.1:9222) JSON-RPC WebSocket
  ├──► Windows UIA (pywinauto) with IsHungAppWindow deadlock protection
  └──► SafetyGate: Token-sandboxed PowerShell runner on isolated E-cores

[5. SPEECH SYNTHESIS & DSP]
  Neural Kokoro-82M ONNX + Paul Bettany DSP Chain
  ├──► Latent Style Blending: 50% bm_fable + 30% bm_george + 20% bm_daniel
  ├──► 32-sample FIR Polyphase Upsampler (24kHz ONNX -> 48kHz WASAPI DAC)
  ├──► Acoustic DSP: 110Hz HPF + 7.5ms comb micro-reflection (-21.4 dB wet)
  └──► Calibrated Cadence: 130ms [,;:] clause pauses, 240ms [.!?] sentence pauses
```

---

## 2. Key Subsystem Highlights

### Authentic Paul Bettany Voice Persona & Acoustic DSP
* **Latent Style Blending:** Normalizes linear combinations of $510 \times 1 \times 256$ float style tensors in StyleTTS-2 latent space (`50% bm_fable + 30% bm_george + 20% bm_daniel`), delivering authentic British diction and dry wit.
* **Polyphase Boundary Reconstruction:** Features `StatefulPolyphaseUpsampler` with a 32-sample Kaiser-windowed FIR filter to eliminate WASAPI resampler clicks, high-frequency distortion, and boundary discontinuities.
* **Sub-Millisecond Barge-in:** Voice Activity Detection (VAD) cuts audio ring buffers and invalidates stale speech via atomic epoch transitions (~0.74µs epoch cut, ~5.6µs PCM read/write), preserving active reasoning context via `ReasoningBookmarkStack`.

### 4-Tier Sovereign Memory Pyramid
* **Tier 1 (Core Preferences):** Stored in `user_preferences` (<1KB); injected on every conversational turn under `<core_user_preferences>`.
* **Tier 2 (Atomic Facts):** Single-predicate assertions indexed via `facts_vec` (`sqlite-vec` 64-dim float embeddings) and FTS5 BM25 virtual tables, fused via Reciprocal Rank Fusion (RRF). Recalled in $<5\text{ms}$.
* **Tier 3 (JIT Document Vaults):** Multi-page PDFs, manuals, and workspace documents ingested via `process_staged_document` and queried just-in-time via `search_archived_documents` with chunk sandwiching.
* **Tier 4 (Episodic Lessons):** Reflexion heuristics distill failure post-mortems into `lessons_vec` to permanently eliminate recurring tool errors.
* **Storage Invariants:** SQLite WAL mode with `synchronous=NORMAL`, 256MB memory-mapped I/O (`mmap_size=268435456`), and multi-reader connection pooling via `ro_pool.py`.

### Closed-Loop OS & Browser Control
* **Silent Browser Automation:** Native Chrome DevTools Protocol (`127.0.0.1:9222`) automation for Brave, Chrome, and Edge (`open_browser_tab`, `close_browser_tab`, `read_webpage_content_cdp`) without window thrashing.
* **Win32 Accessibility Tree (UIA):** Idempotent window focusing with DWM cloaking awareness (`is_window_dwm_cloaked`), `IsHungAppWindow` anti-hang safeguards, and synthetic `VK_MENU` fallback keystrokes.
* **Visual Privacy Guard:** Zero-exposure window privacy guard that checks foreground HWNDs against password managers and crypto wallets, halting screen captures when sensitive windows are active.

---

## 3. Master Capabilities & Autonomous Operations (143 Registered Tools)

Jarvis 2.0 exposes **143 atomic capabilities across 36 specialized modules**, governed by the central `SkillRegistry`, dual-key safety gates, and physical closed-loop post-condition verification.

### 3.1 Autonomous Creation & Self-Evolution (What Jarvis Can Build)
* **Autonomous Skill Authoring & Hot-Reloading (`jarvis.skills.self_evolution`):** Synthesizes, AST-validates, and sandbox-executes brand new Python skills in isolated worker runtimes, then hot-injects them directly into the live brain registry without daemon downtime (`create_or_update_custom_skill`, `list_custom_skills`, `delete_custom_skill`).
* **Reflex Macro Compilation (`jarvis.skills.reflex_macro_compiler`):** Detects recurring multi-step tool execution sequences and compiles them into persistent, deterministic `<15ms` local execution macros executed directly by `DynamicReflexEngine` (`compile_workflow_reflex`, `execute_workflow_reflex`, `list_compiled_reflex_macros`).
* **AST Code Surgery & Self-Healing Tests (`jarvis.skills.ast_code_surgeon`, `jarvis.skills.dev_shell`):** Analyzes failing pytest traces down to AST node coordinates and generates surgical patches to fix broken code and green-light suites autonomously (`diagnose_test_failure_ast`, `apply_and_verify_surgical_patch`, `patch_file`).
* **Deep Research Dossier Synthesis (`jarvis.skills.deep_research`):** Conducts autonomous recursive web research across multiple search endpoints, corroborates technical findings, and synthesizes structured Markdown briefings (`deep_research`).
* **Document Vault Distillation (`jarvis.skills.document_ingest`):** Ingests staged PDFs, research papers, and technical manuals, extracts core deadlines and specifications, and formats them into atomic vector memories using attention-optimizing chunk sandwiching (`process_staged_document`, `read_archived_document`, `search_archived_documents`).
* **Email & Communication Drafting (`jarvis.skills.email_manager`):** Composes, sanitizes, previews, and transmits messages via Gmail and Outlook SMTP with strict preview gating (`send_email`, `check_unread_emails`, `read_email_details`, `search_emails`).

### 3.2 Physical Desktop OS & Workstation Control (Windows 11)
* **Idempotent Window Activation & Layout (`jarvis.skills.os_control`):** Manages window lifecycle with `safe_set_foreground_window`, `IsHungAppWindow` anti-deadlock protection, and DWM cloaking verification (`launch_or_focus_application`, `focus_window`, `close_window`, `list_open_windows`, `minimize_all_windows`, `toggle_window_maximize`, `position_window`).
* **Hardware Endpoint Audio Verification (`jarvis.skills.os_control`):** Controls master audio endpoints and hardware volume levels with closed-loop CoreAudio/WASAPI verification (`adjust_volume`, `get_current_volume`, `mute_system_audio`, `unmute_system_audio`, `media_control`).
* **Native Windows UI Automation (`jarvis.skills.windows_uia`):** Traverses and controls native application controls, buttons, menus, and input fields via `pywinauto` without synthetic mouse clicks (`inspect_window_controls`).
* **Sub-2ms Process & File Lock Surgery (`jarvis.skills.rstrtmgr_lock_surgeon`):** Identifies locking process IDs in `<2ms` via Windows `Rstrtmgr.dll` and surgically frees locked files or folders while shielding system-critical processes; identifies port listeners via `iphlpapi.dll` (`inspect_file_locks`, `release_file_lock`, `inspect_port_holders`).
* **Sub-15ms Drive-Wide Indexing (`jarvis.skills.everything_search`):** Locates files and codebases instantly across all local NTFS/FAT partitions via Voidtools Everything IPC with zero disk thrashing (`search_everything`).
* **Sub-1ms NTFS USN Journal Radar (`jarvis.skills.usn_journal_delta_radar`):** Detects real-time source file mutations and file creations in `<1ms` via low-level NTFS USN Journal tracking (`get_recent_workspace_mutations`, `record_workspace_mutation`).
* **Hardware Sentinel & Maintenance (`jarvis.skills.system_watchdog`, `jarvis.skills.system_maintenance`):** Monitors workstation thermals, battery level, and CPU load via a background watchdog sentinel, identifies top RAM-consuming processes, cleans temporary files, and flushes DNS caches (`check_hardware_health`, `start_hardware_watchdog`, `stop_hardware_watchdog`, `inspect_ram_hogs`, `optimize_workstation`).

### 3.3 Silent Web Automation & Browser Intelligence
* **Silent Chrome DevTools Protocol Control (`jarvis.skills.browser_cdp`):** Manages Brave, Chrome, and Edge sessions via port 9222 JSON-RPC frames without window thrashing or clipboard hijacking (`open_browser_tab`, `close_browser_tab`, `list_open_browser_tabs`, `read_webpage_content`, `switch_to_browser_tab`, `launch_debug_browser`).
* **Semantic Web Actuation Across Shadow DOMs (`jarvis.skills.hybrid_actuator`):** Locates and activates complex web elements and text inputs across nested shadow boundaries using CDP Accessibility Trees and layout centroid coordinates (`click_web_element_semantic`, `type_into_web_element_semantic`).
* **Direct Webpage JIT Archival (`jarvis.skills.browser_cdp`):** Ingests web documentation and articles straight from active browser tabs into the local vector vault (`scrape_and_archive_webpage`).
* **Headless Playwright Automation (`jarvis.skills.browser_agent`):** Executes multi-step browser tasks in the background when CDP endpoints are unavailable (`run_browser_task`, `navigate_to_url`, `click_web_element`, `extract_page_content`).
* **Live Search & Telemetry (`jarvis.skills.web_search`):** Fetches real-time internet intelligence via DuckDuckGo and worldwide meteorological telemetry via Open-Meteo (`web_search`, `get_weather`, `get_wikipedia_summary`).

### 3.4 Acoustic, Voice & Conversational Full-Duplex Subsystem
* **Neural Bettany Voice Synthesis (`jarvis.audio.tts`):** Generates 24kHz speech via Kokoro-82M ONNX using a custom latent style tensor blend (`50% bm_fable + 30% bm_george + 20% bm_daniel`) pinned to dedicated P-core threads (`0xFF`).
* **MCU DSP Audio Chain:** Applies a 110Hz high-pass filter, 7.5ms micro-reflection comb filter (-21.4 dB wet), 5ms Hann cross-fading, 32-sample polyphase upsampling to 48kHz, and natural conversational breath pauses (130ms / 240ms).
* **Instant Barge-In Interruption:** Clears audio ring buffers atomically in sub-millisecond cycles (~0.74µs epoch invalidation) whenever you speak mid-sentence, snapshotting active reasoning bookmarks onto `ReasoningBookmarkStack`.
* **GPU-Accelerated Transcription (`jarvis.audio.stt`):** Low-latency CUDA INT8 Whisper transcription via `faster-whisper`, governed by dynamic silence scaling and VAD hesitation damping.
* **Sub-1ms Timbre Switching & Device Routing (`jarvis.skills.voice_control`):** Shifts voice profiles across Bettany, Classic, Authoritative, Crisp, and Friday personas, tunes speech speed, and dynamically re-routes audio output devices (`set_voice_profile`, `get_voice_settings`, `set_speech_rate`, `list_audio_devices`, `set_audio_output_device`).

### 3.5 Sovereign 4-Tier Memory & Knowledge Pyramid
* **Tier 1: Core User Preferences (`<1KB`) (`jarvis.skills.productivity`):** Maintains self-editing personal habits, naming preferences, and directives injected into working memory on every turn (`update_user_preference`, `get_user_preferences`, `delete_user_preference`).
* **Tier 2: Atomic Facts & Semantic Recall (`jarvis.skills.productivity`):** Queries single-predicate assertions via native `sqlite-vec` 64-dim float embeddings fused with FTS5 BM25 through Hybrid Reciprocal Rank Fusion (`remember_fact`, `recall_fact`, `delete_stored_fact`, `semantic_search_memory`, `recall_past_conversation`).
* **Tier 3: Just-In-Time Document Vaults (`jarvis.skills.document_ingest`):** Reads technical manuals, syllabus documents, and project notes queried strictly JIT with Chunk Sandwiching to eliminate prompt bloat (`search_archived_documents`, `read_archived_document`).
* **Tier 4: Episodic Lessons & Mistake Avoidance (`jarvis.skills.self_evolution`):** Indexes post-mortem failure logs via Reflexion heuristics to prevent recurring errors across sessions (`record_episodic_lesson`, `recall_episodic_lessons`, `verify_learned_habits`).

### 3.6 Multimodal Perception & Visual Privacy Guard
* **Automated Visual Privacy Guard (`jarvis.vision.privacy_guard`):** Scans active window handles and process titles against password managers, cryptocurrency wallets, and banking interfaces before frame capture, halting immediately if sensitive data is detected.
* **Set-of-Marks UI Grounding (`jarvis.skills.screen_vision`):** Overlays numbered bounding badges over interactable UI components on screen for precise visual targeting (`inspect_screen_with_marks`, `click_marked_element`).
* **Multi-Tier Cloud Vision Cascade (`jarvis.skills.screen_vision`, `jarvis.skills.custom.see_screen`, `jarvis.skills.photo_manager`):** Processes workstation screen frames through a 12-second bounded Gemini cascade with zero local VRAM overhead, falling back to semantic UIA if offline (`inspect_screen`, `see_screen`, `describe_photo`, `save_photo`, `save_clipboard_photo`, `list_photos`).

### 3.7 Cognitive Triad & Multi-Agent Deliberation
* **Tri-Tier Cognitive Routing (`jarvis.core.cognition`):** Directs requests through Tier 0 (`<15ms` local regexes), Tier 1 (local CPU ONNX intent classification), and Tier 2 (Antigravity CLI Gemini 3.8 Flash Low/Medium/High and Claude 3.7/Sonnet 4.6 with thinking budgets).
* **The Council of Advisors (`jarvis.core.council`):** Convenes Systems Architect, Security Auditor, and Pragmatic Implementer agents for multi-perspective consensus on high-blast-radius architecture decisions.
* **Specialist Cloud Consultations (`jarvis.core.brain`):** Dispatches focused queries to Gemini, Claude, Copilot, Grok, and Groq specialists (`JarvisBrain.consult_specialist`).
* **Sub-Second Quota Failover:** Applies a 900-second cooldown and routes from Claude to Gemini 3.8 Flash High in `<10ms` upon any 429 quota exhaustion.

### 3.8 Developer Shell, Sandboxing & Hardware Safeguards
* **Kernel Job Object Sandboxing (`jarvis.skills.job_object_sandbox`, `jarvis.skills.dev_shell`):** Constrains untrusted or heavy processes inside Windows Job Objects pinned to E-cores (`0xFF00`) with strict RAM caps (`run_job_sandboxed_process`, `execute_sandboxed_powershell`).
* **Codebase Intelligence Tools (`jarvis.skills.dev_shell`):** Conducts structural AST code searches via `ast-grep`, executes static linting and syntax checks via `Ruff` in `<20ms`, and traces codebase caller graphs via Graphify (`ast_search_codebase`, `check_code_diagnostics`, `query_codebase_graph`, `search_codebase`, `read_file`, `read_multiple_files`, `write_file`, `patch_file`, `list_directory`, `git_status`, `git_diff`).
* **Asynchronous Background Execution (`jarvis.skills.dev_shell`):** Runs terminal compilations, test suites, and downloads asynchronously with proactive completion chimes (`run_background_task`, `list_background_tasks`, `get_background_task_output`, `cancel_background_task`).
* **NVML/DXGI VRAM Arbiter (`jarvis.skills.nvml_dxgi_vram_arbiter`):** Guards the 4GB RTX 3050 Laptop GPU against allocation spikes and out-of-memory crashes (`get_vram_allocation_status`).
* **Live Diagnostics & Profiling (`jarvis.skills.diagnostics`):** Runs 360-degree self-diagnostic scans (`jarvis_doctor`, `run_system_troubleshooting`), zero-overhead sampling profiler (`profile_runtime` via `py-spy`), heap audit (`audit_memory_heap`), and turn latency statistics (`get_pipeline_latency_stats`).

### 3.9 Executive Productivity, Protocols & Daily Coordination
* **Morning Executive Protocol (`jarvis.skills.calendar_manager`):** Delivers a coordinated morning briefing covering date, time, weather, calendar agenda, pending tasks, and global headlines (`morning_protocol`).
* **Coordinated Workstation Protocols (`jarvis.skills.protocols`):** Executes multi-action sequences for `work`, `lockdown`, `relax`, `night`, and `focus` operational modes (`execute_protocol`).
* **Timers, Tasks & Voice Notes (`jarvis.skills.timers`, `jarvis.skills.notes`, `jarvis.skills.productivity`):** Captures persistent reminders, spoken thoughts, and countdown timers via voice (`start_timer`, `cancel_timer`, `list_active_timers`, `add_task_or_reminder`, `complete_task`, `list_pending_tasks`, `take_quick_note`, `read_recent_notes`, `search_notes`).
* **Calendar Scheduling (`jarvis.skills.calendar_manager`):** Full scheduling lifecycle for appointments and meetings (`schedule_calendar_event`, `list_calendar_events`, `cancel_calendar_event`, `delete_calendar`).
* **Media DJ (`jarvis.skills.media_dj`):** Controls browser playback for music, soundtracks, and videos on YouTube (`play_youtube_media`).
* **Project & Folder Navigation (`jarvis.skills.project_manager`):** Instant opening of repositories in Visual Studio Code and user folders in Windows Explorer (`open_project`, `open_user_folder`).

---

### 3.10 Registered Skill Modules Matrix (36 Modules | 143 Tools)

| Module | Tools | Primary Domain | Core Functions |
| :--- | :---: | :--- | :--- |
| `jarvis.skills.ast_code_surgeon` | 2 | AST Code Repair | `diagnose_test_failure_ast`, `apply_and_verify_surgical_patch` |
| `jarvis.skills.briefing` | 1 | Executive Intel | `get_daily_briefing` |
| `jarvis.skills.browser_agent` | 4 | Headless Automation | `run_browser_task`, `navigate_to_url`, `click_web_element`, `extract_page_content` |
| `jarvis.skills.browser_cdp` | 6 | Silent CDP Browser Control | `open_browser_tab`, `close_browser_tab`, `list_open_browser_tabs`, `read_webpage_content`, `switch_to_browser_tab`, `scrape_and_archive_webpage` |
| `jarvis.skills.calendar_manager` | 5 | Schedule Management | `schedule_calendar_event`, `list_calendar_events`, `cancel_calendar_event`, `delete_calendar`, `morning_protocol` |
| `jarvis.skills.chrome_devtools` | 3 | DevTools Auditing & Snapshots | `audit_webpage_accessibility`, `audit_webpage_performance`, `capture_devtools_snapshot` |
| `jarvis.skills.custom.see_screen` | 1 | Real-time Vision | `see_screen` |
| `jarvis.skills.deep_research` | 1 | Autonomous Web Research | `deep_research` |
| `jarvis.skills.dev_shell` | 18 | Workspace & Developer CLI | `execute_powershell`, `execute_sandboxed_powershell`, `read_file`, `write_file`, `patch_file`, `ast_search_codebase`, `check_code_diagnostics`, `query_codebase_graph`, `run_background_task` |
| `jarvis.skills.diagnostics` | 5 | System Troubleshooting | `jarvis_doctor`, `run_system_troubleshooting`, `profile_runtime`, `audit_memory_heap`, `get_pipeline_latency_stats` |
| `jarvis.skills.document_ingest` | 4 | JIT Document Ingestion | `process_staged_document`, `list_staged_documents`, `read_archived_document`, `search_archived_documents` |
| `jarvis.skills.email_manager` | 4 | Email Operations | `send_email`, `check_unread_emails`, `read_email_details`, `search_emails` |
| `jarvis.skills.everything_search` | 1 | Sub-15ms Drive Search | `search_everything` |
| `jarvis.skills.gemini_intelligence` | 3 | Gemini Docs & Live API | `search_gemini_api_documentation`, `get_gemini_documentation_chunk`, `get_gemini_live_architecture_overview` |
| `jarvis.skills.hybrid_actuator` | 2 | Shadow DOM Actuation | `click_web_element_semantic`, `type_into_web_element_semantic` |
| `jarvis.skills.job_object_sandbox` | 1 | Kernel E-Core Sandboxing | `run_job_sandboxed_process` |
| `jarvis.skills.media_dj` | 1 | YouTube Media Control | `play_youtube_media` |
| `jarvis.skills.modern_web` | 2 | Modern Web Guidance | `search_modern_web_guidance`, `retrieve_modern_web_guide` |
| `jarvis.skills.notes` | 3 | Spoken Voice Notes | `take_quick_note`, `read_recent_notes`, `search_notes` |
| `jarvis.skills.nvml_dxgi_vram_arbiter`| 1 | GPU Memory Protection | `get_vram_allocation_status` |
| `jarvis.skills.os_control` | 21 | Windows 11 Desktop Control | `launch_or_focus_application`, `focus_window`, `close_window`, `adjust_volume`, `mute_system_audio`, `media_control`, `take_desktop_screenshot`, `shutdown_jarvis` |
| `jarvis.skills.photo_manager` | 4 | Visual Media Management | `describe_photo`, `list_photos`, `save_photo`, `save_clipboard_photo` |
| `jarvis.skills.productivity` | 12 | Core Memory & Tasks | `update_user_preference`, `get_user_preferences`, `remember_fact`, `recall_fact`, `semantic_search_memory`, `add_task_or_reminder`, `complete_task` |
| `jarvis.skills.project_manager` | 2 | VS Code & Folders | `open_project`, `open_user_folder` |
| `jarvis.skills.protocols` | 1 | Operational Modes | `execute_protocol` (`work`, `lockdown`, `relax`, `night`, `focus`) |
| `jarvis.skills.reflex_macro_compiler`| 4 | Reflex Compilation | `compile_workflow_reflex`, `execute_workflow_reflex`, `list_compiled_reflex_macros`, `delete_compiled_reflex_macro` |
| `jarvis.skills.rstrtmgr_lock_surgeon` | 3 | Sub-2ms Lock Release | `inspect_file_locks`, `release_file_lock`, `inspect_port_holders` |
| `jarvis.skills.screen_vision` | 3 | Set-of-Marks Vision | `inspect_screen`, `inspect_screen_with_marks`, `click_marked_element` |
| `jarvis.skills.self_evolution` | 6 | Autonomous Skill Synthesis | `create_or_update_custom_skill`, `list_custom_skills`, `delete_custom_skill`, `record_episodic_lesson`, `recall_episodic_lessons`, `verify_learned_habits` |
| `jarvis.skills.system_maintenance` | 2 | Storage & Cache Hygiene | `inspect_ram_hogs`, `optimize_workstation` |
| `jarvis.skills.system_watchdog` | 3 | Real-Time Hardware Sentinel | `check_hardware_health`, `start_hardware_watchdog`, `stop_hardware_watchdog` |
| `jarvis.skills.timers` | 3 | Voice Timers & Countdowns | `start_timer`, `cancel_timer`, `list_active_timers` |
| `jarvis.skills.usn_journal_delta_radar`| 2 | Sub-1ms NTFS Radar | `get_recent_workspace_mutations`, `record_workspace_mutation` |
| `jarvis.skills.voice_control` | 5 | Timbre & Audio Routing | `set_voice_profile`, `get_voice_settings`, `set_speech_rate`, `list_audio_devices`, `set_audio_output_device` |
| `jarvis.skills.web_search` | 3 | Web Search & Weather | `web_search`, `get_weather`, `get_wikipedia_summary` |
| `jarvis.skills.windows_uia` | 1 | Semantic Accessibility Tree | `inspect_window_controls` |

---

## 4. Host Hardware Envelope & Verification Baseline

| Subsystem | Specification / Target Constraint |
| :--- | :--- |
| **CPU** | Intel Core i5-12500H (4 P-Cores / 8 Threads + 8 E-Cores) |
| **GPU** | NVIDIA GeForce RTX 3050 Laptop GPU (4GB VRAM Hard Ceiling) |
| **OS** | Windows 11 Pro 64-bit |
| **Audio DAC** | Realtek ALC / WASAPI 48kHz 16-bit Stereo DAC |
| **Python** | Python 3.12 (Virtual Environment: `.venv`) |
| **Test Architecture** | Two-tier test runner (`tools/run_fast_tests.py`): fast parallel suite + isolated serial hardware suite |

---

## 5. Quick Start

### 1. Prerequisites & Virtual Environment
Ensure Python 3.12 is installed on Windows 11:
```powershell
# Clone the repository
git clone https://github.com/Youssef1473/Jarvis2.0.git
cd Jarvis2.0

# Create and activate virtual environment
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1

# Install production dependencies
pip install --upgrade pip
pip install -r requirements.txt

# (Optional) Install development and test dependencies
pip install -r requirements-dev.txt
```

### 2. Configure Environment & Keys
Copy the template configuration and provide your optional cloud specialist API keys in `.env`:
```powershell
# Core configuration
Copy-Item config/config.yaml.example config/config.yaml -ErrorAction SilentlyContinue

# Set environment variables for optional specialist consultation
Set-Content .env "GEMINI_API_KEY=your_key_here`nANTHROPIC_API_KEY=your_key_here"
```

### 3. Launch Jarvis
```powershell
.\.venv\Scripts\python.exe run.py
```

### 4. Interactive Voice & Terminal Controls
* **Push-to-Talk (PTT):** Hold the `End` key (`fin`), speak your directive, then release.
* **Wake Word:** Say *"Hey Jarvis"* to activate hands-free microphone listening.
* **Terminal Input:** Enter commands directly into the terminal prompt.
* **Holographic HUD:** Open `http://127.0.0.1:20129` in any browser to inspect live acoustic telemetry, working memory tokens, and active tool execution streams.

---

## 6. Development, Testing & Verification

Running the test suite requires the development dependencies installed via `pip install -r requirements-dev.txt`. Jarvis 2.0 includes a specialized high-performance test runner implementing intelligent two-tier sharding:

```powershell
# Run the fast parallel unit test suite (non-hardware)
python tools/run_fast_tests.py --fast

# Run a specific targeted test file
python tools/run_fast_tests.py tests/test_security_boundaries.py

# Run the serial hardware/audio suite (CUDA, WASAPI, and ONNX endpoints)
python tools/run_fast_tests.py --hardware

# Run the complete test gate
python tools/run_fast_tests.py --all
```

---

## 7. Cryptographic Commit Verification Protocol (Recommended)

Contributors are encouraged to cryptographically sign commits using standard Ed25519 SSH keys. To configure Git on Windows 11 for verified commit status:

```powershell
# 1. Generate a dedicated Ed25519 SSH signing key
ssh-keygen -t ed25519 -C "your_email@example.com" -f "$HOME\.ssh\id_ed25519_signing" -N '""'

# 2. Configure Git to use SSH signing globally
git config --global gpg.format ssh
git config --global user.signingkey "$HOME/.ssh/id_ed25519_signing.pub"
git config --global commit.gpgsign true

# 3. Add the public key to your GitHub account under:
# GitHub -> Settings -> SSH and GPG keys -> New SSH Key (Key type: Signing Key)
Get-Content "$HOME\.ssh\id_ed25519_signing.pub"
```

---

## 8. License

This project is licensed under the [MIT License](LICENSE) — see the [`LICENSE`](LICENSE) file for details.

Copyright (c) 2026 Drake (Youssef Chamroukhi).
