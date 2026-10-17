' ==============================================================================
' OmniRoute AI Gateway - Silent Background Launcher
' Runs OmniRoute detached in background with zero window flashing.
' ==============================================================================

Set WshShell = CreateObject("WScript.Shell")
strCommand = "cmd.exe /c omniroute serve --no-open --no-tray"
WshShell.Run strCommand, 0, False
