' ==============================================================================
' JARVIS 2.0 - Invisible Background Launcher (Stealth Mode)
' Runs Jarvis via pythonw.exe with zero command prompt window flashing.
' ==============================================================================

Set objFSO = CreateObject("Scripting.FileSystemObject")
strScriptDir = objFSO.GetParentFolderName(WScript.ScriptFullName)
strProjectDir = objFSO.GetParentFolderName(strScriptDir)

Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = strProjectDir

strPythonw = strProjectDir & "\.venv\Scripts\pythonw.exe"
If Not objFSO.FileExists(strPythonw) Then
    strPythonw = "pythonw.exe"
End If

strCommand = """" & strPythonw & """ run.py --stealth"
WshShell.Run strCommand, 0, False
