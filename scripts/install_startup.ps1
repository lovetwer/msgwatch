$ws = New-Object -ComObject WScript.Shell
$startup = [Environment]::GetFolderPath('Startup')
$lnk = $ws.CreateShortcut("$startup\MsgWatch.lnk")
$lnk.TargetPath = 'C:\Windows\System32\wscript.exe'
$lnk.Arguments = '"C:\Users\10292\.zcode\workspace\default\msgwatch\watch_hidden.vbs"'
$lnk.WorkingDirectory = 'C:\Users\10292\.zcode\workspace\default\msgwatch'
$lnk.Save()
Write-Output "LNK_CREATED: $startup\MsgWatch.lnk"
