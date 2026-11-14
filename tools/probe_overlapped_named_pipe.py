"""
Empirical probe for Design B: Win32 Named Pipes with Overlapped I/O.
Tests true kernel-level asynchronous I/O and CancelIoEx cancellation.
"""

import ctypes
from ctypes import wintypes
import os
import subprocess
import sys
import threading
import time
import uuid

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# Win32 Constants
PIPE_ACCESS_INBOUND = 0x00000001
FILE_FLAG_OVERLAPPED = 0x40000000
PIPE_TYPE_BYTE = 0x00000000
PIPE_READMODE_BYTE = 0x00000000
PIPE_WAIT = 0x00000000

GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3
FILE_ATTRIBUTE_NORMAL = 0x00000080
INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value

ERROR_IO_PENDING = 997
ERROR_BROKEN_PIPE = 109
ERROR_PIPE_CONNECTED = 535
ERROR_OPERATION_ABORTED = 995

WAIT_OBJECT_0 = 0x00000000
WAIT_TIMEOUT = 0x00000102

class SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("nLength", wintypes.DWORD),
        ("lpSecurityDescriptor", wintypes.LPVOID),
        ("bInheritHandle", wintypes.BOOL),
    ]

class OVERLAPPED(ctypes.Structure):
    _fields_ = [
        ("Internal", ctypes.c_ulonglong),
        ("InternalHigh", ctypes.c_ulonglong),
        ("Offset", wintypes.DWORD),
        ("OffsetHigh", wintypes.DWORD),
        ("hEvent", wintypes.HANDLE),
    ]

# Setup function signatures
kernel32.CreateNamedPipeW.restype = wintypes.HANDLE
kernel32.CreateNamedPipeW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
    wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(SECURITY_ATTRIBUTES)
]

kernel32.CreateFileW.restype = wintypes.HANDLE
kernel32.CreateFileW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(SECURITY_ATTRIBUTES),
    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE
]

kernel32.ConnectNamedPipe.restype = wintypes.BOOL
kernel32.ConnectNamedPipe.argtypes = [wintypes.HANDLE, ctypes.POINTER(OVERLAPPED)]

kernel32.ReadFile.restype = wintypes.BOOL
kernel32.ReadFile.argtypes = [
    wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(OVERLAPPED)
]

kernel32.GetOverlappedResult.restype = wintypes.BOOL
kernel32.GetOverlappedResult.argtypes = [
    wintypes.HANDLE, ctypes.POINTER(OVERLAPPED),
    ctypes.POINTER(wintypes.DWORD), wintypes.BOOL
]

kernel32.CancelIoEx.restype = wintypes.BOOL
kernel32.CancelIoEx.argtypes = [wintypes.HANDLE, ctypes.POINTER(OVERLAPPED)]

kernel32.CreateEventW.restype = wintypes.HANDLE
kernel32.CreateEventW.argtypes = [ctypes.POINTER(SECURITY_ATTRIBUTES), wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]

kernel32.SetEvent.restype = wintypes.BOOL
kernel32.SetEvent.argtypes = [wintypes.HANDLE]

kernel32.ResetEvent.restype = wintypes.BOOL
kernel32.ResetEvent.argtypes = [wintypes.HANDLE]

kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

kernel32.WaitForMultipleObjects.restype = wintypes.DWORD
kernel32.WaitForMultipleObjects.argtypes = [
    wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE), wintypes.BOOL, wintypes.DWORD
]


def create_overlapped_pipe_pair() -> tuple[wintypes.HANDLE, wintypes.HANDLE, str]:
    pipe_name = f"\\\\.\\pipe\\jarvis_test_pipe_{uuid.uuid4().hex}"
    
    # Server end (Inbound, Overlapped, Non-inheritable)
    h_server = kernel32.CreateNamedPipeW(
        pipe_name,
        PIPE_ACCESS_INBOUND | FILE_FLAG_OVERLAPPED,
        PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT,
        1,       # 1 instance
        65536,   # out buf
        65536,   # in buf
        0,       # default timeout
        None     # non-inheritable
    )
    if h_server == INVALID_HANDLE_VALUE:
        raise OSError(f"CreateNamedPipeW failed: {ctypes.get_last_error()}")

    # Client end (Write, Synchronous, Inheritable)
    sa = SECURITY_ATTRIBUTES()
    sa.nLength = ctypes.sizeof(SECURITY_ATTRIBUTES)
    sa.lpSecurityDescriptor = None
    sa.bInheritHandle = True

    h_client = kernel32.CreateFileW(
        pipe_name,
        GENERIC_WRITE,
        0,
        ctypes.byref(sa),
        OPEN_EXISTING,
        FILE_ATTRIBUTE_NORMAL,
        None
    )
    if h_client == INVALID_HANDLE_VALUE:
        err = ctypes.get_last_error()
        kernel32.CloseHandle(h_server)
        raise OSError(f"CreateFileW failed: {err}")

    # Connect server end
    conn = kernel32.ConnectNamedPipe(h_server, None)
    if not conn:
        err = ctypes.get_last_error()
        if err != ERROR_PIPE_CONNECTED:
            kernel32.CloseHandle(h_client)
            kernel32.CloseHandle(h_server)
            raise OSError(f"ConnectNamedPipe failed: {err}")

    return h_server, h_client, pipe_name


