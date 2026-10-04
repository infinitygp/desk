# First logon of the Windows golden image, started by autounattend.xml from the answer-file CD.
# Sets up everything the harness needs, then powers off; `desk image build` waits for that.
$ErrorActionPreference = 'Continue'
$cd = Split-Path -Parent $MyInvocation.MyCommand.Path
New-Item -ItemType Directory -Force C:\desk | Out-Null
Start-Transcript -Path C:\desk\firstboot.log -Append

function Reg($path, $name, $value, $type = 'DWord') {
    if (-not (Test-Path $path)) { New-Item -Path $path -Force | Out-Null }
    New-ItemProperty -Path $path -Name $name -Value $value -PropertyType $type -Force | Out-Null
}

# virtio-win guest tools: virtio-gpu display driver, virtio-serial and qemu-guest-agent.
Start-Process -Wait "$cd\virtio-win-guest-tools.exe" -ArgumentList '/install', '/quiet', '/norestart'

# OpenSSH server from the MSI on this CD (no Windows Update needed), PowerShell as its shell and
# the harness key for the administrator account.
Start-Process -Wait msiexec.exe -ArgumentList '/i', "`"$cd\openssh.msi`"", '/qn', '/norestart'
Set-Service sshd -StartupType Automatic
Reg 'HKLM:\SOFTWARE\OpenSSH' DefaultShell "$env:WINDIR\System32\WindowsPowerShell\v1.0\powershell.exe" String
Copy-Item "$cd\authorized_keys" C:\ProgramData\ssh\administrators_authorized_keys -Force
icacls.exe C:\ProgramData\ssh\administrators_authorized_keys /inheritance:r /grant '*S-1-5-32-544:F' /grant 'SYSTEM:F' | Out-Null
New-NetFirewallRule -Name desk-ssh -DisplayName 'desk SSH' -Direction Inbound -Protocol TCP -LocalPort 22 -Action Allow -Profile Any | Out-Null
New-NetFirewallRule -Name desk-agent -DisplayName 'desk agent' -Direction Inbound -Protocol TCP -LocalPort 47011 -Action Allow -Profile Any | Out-Null
Start-Service sshd

# The in-session agent (UI Automation, starting programs on the desktop) at every logon.
Copy-Item "$cd\agent.ps1", "$cd\uia.ps1" C:\desk -Force
# Files from a CD keep the read-only attribute; provisioning replaces them later.
Get-ChildItem C:\desk -File | ForEach-Object { $_.IsReadOnly = $false }
# conhost --headless: Windows Terminal, the default console host, ignores -WindowStyle Hidden.
Reg 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' desk-agent `
    'conhost.exe --headless powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\desk\agent.ps1' String

# A stable screen for screenshots: no sleep, lock, screen saver, blinking cursor or animations.
powercfg /change monitor-timeout-ac 0
powercfg /change standby-timeout-ac 0
powercfg /hibernate off
Reg 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\Personalization' NoLockScreen 1
Reg 'HKCU:\Control Panel\Desktop' ScreenSaveActive '0' String
Reg 'HKCU:\Control Panel\Desktop' CursorBlinkRate '-1' String
Reg 'HKCU:\Control Panel\Desktop\WindowMetrics' MinAnimate '0' String
Reg 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\VisualEffects' VisualFXSetting 2

# Nothing popping up over the app under test: notifications, tips, first-run pages, nags.
Reg 'HKCU:\Software\Microsoft\Windows\CurrentVersion\PushNotifications' ToastEnabled 0
Reg 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\CloudContent' DisableWindowsConsumerFeatures 1
Reg 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\CloudContent' DisableSoftLanding 1
Reg 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\OOBE' DisablePrivacyExperience 1
Reg 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\OneDrive' DisableFileSyncNGSC 1
Reg 'HKLM:\SOFTWARE\Policies\Microsoft\Edge' HideFirstRunExperience 1
Reg 'HKCU:\Software\Microsoft\Windows\CurrentVersion\UserProfileEngagement' ScoobeSystemSettingEnabled 0
foreach ($id in '310093', '338388', '338389', '338393', '353694', '353696') {
    Reg 'HKCU:\Software\Microsoft\Windows\CurrentVersion\ContentDeliveryManager' "SubscribedContent-$($id)Enabled" 0
}

# No automatic updates or restarts during tests, and no Store app updates (an app being updated
# fails to start).
Reg 'HKLM:\SOFTWARE\Policies\Microsoft\WindowsStore' AutoDownload 2
Reg 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU' NoAutoUpdate 1
Reg 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU' NoAutoRebootWithLoggedOnUsers 1
Reg 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate' ExcludeWUDriversInQualityUpdate 1

Get-Service sshd, QEMU-GA -ErrorAction SilentlyContinue | Format-Table -AutoSize
Stop-Transcript
Stop-Computer -Force
