---
name: win32-com-ctypes-hardening
description: >-
  Mandatory engineering playbook and failure-mode checklist for writing 64-bit Win32 ctypes bindings,
  SetWinEventHook/SetWindowsHookEx callbacks, GDI/Kernel handle lifecycle, and COM STA/MTA threading in Jarvis.
  Load whenever modifying jarvis/vision/, jarvis/audio/, jarvis/skills/windows_uia.py, or any module importing ctypes.
---

# Win32, COM & ctypes Hardening Playbook (Windows 11 64-Bit)

This skill establishes the mandatory rules and safety patterns for developing low-level C and Win32 interfaces within the Jarvis 2.0 codebase.

---

## 1. The 64-Bit Pointer Truncation Law (`restype` & `argtypes`)

- **Fatal Bug:** By default, Python's `ctypes` assumes every foreign function returns a 32-bit `c_int`. On 64-bit Windows 11, `HWND`, `HANDLE`, `HHOOK`, `HDC`, `HMODULE`, `WPARAM`, and `LPARAM` are **64-bit pointers / integers**. Omitting `restype` truncates high memory addresses above `0x7FFFFFFF`, yielding intermittent `ERROR_INVALID_HANDLE (6)` or fatal access violations (`0xC0000005`).
- **Rule:** ALWAYS declare explicit `argtypes` and `restype` using `ctypes.wintypes` (`wintypes.HANDLE`, `wintypes.HWND`, `wintypes.WPARAM`, `wintypes.LPARAM`, `ctypes.c_ssize_t` for `LRESULT`) before calling any Win32 API.

```python
import ctypes
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# Correct 64-bit handle and pointer declarations:
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetForegroundWindow.argtypes = []

kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
```

---

## 2. C Callback Garbage-Collection Guard (`WINFUNCTYPE`)

- **Fatal Bug:** Passing `WINEVENTPROC(callback_fn)` or `HOOKPROC(callback_fn)` inline to `SetWinEventHook` or `SetWindowsHookExW` creates a temporary Python wrapper object. Once the registration line finishes, the Python garbage collector reclaims the object. The next time the OS invokes the hook pointer, it jumps into deallocated memory, crashing Python immediately with `0xC0000005`.
- **Rule:** Store the callback delegate as an attribute (`self._proc_delegate = HOOKPROC(self._callback)`) on the managing instance for the entire lifetime of the hook until after `UnhookWinEvent` or `UnhookWindowsHookEx` completes.

```python
class WinEventHookWatcher:
    def __init__(self, callback):
        self._user_callback = callback
        # Define and retain the WINFUNCTYPE delegate on self
        WINEVENTPROC = ctypes.WINFUNCTYPE(
            None, wintypes.HANDLE, wintypes.DWORD, wintypes.HWND,
            wintypes.LONG, wintypes.LONG, wintypes.DWORD, wintypes.DWORD
        )
        self._delegate = WINEVENTPROC(self._on_event)
        self._hook_handle = None

    def start(self):
        self._hook_handle = user32.SetWinEventHook(
            0x0003, 0x0003, None, self._delegate, 0, 0, 0x0000 | 0x0002
        )

    def stop(self):
        if self._hook_handle:
            user32.UnhookWinEvent(self._hook_handle)
            self._hook_handle = None
```

---

## 3. Low-Level Hook Latency & Deadlock Law (`WH_KEYBOARD_LL` / `GetMessageW`)

- **Rule 1:** Windows enforces a strict timeout (often ~200ms via `LowLevelHooksTimeout` registry) on `WH_KEYBOARD_LL` callbacks. Never acquire a blocking `threading.Lock()` or execute disk/database I/O inside a hook callback. Offload events to an in-memory `queue.Queue`.
- **Rule 2:** To cleanly terminate a background thread blocked on a Win32 message loop (`GetMessageW`), record the thread's ID at startup via `kernel32.GetCurrentThreadId()`. In your `stop()` method, post `WM_QUIT` using `user32.PostThreadMessageW(thread_id, 0x0012, 0, 0)`.

---

## 4. COM Apartment Isolation (`RPC_E_CHANGED_MODE = 0x80010106`)

- **Fatal Bug:** Mixing `COINIT_MULTITHREADED (0x0)` and `COINIT_APARTMENTTHREADED (0x2)` on the same worker thread causes `RPC_E_CHANGED_MODE (0x80010106)`.
- **Rule:** UIA (`pywinauto`) and Shell APIs must run on dedicated STA worker threads initialized explicitly with `ole32.CoInitializeEx(None, 0x2)`. Background compute threads can use MTA or rely on default apartment initialization.

---

## 5. Kernel & GDI Handle RAII Discipline

- **Rule:** Windows limits each process to 10,000 GDI handles. Every `GetDC`, `CreateCompatibleDC`, `CreateCompatibleBitmap`, `OpenProcess`, or `CreateJobObjectW` MUST be paired with `ReleaseDC`, `DeleteDC`, `DeleteObject`, or `CloseHandle` inside a `try...finally` block.
