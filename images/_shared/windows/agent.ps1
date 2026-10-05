# desk in-session agent. Started from the user's Run key, so it lives in the interactive desktop
# session that SSH cannot reach: it answers UI Automation queries and starts programs there.
# One JSON request per TCP connection, one JSON reply. The port is forwarded by the host only.
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
. (Join-Path $PSScriptRoot 'uia.ps1')
# Per-monitor DPI aware, so UI Automation reports physical pixels, the same as the screenshots,
# at any scaling (a DPI-unaware process gets coordinates scaled down at 125 % and up).
Add-Type -Namespace Desk -Name Dpi -MemberDefinition @'
[DllImport("user32.dll")] public static extern bool SetProcessDpiAwarenessContext(IntPtr value);
'@
[void][Desk.Dpi]::SetProcessDpiAwarenessContext([IntPtr]-4)   # PER_MONITOR_AWARE_V2
Add-Type -Namespace Desk -Name Win -MemberDefinition @'
[DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
[DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int cmd);
[DllImport("user32.dll")] public static extern void keybd_event(byte vk, byte scan, uint flags, UIntPtr extra);
[DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
[DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern IntPtr SendMessageTimeout(IntPtr hwnd, uint msg, UIntPtr wp, string lp, uint flags, uint timeout, out UIntPtr result);
[DllImport("user32.dll")] public static extern void mouse_event(uint flags, int dx, int dy, int data, UIntPtr extra);
'@

function Get-TopWindowInfo {
    @($AE::RootElement.FindAll([Windows.Automation.TreeScope]::Children, $ControlView) |
      ForEach-Object { [pscustomobject]@{ Handle = $_.Current.NativeWindowHandle; Pid = $_.Current.ProcessId } })
}

function Get-TopHandles { @(Get-TopWindowInfo | ForEach-Object { $_.Handle }) }

# Windows does not let a background process (this agent) hand focus to the windows it starts,
# while a user starting a program gets its window in front. Find the new top-level window and
# bring it forward. SetForegroundWindow is only allowed for the process that sent the last input,
# so tap F24 first: Alt, the usual trick, makes apps show their access-key tips.
function Focus-NewWindow($before, $procId, $timeout = 15) {
    # Prefer a window of the started process. Packaged apps (Calculator) draw theirs from another
    # process, so after a short grace period any window that was not there before will do.
    $start = Get-Date
    $deadline = $start.AddSeconds($timeout)
    while ((Get-Date) -lt $deadline) {
        $fresh = @(Get-TopWindowInfo | Where-Object { $_.Handle -and ($before -notcontains $_.Handle) })
        $own = $fresh | Where-Object { $_.Pid -eq $procId } | Select-Object -First 1
        $new = if ($own) { $own.Handle }
               elseif ($fresh -and ((Get-Date) - $start).TotalSeconds -gt 3) { $fresh[0].Handle }
        if ($new) { return Show-Window $new }
        Start-Sleep -Milliseconds 250
    }
    $false
}

function Show-Window($handle) {
    [Desk.Win]::keybd_event(0x87, 0, 0, [UIntPtr]::Zero)
    [Desk.Win]::keybd_event(0x87, 0, 2, [UIntPtr]::Zero)
    [void][Desk.Win]::ShowWindow([IntPtr][long]$handle, 9)
    [Desk.Win]::SetForegroundWindow([IntPtr][long]$handle)
}

# The topmost window whose title or process name contains the query, brought to the front.
function Focus-Window($query) {
    $hit = $AE::RootElement.FindAll([Windows.Automation.TreeScope]::Children, $ControlView) | Where-Object {
        $_.Current.NativeWindowHandle -and (
            $_.Current.Name -like "*$query*" -or
            (Get-Process -Id $_.Current.ProcessId -ErrorAction SilentlyContinue).ProcessName -like "*$query*")
    } | Select-Object -First 1
    if (-not $hit) { return @{ ok = $true; title = '' } }
    @{ ok = $true; title = $hit.Current.Name; focused = (Show-Window $hit.Current.NativeWindowHandle) }
}

# Monitors: the layout desk composes its screenshots from, and changing modes and arrangement.
Add-Type -AssemblyName System.Windows.Forms
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
namespace Desk {
    public static class Display {
        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
        public struct DEVMODE {
            [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)] public string dmDeviceName;
            public short dmSpecVersion, dmDriverVersion, dmSize, dmDriverExtra;
            public int dmFields, dmPositionX, dmPositionY, dmDisplayOrientation, dmDisplayFixedOutput;
            public short dmColor, dmDuplex, dmYResolution, dmTTOption, dmCollate;
            [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)] public string dmFormName;
            public short dmLogPixels;
            public int dmBitsPerPel, dmPelsWidth, dmPelsHeight, dmDisplayFlags, dmDisplayFrequency, dmICMMethod,
                dmICMIntent, dmMediaType, dmDitherType, dmReserved1, dmReserved2, dmPanningWidth, dmPanningHeight;
        }
        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
        public struct DISPLAY_DEVICE {
            public int cb;
            [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)] public string DeviceName;
            [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 128)] public string DeviceString;
            public int StateFlags;
            [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 128)] public string DeviceID;
            [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 128)] public string DeviceKey;
        }
        [DllImport("user32.dll", CharSet = CharSet.Unicode)]
        public static extern bool EnumDisplayDevices(string dev, int i, ref DISPLAY_DEVICE dd, int flags);
        [DllImport("user32.dll", CharSet = CharSet.Unicode)]
        public static extern bool EnumDisplaySettings(string dev, int mode, ref DEVMODE dm);
        [DllImport("user32.dll", CharSet = CharSet.Unicode)]
        public static extern int ChangeDisplaySettingsEx(string dev, ref DEVMODE dm, IntPtr hwnd, int flags, IntPtr lp);
        // Every output (one per virtio GPU) side by side, left to right, optionally in one mode.
        // In C#: structs passed by [ref] from PowerShell do not come back.
        public static string Arrange(int width, int height) {
            var log = "";
            int x = 0;
            for (int i = 0; i < 16; i++) {
                var dd = new DISPLAY_DEVICE();
                dd.cb = Marshal.SizeOf(dd);
                if (!EnumDisplayDevices(null, i, ref dd, 0)) break;
                var dm = new DEVMODE();
                dm.dmSize = (short)Marshal.SizeOf(dm);
                // Current settings, or the stored ones of an output not on the desktop yet.
                if (!EnumDisplaySettings(dd.DeviceName, -1, ref dm) || dm.dmPelsWidth == 0)
                    if (!EnumDisplaySettings(dd.DeviceName, -2, ref dm)) continue;
                if (width > 0) { dm.dmPelsWidth = width; dm.dmPelsHeight = height; }
                if (dm.dmPelsWidth == 0) continue;
                dm.dmPositionX = x;
                dm.dmPositionY = 0;
                dm.dmFields = 0x20 | 0x80000 | 0x100000;           // POSITION | PELSWIDTH | PELSHEIGHT
                int flags = 0x01 | (i == 0 ? 0x10 : 0);              // UPDATEREGISTRY, SET_PRIMARY
                int r = ChangeDisplaySettingsEx(dd.DeviceName, ref dm, IntPtr.Zero, flags, IntPtr.Zero);
                log += dd.DeviceName + " " + dm.dmPelsWidth + "x" + dm.dmPelsHeight + "+" + x + ": " + r + "\n";
                if (r != 0) throw new Exception(dd.DeviceName + ": mode " + dm.dmPelsWidth + "x" + dm.dmPelsHeight + " refused (" + r + ")");
                x += dm.dmPelsWidth;
            }
            return log;
        }
    }
}
'@

