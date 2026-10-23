' Firefly one-click launcher (silent, no console window).
'
' Calls launch.py with pythonw; launch.py starts TTS / STT / pet and
' redirects their output to logs\*.log.
'
' Keep this file ASCII-only and CRLF. Windows Script Host reads .vbs with
' the system ANSI code page, so UTF-8 Chinese comments here will break it.

Option Explicit

Dim sh, fso, root, py, cmd, q

Set sh  = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

root = fso.GetParentFolderName(WScript.ScriptFullName)
py   = root & "\.venv\Scripts\pythonw.exe"

If Not fso.FileExists(py) Then
    MsgBox "pythonw.exe not found:" & vbCrLf & py, 16, "Firefly"
    WScript.Quit 1
End If

q = Chr(34)
cmd = q & py & q & " " & q & root & "\launch.py" & q

sh.CurrentDirectory = root
sh.Run cmd, 0, False