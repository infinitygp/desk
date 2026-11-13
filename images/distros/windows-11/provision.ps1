# Runs over SSH (as tester, elevated) before the warm state is saved; the reboot that follows
# (relogin in block.toml) starts the in-session agent again. Keep it idempotent.
# Any error ends it: a setting that could not be made must not end up in a warm state that looks
# complete.
$ErrorActionPreference = 'Stop'
$run = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
# conhost --headless: Windows Terminal, the default console host, ignores -WindowStyle Hidden.
# agent-loop.ps1 starts the agent again when it ends.
Set-ItemProperty $run -Name desk-agent -Value 'conhost.exe --headless powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\desk\agent-loop.ps1'

# English (US) with the keyboard layout desk types through: US, or Polish (Programmers), the US
# layout plus AltGr letters. An earlier image listed Polish as a language of its own, which made
# Store apps (Notepad) show Polish while Windows itself is English.
$tip = @{ us = '0409:00000409'; pl = '0409:00000415' }[$env:DESK_LAYOUT]
if (-not $tip) { throw "no keyboard layout for DESK_LAYOUT '$env:DESK_LAYOUT'" }
$langs = New-WinUserLanguageList en-US
$langs[0].InputMethodTips.Clear()
$langs[0].InputMethodTips.Add($tip)
Set-WinUserLanguageList $langs -Force

# The host's time zone, from desk as an IANA name; ICU, which Windows ships, knows its Windows id.
Add-Type -TypeDefinition @'
using System.Runtime.InteropServices;
using System.Text;
public static class DeskZone {
    [DllImport("icu.dll", CharSet = CharSet.Unicode)]
    static extern int ucal_getWindowsTimeZoneID(string id, int len, StringBuilder winid, int capacity, ref int status);
    public static string Windows(string iana) {
        var id = new StringBuilder(128);
        int status = 0;
        int n = ucal_getWindowsTimeZoneID(iana, iana.Length, id, id.Capacity, ref status);
        return status > 0 || n == 0 ? null : id.ToString(0, n);
    }
}
'@
$zone = [DeskZone]::Windows($env:DESK_TIMEZONE)
if (-not $zone) { throw "Windows has no time zone for '$env:DESK_TIMEZONE'" }
Set-TimeZone -Id $zone

# The Store updating Calculator or Notepad mid-test makes them fail to start.
$store = 'HKLM:\SOFTWARE\Policies\Microsoft\WindowsStore'
if (-not (Test-Path $store)) { New-Item $store -Force | Out-Null }
New-ItemProperty $store -Name AutoDownload -Value 2 -PropertyType DWord -Force | Out-Null

# Notepad opens a teaching tip ("Notepad automatically saves your progress") over its first window,
# and UI Automation gets nothing from the window while it is open. Its settings say it was closed,
# as "Got it" leaves them; they are of a packaged app's own value types, which only reg import writes.
$hive = "$env:LOCALAPPDATA\Packages\Microsoft.WindowsNotepad_8wekyb3d8bbwe\Settings\settings.dat"
$tips = "$env:TEMP\desk-notepad.reg"
@'
Windows Registry Editor Version 5.00

