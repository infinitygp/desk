# Runs over SSH (as tester, elevated) before the warm state is saved; the reboot that follows
# (relogin in image.toml) starts the in-session agent again. Keep it idempotent.
# Any error ends it: a setting that could not be made must not end up in a warm state that looks
# complete.
$ErrorActionPreference = 'Stop'
$run = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
# conhost --headless: Windows Terminal, the default console host, ignores -WindowStyle Hidden.
# agent-loop.ps1 starts the agent again when it ends.
Set-ItemProperty $run -Name desk-agent -Value 'conhost.exe --headless powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\desk\agent-loop.ps1'

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

# The same desktop every time: Windows Spotlight changes the wallpaper daily and adds a "Learn
# about this picture" icon, which would differ between screenshots and between icon layouts.
$cloud = 'HKCU:\Software\Policies\Microsoft\Windows\CloudContent'
if (-not (Test-Path $cloud)) { New-Item $cloud -Force | Out-Null }
New-ItemProperty $cloud -Name DisableSpotlightCollectionOnDesktop -Value 1 -PropertyType DWord -Force | Out-Null
Set-ItemProperty 'HKCU:\Control Panel\Desktop' -Name WallPaper -Value 'C:\Windows\Web\Wallpaper\Windows\img0.jpg'

# Windows features Microsoft rolls out gradually (Controlled Feature Rollout), so a fresh install
# may lack what users already have. Opt in the official way ("Get the latest updates as soon as
# they're available"), and turn on the ones tests need with ViVeTool, pinned by hash. The skill
# lists them; they take effect with the reboot after provisioning.
$wu = 'HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings'
if (-not (Test-Path $wu)) { New-Item $wu -Force | Out-Null }
New-ItemProperty $wu -Name IsContinuousInnovationOptedIn -Value 1 -PropertyType DWord -Force | Out-Null
$vive = 'C:\desk\vivetool'
$zip = 'C:\desk\vivetool.zip'
$sha = 'CC27F073F3FE5DD2C3D947FAF558FD4B2F8E34454F812689B0D65EE8A52E4147'
if (-not (Test-Path "$vive\ViVeTool.exe")) {
    Invoke-WebRequest -UseBasicParsing -OutFile $zip 'https://github.com/thebookisclosed/ViVe/releases/download/v0.3.4/ViVeTool-v0.3.4-IntelAmd.zip'
    if ((Get-FileHash $zip -Algorithm SHA256).Hash -ne $sha) { throw 'ViVeTool download does not match its pinned SHA-256' }
    Expand-Archive -Force $zip $vive
    Remove-Item $zip
}
# 59213768: taskbar position (bottom, top, left, right); 61090762: taskbar size (default, small).
& "$vive\ViVeTool.exe" /enable /id:59213768,61090762 | Out-Null
if ($LASTEXITCODE) { throw "ViVeTool failed ($LASTEXITCODE)" }
