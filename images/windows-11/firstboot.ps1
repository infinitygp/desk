# First logon of the Windows golden image, started by autounattend.xml from the answer-file CD.
# Sets up everything the harness needs, reports the outcome on the serial console and powers off;
# `desk image build` waits for that and keeps the install only when the report says "ok".
$ErrorActionPreference = 'Continue'
$cd = Split-Path -Parent $MyInvocation.MyCommand.Path
New-Item -ItemType Directory -Force C:\desk | Out-Null
Start-Transcript -Path C:\desk\firstboot.log -Append

function Reg($path, $name, $value, $type = 'DWord') {
    if (-not (Test-Path $path)) { New-Item -Path $path -Force | Out-Null }
    New-ItemProperty -Path $path -Name $name -Value $value -PropertyType $type -Force | Out-Null
}

$problems = @()
function Installed($what, $process) {
    # 3010: installed, a restart is needed (the build ends with one anyway).
    if ($process.ExitCode -notin 0, 3010) { $script:problems += "$what exited $($process.ExitCode)" }
}

# virtio-win guest tools: virtio-gpu display driver, virtio-serial and qemu-guest-agent.
Installed 'the virtio-win guest tools' (Start-Process -Wait -PassThru "$cd\virtio-win-guest-tools.exe" `
    -ArgumentList '/install', '/quiet', '/norestart')

# OpenSSH server from the MSI on this CD (no Windows Update needed), PowerShell as its shell and
# the harness key for the administrator account.
Installed 'the OpenSSH MSI' (Start-Process -Wait -PassThru msiexec.exe `
    -ArgumentList '/i', "`"$cd\openssh.msi`"", '/qn', '/norestart')
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
# -MTA: UI Automation clients belong in the multithreaded apartment; in PowerShell's default
# STA the MSAA proxies of classic Win32 windows (installers) return no children.
Reg 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' desk-agent `
    'conhost.exe --headless powershell.exe -MTA -NoProfile -ExecutionPolicy Bypass -File C:\desk\agent.ps1' String

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

# WebView2, which apps under test run on, is brought up to date by Microsoft's bootstrapper, which
# waits for its install (the updater left it at the ISO's version whenever Edge had just checked);
# Edge is as far as the updater got since logon. Then both stay at that version: their updater would
# go on with an update inside every run (CPU, about 1 GB of disk) and could replace WebView2 under an
# app being tested. The bootstrapper is always the latest, so its signature is checked, not a hash.
$wv2 = "$env:TEMP\MicrosoftEdgeWebview2Setup.exe"
try {
    Invoke-WebRequest -UseBasicParsing -OutFile $wv2 'https://go.microsoft.com/fwlink/p/?LinkId=2124703'
    $sig = Get-AuthenticodeSignature $wv2
    if ($sig.Status -ne 'Valid' -or $sig.SignerCertificate.Subject -notlike 'CN=Microsoft Corporation,*') {
        $problems += 'the WebView2 bootstrapper is not signed by Microsoft'
    } else {
        $p = Start-Process -PassThru $wv2 -ArgumentList '/silent', '/install'
        if (-not $p.WaitForExit(600000)) { $problems += 'the WebView2 bootstrapper did not finish within 10 minutes' }
        elseif ($p.ExitCode) { $problems += "the WebView2 bootstrapper exited $($p.ExitCode)" }
    }
} catch {
    $problems += "downloading the WebView2 bootstrapper failed: $_"
}
Get-ScheduledTask -TaskName 'MicrosoftEdgeUpdateTask*' | Disable-ScheduledTask | Out-Null
Get-Process MicrosoftEdgeUpdate -ErrorAction SilentlyContinue | Stop-Process -Force
# Services that work in the background during tests and no test needs: the Edge updater's, SysMain
# (prefetching into the file cache, which the warm state would save), Windows Search (indexes every
# file pushed into the profile) and telemetry.
foreach ($s in 'edgeupdate', 'edgeupdatem', 'SysMain', 'WSearch', 'DiagTrack') { Set-Service $s -StartupType Disabled }
# Delivery Optimization refuses Set-Service, but not its registry key.
Reg 'HKLM:\SYSTEM\CurrentControlSet\Services\DoSvc' Start 4

# .NET compiles its native images in the background some time after an install; until it has, every
# PowerShell start (each desk exec) takes about 0.7 s longer, and every run would do the work again.
foreach ($f in 'Framework', 'Framework64') {
    & "$env:WINDIR\Microsoft.NET\$f\v4.0.30319\ngen.exe" executeQueuedItems /nologo | Out-Null
    if ($LASTEXITCODE) { $problems += "ngen ($f) exited $LASTEXITCODE" }
}

Get-Service sshd, QEMU-GA -ErrorAction SilentlyContinue | Format-Table -AutoSize
# What every later step of the harness depends on; without these the image only fails later, as
# "SSH not reachable", with nothing pointing back at the install.
if ((Get-Service sshd -ErrorAction SilentlyContinue).Status -ne 'Running') { $problems += 'sshd is not running' }
if (-not (Test-Path C:\ProgramData\ssh\administrators_authorized_keys)) { $problems += 'the SSH key is missing' }
if (-not (Get-Service QEMU-GA -ErrorAction SilentlyContinue)) { $problems += 'qemu-guest-agent is not installed' }
if (-not (Test-Path C:\desk\agent.ps1)) { $problems += 'the in-session agent is missing' }
# Not needed by the harness, but an image that quietly kept them would be the heavy one again.
foreach ($s in 'edgeupdate', 'edgeupdatem', 'SysMain', 'WSearch', 'DiagTrack') {
    if ((Get-Service $s -ErrorAction SilentlyContinue).StartType -ne 'Disabled') { $problems += "$s is not disabled" }
}
if ((Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Services\DoSvc').Start -ne 4) { $problems += 'DoSvc is not disabled' }
$edge = @(Get-ScheduledTask -TaskName 'MicrosoftEdgeUpdateTask*' -ErrorAction SilentlyContinue)
if (-not $edge -or ($edge | Where-Object State -ne 'Disabled')) { $problems += 'the Edge update tasks are not disabled' }
$report = if ($problems) { 'desk-firstboot: FAILED ' + ($problems -join '; ') } else { 'desk-firstboot: ok' }
$report
try {
    $serial = New-Object System.IO.Ports.SerialPort 'COM1', 115200
    $serial.WriteTimeout = 5000
    $serial.Open()
    $serial.WriteLine($report)
    $serial.Close()
} catch {
    "no serial console to report on: $_"
}
Stop-Transcript
Stop-Computer -Force