def test_overlapped_cancellation():
    print("Testing Overlapped Named Pipe Cancellation via CancelIoEx...")
    h_server, h_client, name = create_overlapped_pipe_pair()

    h_stop_event = kernel32.CreateEventW(None, True, False, None)
    chunks = []
    thread_done = threading.Event()
    diagnostics = []

    def reader_thread():
        ov = OVERLAPPED()
        ov.hEvent = kernel32.CreateEventW(None, True, False, None)
        buf = ctypes.create_string_buffer(4096)
        bytes_transferred = wintypes.DWORD(0)

        handle_arr = (wintypes.HANDLE * 2)(ov.hEvent, h_stop_event)

        while True:
            kernel32.ResetEvent(ov.hEvent)
            success = kernel32.ReadFile(
                h_server, buf, 4096, ctypes.byref(bytes_transferred), ctypes.byref(ov)
            )
            if not success:
                err = ctypes.get_last_error()
                if err == ERROR_IO_PENDING:
                    # Asynchronous wait on I/O completion or stop signal
                    wait_res = kernel32.WaitForMultipleObjects(2, handle_arr, False, 50)
                    if wait_res == WAIT_OBJECT_0:
                        # I/O completed
                        res = kernel32.GetOverlappedResult(
                            h_server, ctypes.byref(ov), ctypes.byref(bytes_transferred), False
                        )
                        if res and bytes_transferred.value > 0:
                            chunks.append(buf.raw[:bytes_transferred.value].decode("utf-8", "replace"))
                    elif wait_res == WAIT_OBJECT_0 + 1:
                        # Stop signaled! Cancel outstanding I/O!
                        diagnostics.append("Stop event signaled; issuing CancelIoEx")
                        cancel_res = kernel32.CancelIoEx(h_server, ctypes.byref(ov))
                        # Wait for I/O manager to abort
                        kernel32.GetOverlappedResult(
                            h_server, ctypes.byref(ov), ctypes.byref(bytes_transferred), True
                        )
                        abort_err = ctypes.get_last_error()
                        diagnostics.append(f"CancelIoEx returned {bool(cancel_res)}, aborted err: {abort_err}")
                        break
                    elif wait_res == WAIT_TIMEOUT:
                        continue
                elif err == ERROR_BROKEN_PIPE:
                    diagnostics.append("Normal EOF (broken pipe)")
                    break
                else:
                    diagnostics.append(f"Unexpected ReadFile error: {err}")
                    break
            else:
                # Synchronous completion
                if bytes_transferred.value > 0:
                    chunks.append(buf.raw[:bytes_transferred.value].decode("utf-8", "replace"))

        kernel32.CloseHandle(ov.hEvent)
        thread_done.set()

    t = threading.Thread(target=reader_thread)
    t.start()

    # Client writes a message
    msg = b"Hello Overlapped Pipe!\n"
    written = wintypes.DWORD(0)
    kernel32.WriteFile(h_client, msg, len(msg), ctypes.byref(written), None)
    time.sleep(0.1)

    # Note: h_client remains OPEN (held writer scenario!)
    print(f"Read chunks so far: {chunks}")
    assert "Hello Overlapped Pipe!\n" in chunks

    # Now signal stop while h_client is STILL OPEN and ReadFile is blocked waiting for data!
    print("Signaling stop event while pipe writer is held open...")
    t0 = time.perf_counter()
    kernel32.SetEvent(h_stop_event)
    t.join(timeout=0.5)
    dur_ms = (time.perf_counter() - t0) * 1000.0

    print(f"Reader thread joined in: {dur_ms:.2f} ms")
    print(f"Thread alive: {t.is_alive()}")
    print(f"Diagnostics: {diagnostics}")

    # Clean up handles
    kernel32.CloseHandle(h_client)
    kernel32.CloseHandle(h_server)
    kernel32.CloseHandle(h_stop_event)

    assert not t.is_alive()
    assert dur_ms < 500.0
    print("SUCCESS: Overlapped Named Pipe CancelIoEx verified!")

if __name__ == "__main__":
    test_overlapped_cancellation()

