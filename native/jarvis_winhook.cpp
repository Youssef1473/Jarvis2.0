// jarvis_winhook.cpp - Native WinEvent Hook & Shared Memory SPSC Ring Buffer for Jarvis 2.0
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <cstdint>
#include <atomic>
#include <thread>
#include <chrono>
#include <cstring>

#pragma pack(push, 1)
struct WinEventFrame {
    uint64_t qpc_timestamp;
    uint64_t hwnd;
    uint32_t pid;
    uint32_t event_id;
    int32_t  obj_id;
    uint32_t privacy_flags;
    char     utf8_title[32];
};

struct RingBufferHeader {
    std::atomic<uint64_t> write_idx;
    std::atomic<uint64_t> read_idx;
    std::atomic<uint32_t> privacy_lock;
    uint32_t capacity; // 1024
    uint8_t  padding[40];
};
#pragma pack(pop)

static_assert(sizeof(WinEventFrame) == 64, "WinEventFrame must be 64 bytes (1 cache line)");
static_assert(sizeof(RingBufferHeader) == 64, "RingBufferHeader must be 64 bytes (1 cache line)");

constexpr const wchar_t* SHM_NAME = L"Local\\JarvisWinEventRing";
constexpr uint32_t RING_CAPACITY = 1024;
constexpr size_t TOTAL_SHM_SIZE = sizeof(RingBufferHeader) + (RING_CAPACITY * sizeof(WinEventFrame));

static HANDLE g_hMapFile = nullptr;
static uint8_t* g_pBuf = nullptr;
static RingBufferHeader* g_pHeader = nullptr;
static WinEventFrame* g_pFrames = nullptr;
static HWINEVENTHOOK g_hHook = nullptr;
static std::thread g_hookThread;
static std::atomic<bool> g_running{false};

static bool IsSensitiveWindow(const wchar_t* title, const wchar_t* procName) {
    if (!title && !procName) return false;
    const wchar_t* sensitiveKeywords[] = {
        L"1password", L"bitwarden", L"keepass", L"lastpass", L"dashlane",
        L"authenticator", L"chase online", L"bank of america", L"wells fargo",
        L"metamask", L"ledger live", L"trezor", nullptr
    };
    for (int i = 0; sensitiveKeywords[i] != nullptr; ++i) {
        if (title && wcsstr(title, sensitiveKeywords[i]) != nullptr) return true;
        if (procName && wcsstr(procName, sensitiveKeywords[i]) != nullptr) return true;
    }
    return false;
}

static void PushEventFrame(HWINEVENTHOOK hHook, DWORD event, HWND hwnd, LONG idObject, LONG idChild, DWORD dwEventThread, DWORD dwmsEventTime) {
    if (!g_pHeader || !g_pFrames) return;

    LARGE_INTEGER qpc;
    QueryPerformanceCounter(&qpc);

    DWORD pid = 0;
    GetWindowThreadProcessId(hwnd, &pid);

    wchar_t titleBuf[64] = {0};
    GetWindowTextW(hwnd, titleBuf, 63);

    // Fast Privacy Guard check (<10us)
    uint32_t privacy = 0;
    if (IsSensitiveWindow(titleBuf, nullptr)) {
        privacy = 1;
        g_pHeader->privacy_lock.store(1, std::memory_order_release);
    }

    uint64_t w_idx = g_pHeader->write_idx.load(std::memory_order_relaxed);
    uint64_t r_idx = g_pHeader->read_idx.load(std::memory_order_acquire);
    if (w_idx - r_idx >= RING_CAPACITY) {
        // Drop frames safely if queue exceeds 1,024 events without allocating memory on callback thread
        return;
    }
    uint32_t slot = static_cast<uint32_t>(w_idx % RING_CAPACITY);

    WinEventFrame& frame = g_pFrames[slot];
    frame.qpc_timestamp = static_cast<uint64_t>(qpc.QuadPart);
    frame.hwnd = reinterpret_cast<uint64_t>(hwnd);
    frame.pid = pid;
    frame.event_id = event;
    frame.obj_id = idObject;
    frame.privacy_flags = privacy;

    // Convert wide title to UTF-8
    WideCharToMultiByte(CP_UTF8, 0, titleBuf, -1, frame.utf8_title, sizeof(frame.utf8_title) - 1, nullptr, nullptr);
    frame.utf8_title[sizeof(frame.utf8_title) - 1] = '\0';

    g_pHeader->write_idx.store(w_idx + 1, std::memory_order_release);
}

static void CALLBACK WinEventProc(HWINEVENTHOOK hHook, DWORD event, HWND hwnd, LONG idObject, LONG idChild, DWORD dwEventThread, DWORD dwmsEventTime) {
    if (event == EVENT_SYSTEM_FOREGROUND || event == EVENT_OBJECT_FOCUS || event == EVENT_OBJECT_VALUECHANGE) {
        PushEventFrame(hHook, event, hwnd, idObject, idChild, dwEventThread, dwmsEventTime);
    }
}

static void HookMessageLoop() {
    g_hHook = SetWinEventHook(
        EVENT_SYSTEM_FOREGROUND,
        EVENT_OBJECT_VALUECHANGE,
        nullptr,
        WinEventProc,
        0, 0,
        WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS
    );

    MSG msg;
    while (g_running.load(std::memory_order_acquire)) {
        while (PeekMessageW(&msg, nullptr, 0, 0, PM_REMOVE)) {
            TranslateMessage(&msg);
            DispatchMessageW(&msg);
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(5));
    }

    if (g_hHook) {
        UnhookWinEvent(g_hHook);
        g_hHook = nullptr;
    }
}

extern "C" {
    __declspec(dllexport) bool StartWinEventHookSidecar() {
        if (g_running.load()) return true;

        g_hMapFile = CreateFileMappingW(
            INVALID_HANDLE_VALUE, nullptr, PAGE_READWRITE,
            0, static_cast<DWORD>(TOTAL_SHM_SIZE), SHM_NAME
        );
        if (!g_hMapFile) return false;

        g_pBuf = static_cast<uint8_t*>(MapViewOfFile(g_hMapFile, FILE_MAP_ALL_ACCESS, 0, 0, TOTAL_SHM_SIZE));
        if (!g_pBuf) {
            CloseHandle(g_hMapFile);
            g_hMapFile = nullptr;
            return false;
        }

        g_pHeader = reinterpret_cast<RingBufferHeader*>(g_pBuf);
        g_pFrames = reinterpret_cast<WinEventFrame*>(g_pBuf + sizeof(RingBufferHeader));

        g_pHeader->write_idx.store(0);
        g_pHeader->read_idx.store(0);
        g_pHeader->privacy_lock.store(0);
        g_pHeader->capacity = RING_CAPACITY;

        g_running.store(true);
        g_hookThread = std::thread(HookMessageLoop);
        return true;
    }

    __declspec(dllexport) void StopWinEventHookSidecar() {
        if (!g_running.load()) return;
        g_running.store(false);
        if (g_hookThread.joinable()) g_hookThread.join();

        if (g_pBuf) {
            UnmapViewOfFile(g_pBuf);
            g_pBuf = nullptr;
        }
        if (g_hMapFile) {
            CloseHandle(g_hMapFile);
            g_hMapFile = nullptr;
        }
    }
}
