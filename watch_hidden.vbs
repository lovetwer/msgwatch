' msgwatch 隐藏启动脚本：计划任务/开机自启用。双击亦可。
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = fso.GetParentFolderName(WScript.ScriptFullName)
' 0 = 隐藏窗口, False = 不等待
sh.Run """.venv\Scripts\pythonw.exe"" -m msgwatch run", 0, False