function Get-Monitors {
    ,@([System.Windows.Forms.Screen]::AllScreens | Sort-Object { $_.DeviceName } | ForEach-Object {
        @{ name = $_.DeviceName; primary = $_.Primary
           bounds = @($_.Bounds.X, $_.Bounds.Y, $_.Bounds.Width, $_.Bounds.Height) } })
}

function Set-Displays($width, $height) {
    $w = if ($width) { [int]$width } else { 0 }
    $h = if ($height) { [int]$height } else { 0 }
    [void][Desk.Display]::Arrange($w, $h)
    Start-Sleep -Milliseconds 500
    Get-Monitors
}

# A program in this session with extra environment variables, optionally waiting for it and
# returning its exit code and output (desk exec --gui --wait --env), which SSH cannot see.
function Invoke-Program($req) {
    $psi = New-Object Diagnostics.ProcessStartInfo
    $psi.FileName = $req.file
    $psi.Arguments = (@($req.args) | Where-Object { $_ -ne $null } | ForEach-Object {
        if ($_ -match '[\s"]' -or $_ -eq '') { '"' + ($_ -replace '(\\*)"', '$1$1\"' -replace '(\\+)$', '$1$1') + '"' } else { $_ }
    }) -join ' '
    $psi.UseShellExecute = $false
    if ($req.env) { foreach ($p in $req.env.PSObject.Properties) { $psi.EnvironmentVariables[$p.Name] = [string]$p.Value } }
    if ($req.wait) {
        $psi.RedirectStandardOutput = $true
        $psi.RedirectStandardError = $true
        $psi.CreateNoWindow = $true   # the output comes back to desk, no console window on the desktop
    }
    $before = Get-TopHandles
    $p = [Diagnostics.Process]::Start($psi)
    if (-not $req.wait) { return @{ ok = $true; pid = $p.Id; focused = (Focus-NewWindow $before $p.Id) } }
    $out = New-Object IO.MemoryStream
    $err = New-Object IO.MemoryStream
    $copies = @($p.StandardOutput.BaseStream.CopyToAsync($out), $p.StandardError.BaseStream.CopyToAsync($err))
    $ms = if ($req.timeout_s) { [int](1000 * $req.timeout_s) } else { -1 }
    $done = $p.WaitForExit($ms)
    if (-not $done) { try { $p.Kill() } catch { } }
    [void][Threading.Tasks.Task]::WaitAll($copies, 5000)
    $reply = @{ ok = $true; pid = $p.Id; stdout = (Decode-Output $out.ToArray()); stderr = (Decode-Output $err.ToArray()) }
    if ($done) { $reply.code = $p.ExitCode } else { $reply.timeout = $true }
    $reply
}

