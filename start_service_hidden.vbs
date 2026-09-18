Option Explicit
Dim shell, fso, projectDir, command
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
projectDir = fso.GetParentFolderName(WScript.ScriptFullName)
command = "cmd.exe /c """ & projectDir & "\scripts\start_service.cmd"""
shell.Run command, 0, False