[HKEY_USERS\desk-notepad\LocalState]
"TeachingTipCheckCount"=hex(5f5e105):01,00,00,00,06,57,36,cc,d3,63,dd,01
"TeachingTipExplicitClose"=hex(5f5e10b):01,42,da,4c,dc,d3,63,dd,01
"TeachingTipVersion"=hex(5f5e105):00,00,00,00,06,57,36,cc,d3,63,dd,01
'@ | Set-Content -Encoding Unicode $tips
reg load HKU\desk-notepad $hive | Out-Null
if ($LASTEXITCODE) { throw "loading Notepad's settings ($hive) failed" }
# reg import reports its success on stderr, which this script would take for an error.
cmd /c "reg import `"$tips`" 2>nul"
$imported = $LASTEXITCODE
reg unload HKU\desk-notepad | Out-Null
if ($LASTEXITCODE) { throw "unloading Notepad's settings failed" }
Remove-Item $tips
if ($imported) { throw "writing Notepad's settings failed ($imported)" }

# Defender skips where tests run and write programs (profiles, program folders, temporary files and
# installers), so it takes no time from them. Tamper Protection keeps real-time protection on (only
# a click in Windows Security turns it off), but a local administrator may still exclude folders;
# AMSI blocks a script that excludes the whole drive as malicious. The install's Defender is the
# ISO's: a few minutes after boot it updates its platform and restarts, and meanwhile its settings
# can be neither read nor changed (0x800106ba), so they are tried until they hold.
$skip = 'C:\Users', 'C:\desk', 'C:\Program Files', 'C:\Program Files (x86)', 'C:\ProgramData', 'C:\Windows\Temp', 'C:\Windows\Installer'
$deadline = (Get-Date).AddMinutes(10)
while ($true) {
    try {
        Add-MpPreference -ExclusionPath $skip
        $missing = $skip | Where-Object { (Get-MpPreference).ExclusionPath -notcontains $_ }
        if (-not $missing) { break }
        $why = "did not exclude $($missing -join ', ')"
    } catch { $why = $_.Exception.Message.Trim() }
    if ((Get-Date) -gt $deadline) { throw "Defender, for 10 minutes: $why" }
    Start-Sleep 10
}

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

# Nothing of Windows' own works in the background while a test runs: it takes the CPU from the app under test,
# and a run restored from the warm state, its clock moved on by the state's age, would find it all due. Each of
# these was seen keeping an otherwise idle guest busy.
# Automatic Maintenance, after some minutes without input: defrag, disk and component cleanup and .NET
# compilation, 2-3 vCPU for 3 minutes, then Defender's scheduled scan, 1.5 vCPU for over 13 minutes. The Task
# Scheduler reads this when it starts, so it holds from the reboot after provisioning.
$maint = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Schedule\Maintenance'
if (-not (Test-Path $maint)) { New-Item $maint -Force | Out-Null }
New-ItemProperty $maint -Name MaintenanceDisabled -Value 1 -PropertyType DWord -Force | Out-Null
# The inventory and compatibility appraisal (CompatTelRunner), 8 minutes after boot, is for upgrades and telemetry.
Set-Service InventorySvc -StartupType Disabled
# OneDrive, off by policy (firstboot.ps1), still updates itself through its own tasks.
Get-ScheduledTask -TaskName 'OneDrive *' | Disable-ScheduledTask | Out-Null
# Windows Update scans despite NoAutoUpdate (firstboot.ps1): 15 minutes after the first logon its orchestrator
# installs Store updates (AutoDownload notwithstanding), and a clock moved on, as at every restore, finds its scans
# and the Store's overdue, 1.5-4 vCPU for 4 minutes. An update server of its own where nothing listens, and none of
# Microsoft's, make them fail at once; the Store and winget's Store source lose their server too, Defender takes
# its definitions from its own download site.
$wupolicy = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate'
if (-not (Test-Path "$wupolicy\AU")) { New-Item "$wupolicy\AU" -Force | Out-Null }
$nowhere = 'http://127.0.0.1:8530'
New-ItemProperty $wupolicy -Name WUServer -Value $nowhere -PropertyType String -Force | Out-Null
New-ItemProperty $wupolicy -Name WUStatusServer -Value $nowhere -PropertyType String -Force | Out-Null
New-ItemProperty $wupolicy -Name DoNotConnectToWindowsUpdateInternetLocations -Value 1 -PropertyType DWord -Force | Out-Null
New-ItemProperty "$wupolicy\AU" -Name UseWUServer -Value 1 -PropertyType DWord -Force | Out-Null
# What still started: when the clock moves on, the orchestrator's tasks, the Store's scans and the language
# components' and Setup's tasks, which set off component servicing (TiWorker), 4 vCPU together with the other
# overdue tasks; after the first logon the expedited updates and the language components' installer (15
# minutes), the DirectX database update (10). So those tasks are off, and the orchestrator and Windows Update
# cannot start. Delivery Optimization, switched off by firstboot.ps1, had set itself back to automatic.
$tasks = @{
    '\Microsoft\Windows\InstallService\' = 'ScanForUpdates', 'ScanForUpdatesAsUser', 'SmartRetry'
    '\Microsoft\Windows\LanguageComponentsInstaller\' = 'Installation', 'ReconcileLanguageResources'
    '\Microsoft\Windows\Setup\' = @('PITRTask')
    '\Microsoft\Windows\DirectX\' = @('DirectXDatabaseUpdater')
}
foreach ($path in $tasks.Keys) { $tasks[$path] | ForEach-Object { Disable-ScheduledTask -TaskPath $path -TaskName $_ | Out-Null } }
New-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Services\DoSvc' -Name Start -Value 4 -PropertyType DWord -Force | Out-Null
# The orchestrator's tasks and services refuse the administrator, so a one-shot task does this as SYSTEM.
$uso = '\Microsoft\Windows\UpdateOrchestrator\'
$tasks[$uso] = 'Schedule Scan', 'Schedule Scan Static Task', 'Start Oobe Expedite Work', 'StartOobeAppsScanAfterUpdate',
    'StartOobeAppsScan_LicenseAccepted', 'UIEOrchestrator'
$asSystem = @($tasks[$uso] | ForEach-Object { "schtasks /Change /TN `"$uso$_`" /Disable" }) +
    'sc config UsoSvc start= disabled', 'sc config wuauserv start= disabled'
$a = New-ScheduledTaskAction -Execute cmd.exe -Argument "/c $($asSystem -join ' & ')"
Register-ScheduledTask desk-as-system -Action $a -User SYSTEM -RunLevel Highest -Force | Out-Null
Start-ScheduledTask desk-as-system
$deadline = (Get-Date).AddMinutes(2)
# 0x41301: running; 0x41303: not started yet.
while ((Get-ScheduledTaskInfo desk-as-system).LastTaskResult -in 0x41301, 0x41303) {
    if ((Get-Date) -gt $deadline) { throw 'the task that runs as SYSTEM did not finish within 2 minutes' }
    Start-Sleep 1
}
Unregister-ScheduledTask desk-as-system -Confirm:$false
$loud = @()
foreach ($path in $tasks.Keys) {
    $loud += Get-ScheduledTask -TaskPath $path | Where-Object { $_.TaskName -in $tasks[$path] -and $_.State -ne 'Disabled' } |
        ForEach-Object { $path + $_.TaskName }
}
$loud += Get-Service UsoSvc, wuauserv | Where-Object StartType -ne Disabled | ForEach-Object Name
if ((Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Services\DoSvc').Start -ne 4) { $loud += 'DoSvc' }
if ((Get-ItemProperty $maint).MaintenanceDisabled -ne 1) { $loud += 'Automatic Maintenance' }
if ((Get-Service InventorySvc).StartType -ne 'Disabled') { $loud += 'InventorySvc' }
$p = Get-ItemProperty $wupolicy
if ($p.WUServer -ne $nowhere -or $p.WUStatusServer -ne $nowhere -or $p.DoNotConnectToWindowsUpdateInternetLocations -ne 1 -or
    (Get-ItemProperty "$wupolicy\AU").UseWUServer -ne 1) { $loud += 'Windows Update scans' }
$loud += Get-ScheduledTask -TaskName 'OneDrive *' | Where-Object State -ne Disabled | ForEach-Object TaskName
if ($loud) { throw "still enabled: $($loud -join ', ')" }