# Programs that print UTF-8 (Rust, Python with UTF-8 mode) come through as they are; console
# tools write in the OEM code page when their output is redirected.
function Decode-Output($bytes) {
    try { (New-Object Text.UTF8Encoding($false, $true)).GetString($bytes) }
    catch { [Text.Encoding]::GetEncoding([Globalization.CultureInfo]::CurrentCulture.TextInfo.OEMCodePage).GetString($bytes) }
}

$port = 47011
$listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Any, $port)
$listener.Start()
$utf8 = New-Object Text.UTF8Encoding($false)
while ($true) {
    $client = $listener.AcceptTcpClient()
    try {
        $stream = $client.GetStream()
        $reader = New-Object IO.StreamReader($stream, $utf8)
        $req = $reader.ReadLine() | ConvertFrom-Json
        $reply = switch ($req.cmd) {
            'ping' { @{ ok = $true; session = (Get-Process -Id $PID).SessionId; user = $env:USERNAME } }
            'run' {
                if ($req.wait -or $req.env) { Invoke-Program $req }
                else {
                    $before = Get-TopHandles
                    $p = if ($req.args) { Start-Process -FilePath $req.file -ArgumentList $req.args -PassThru }
                         else { Start-Process -FilePath $req.file -PassThru }
                    @{ ok = $true; pid = $p.Id; focused = (Focus-NewWindow $before $p.Id) }
                }
            }
            'uia' { Invoke-Uia $req.op $req.arg $req.text }
            'focus' { Focus-Window $req.query }
            'monitors' { @{ ok = $true; monitors = (Get-Monitors) } }
            # WM_SETTINGCHANGE to every window, as Settings sends after a change (theme colours).
            'settingchange' {
                $r = [UIntPtr]::Zero
                [void][Desk.Win]::SendMessageTimeout([IntPtr]0xffff, 0x1A, [UIntPtr]::Zero, $req.area, 2, 5000, [ref]$r)
                @{ ok = $true }
            }
            # Pointer on monitors other than the primary, which QEMU's absolute tablet cannot reach.
            'pointer' {
                $flag = @{ 'left-down' = 0x2; 'left-up' = 0x4; 'right-down' = 0x8; 'right-up' = 0x10
                           'middle-down' = 0x20; 'middle-up' = 0x40 }
                switch ($req.action) {
                    'move' { [void][Desk.Win]::SetCursorPos([int]$req.x, [int]$req.y) }
                    'wheel' { [Desk.Win]::mouse_event(0x800, 0, 0, 120 * [int]$req.delta, [UIntPtr]::Zero) }
                    default { [Desk.Win]::mouse_event($flag["$($req.button)-$($req.action)"], 0, 0, 0, [UIntPtr]::Zero) }
                }
                @{ ok = $true }
            }
            'display' { @{ ok = $true; monitors = (Set-Displays $req.width $req.height) } }
            default { @{ error = "unknown command $($req.cmd)" } }
        }
    } catch {
        $reply = @{ error = $_.Exception.Message }
    }
    try {
        $bytes = $utf8.GetBytes(($reply | ConvertTo-Json -Depth 20 -Compress))
        $stream.Write($bytes, 0, $bytes.Length)
    } catch { }
    $client.Close()
}
