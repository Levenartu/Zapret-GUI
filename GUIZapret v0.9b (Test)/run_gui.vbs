Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
appDir = fso.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = appDir

pythonw = ""
Set exec = shell.Exec("py -3 -c ""import os, sys; print(os.path.join(os.path.dirname(sys.executable), 'pythonw.exe'))""")
Do While exec.Status = 0
    WScript.Sleep 50
Loop
pythonw = Trim(exec.StdOut.ReadAll())

If pythonw = "" Or Not fso.FileExists(pythonw) Then
    pythonw = "pythonw"
End If

shell.Run """" & pythonw & """ """ & appDir & "\gui.py""", 0, False
