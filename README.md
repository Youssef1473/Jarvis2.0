# Jarvis 2.0 — Sovereign Voice-First AI Desktop Operating System

[![Jarvis 2.0 Sovereign Test Gate](https://github.com/Youssef1473/Jarvis2.0/actions/workflows/ci.yml/badge.svg)](https://github.com/Youssef1473/Jarvis2.0/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.12](https://img.shields.io/badge/Python-3.12-brightgreen.svg)](https://www.python.org/downloads/release/python-3120/)
[![Platform: Windows 11](https://img.shields.io/badge/Platform-Windows%2011%2064--bit-0078D6.svg)](https://www.microsoft.com/windows)
[![CUDA 12.x](https://img.shields.io/badge/CUDA-RTX%203050%20(4GB)-76B900.svg)](https://developer.nvidia.com/cuda-toolkit)

An autonomous, full-duplex desktop AI operating system daemon engineered from the bare metal up for **Windows 11**. Jarvis 2.0 combines an authentic cinematic J.A.R.V.I.S. persona (voiced by Paul Bettany via Kokoro-82M ONNX latent blending), hardware-pinned MMCSS audio workers, sub-microsecond barge-in cancellation, a 4-tier vector/lexical memory pyramid, and closed-loop OS automation via Chrome DevTools Protocol (CDP) and Windows UI Automation (pywinauto).

---

## 1. System Architecture & Data Flow

Jarvis 2.0 operates as a deterministic, real-time operating system service with strict hardware-level isolation across five sovereign pipeline stages:

```
[1. INPUT]
  WASAPI Mic & Loopback / Push-to-Talk (End key)
  └──► MMCSS "Pro Audio" Scheduling & P-Core Thread Pinning (Mask: 0xFF)
  └──► Lock-Free C-Level AudioRingBuffer (120s Circular Buffer)

[2. INGESTION]
  Unified Barge-In & Acoustic Gate (sub-1us ring clear + 2.5ms cos^2 anti-click)
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
* **Sub-Microsecond Barge-in:** Voice Activity Detection (VAD) instantly clears audio buffers and rolls off gain in $<1\mu\text{s}$, preserving active reasoning context via `ReasoningBookmarkStack`.

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

## 3. Host Hardware Envelope & Verification Baseline

| Subsystem | Specification / Target Constraint |
| :--- | :--- |
| **CPU** | Intel Core i5-12500H (4 P-Cores / 8 Threads + 8 E-Cores) |
| **GPU** | NVIDIA GeForce RTX 3050 Laptop GPU (4GB VRAM Hard Ceiling) |
| **OS** | Windows 11 Pro 64-bit |
| **Audio DAC** | Realtek ALC / WASAPI 48kHz 16-bit Stereo DAC |
| **Python** | Python 3.12 (Virtual Environment: `.venv`) |
| **Test Baseline** | 106 test suites (601 fast parallel + 66 hardware serial = 667 total tests) |

---

## 4. Quick Start

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
* **Holographic HUD:** Open `http://127.0.0.1:8000` in any browser to inspect live acoustic telemetry, working memory tokens, and active tool execution streams.

---

## 5. Development, Testing & Verification

Jarvis 2.0 includes a specialized high-performance test runner implementing intelligent two-tier sharding:

```powershell
# Run the fast parallel unit test suite (601 tests across worker processes)
.\.venv\Scripts\python.exe tools/run_fast_tests.py --fast

# Run the serial hardware/audio suite (CUDA, WASAPI, and ONNX endpoints)
.\.venv\Scripts\python.exe tools/run_fast_tests.py --hardware

# Run the complete test gate
.\.venv\Scripts\python.exe tools/run_fast_tests.py --all

# Standard unittest discovery
.\.venv\Scripts\python.exe -m unittest discover tests
```

---

## 6. Cryptographic Commit Verification Protocol

All commits to this repository are cryptographically signed using standard Ed25519 SSH keys. To configure Git on Windows 11 for verified commit status:

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

## 7. License

This project is licensed under the [MIT License](LICENSE) — see the [`LICENSE`](LICENSE) file for details.

Copyright (c) 2026 Drake (Youssef Chamroukhi).
