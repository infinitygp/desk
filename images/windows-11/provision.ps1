# Runs over SSH (as tester, elevated) before the warm state is saved; the reboot that follows
# (relogin in image.toml) starts the in-session agent again. Keep it idempotent.
$run = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
# conhost --headless: Windows Terminal, the default console host, ignores -WindowStyle Hidden.
Set-ItemProperty $run -Name desk-agent -Value 'conhost.exe --headless powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\desk\agent.ps1'

# English (US) with the Polish (Programmers) layout. An earlier image listed Polish as a language
# of its own, which made Store apps (Notepad) show Polish while Windows itself is English.
$langs = New-WinUserLanguageList en-US
$langs[0].InputMethodTips.Clear()
$langs[0].InputMethodTips.Add('0409:00000415')
Set-WinUserLanguageList $langs -Force

# The Store updating Calculator or Notepad mid-test makes them fail to start.
$store = 'HKLM:\SOFTWARE\Policies\Microsoft\WindowsStore'
if (-not (Test-Path $store)) { New-Item $store -Force | Out-Null }
New-ItemProperty $store -Name AutoDownload -Value 2 -PropertyType DWord -Force | Out-Null
