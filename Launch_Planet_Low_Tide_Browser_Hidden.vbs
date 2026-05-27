Option Explicit

Dim shell, fso, appDir, logDir, logPath, pythonwPath, command

Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

appDir = fso.GetParentFolderName(WScript.ScriptFullName)
logDir = appDir & "\logs"
logPath = logDir & "\hidden_launcher.log"
pythonwPath = "L:\RES_Library\Conda_env\myenv\pythonw.exe"

If Not fso.FolderExists(logDir) Then
  fso.CreateFolder(logDir)
End If

If Not fso.FileExists(pythonwPath) Then
  MsgBox "Shared conda pythonw.exe was not found:" & vbCrLf & pythonwPath & vbCrLf & vbCrLf & _
         "Run Launch_Planet_Low_Tide_Browser.bat for setup and diagnostics.", _
         vbExclamation, "Planet Low Tide Browser"
  WScript.Quit 1
End If

If Not fso.FileExists(appDir & "\tide\CSIRO_tidal_const_v12.nc") Then
  MsgBox "CSIRO tide model file is missing:" & vbCrLf & _
         appDir & "\tide\CSIRO_tidal_const_v12.nc" & vbCrLf & vbCrLf & _
         "Download it from https://data.csiro.au/collection/csiro:45584", _
         vbExclamation, "Planet Low Tide Browser"
  WScript.Quit 1
End If

command = "cmd /c cd /d " & Chr(34) & appDir & Chr(34) & _
          " && " & Chr(34) & pythonwPath & Chr(34) & " app\web_app.py" & _
          " >> " & Chr(34) & logPath & Chr(34) & " 2>&1"

shell.Run command, 0, False
