import ctypes
from ctypes import wintypes

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

TOKEN_QUERY = 0x0008
TokenElevation = 20
TokenIntegrityLevel = 25
TokenIsAppContainer = 29
TokenAppContainerSid = 31

advapi32.OpenProcessToken.restype = wintypes.BOOL
advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]

advapi32.GetTokenInformation.restype = wintypes.BOOL
advapi32.GetTokenInformation.argtypes = [
    wintypes.HANDLE,
    ctypes.c_int,
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD)
]

advapi32.GetSidSubAuthorityCount.restype = ctypes.POINTER(wintypes.BYTE)
advapi32.GetSidSubAuthorityCount.argtypes = [ctypes.c_void_p]

advapi32.GetSidSubAuthority.restype = ctypes.POINTER(wintypes.DWORD)
advapi32.GetSidSubAuthority.argtypes = [ctypes.c_void_p, wintypes.DWORD]

def inspect_token():
    h_proc = kernel32.GetCurrentProcess()
    h_token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(h_proc, TOKEN_QUERY, ctypes.byref(h_token)):
        print("OpenProcessToken failed:", ctypes.get_last_error())
        return

    # IsAppContainer
    is_ac = wintypes.DWORD(0)
    ret_len = wintypes.DWORD(0)
    if advapi32.GetTokenInformation(h_token, TokenIsAppContainer, ctypes.byref(is_ac), ctypes.sizeof(is_ac), ctypes.byref(ret_len)):
        print("TokenIsAppContainer:", is_ac.value)
    else:
        print("GetTokenInformation(TokenIsAppContainer) failed:", ctypes.get_last_error())

    # Elevation
    is_elev = wintypes.DWORD(0)
    if advapi32.GetTokenInformation(h_token, TokenElevation, ctypes.byref(is_elev), ctypes.sizeof(is_elev), ctypes.byref(ret_len)):
        print("TokenIsElevated:", is_elev.value)

    # AppContainer SID
    if is_ac.value:
        ac_buf = ctypes.create_string_buffer(128)
        if advapi32.GetTokenInformation(h_token, TokenAppContainerSid, ac_buf, 128, ctypes.byref(ret_len)):
            p_ac_sid = ctypes.cast(ac_buf, ctypes.POINTER(ctypes.c_void_p))[0]
            str_sid_w = wintypes.LPWSTR()
            advapi32.ConvertSidToStringSidW(p_ac_sid, ctypes.byref(str_sid_w))
            print("Current Token AppContainer SID:", str_sid_w.value)
            kernel32.LocalFree(str_sid_w)
    buf = ctypes.create_string_buffer(64)
    if advapi32.GetTokenInformation(h_token, TokenIntegrityLevel, buf, 64, ctypes.byref(ret_len)):
        # Structure is TOKEN_MANDATORY_LABEL: SID_AND_ATTRIBUTES Label; Label.Sid is at offset 0
        p_sid = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
        cnt_ptr = advapi32.GetSidSubAuthorityCount(p_sid)
        if cnt_ptr:
            cnt = cnt_ptr[0]
            rid_ptr = advapi32.GetSidSubAuthority(p_sid, cnt - 1)
            if rid_ptr:
                print("Integrity RID:", hex(rid_ptr[0]))

    kernel32.CloseHandle(h_token)

if __name__ == "__main__":
    inspect_token()
