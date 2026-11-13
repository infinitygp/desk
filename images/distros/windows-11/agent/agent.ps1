# desk in-session agent. Started from the user's Run key, so it lives in the interactive desktop
# session that SSH cannot reach: it answers UI Automation queries and starts programs there.
# One JSON request per TCP connection, one JSON reply. The port is forwarded by the host only.
# Requests are served one at a time, so none may hold the agent: a connection that sends nothing
# is dropped after 5 s, a tree walk ends after 20 s (uia.ps1), and a program that is waited for
# (exec --gui --wait) is waited for on a thread of its own, which answers when it has ended.
$ErrorActionPreference = 'Stop'
# An error that ends the agent goes to agent.log, which the report lists; agent-loop.ps1 starts it again.
trap { Add-Content (Join-Path $PSScriptRoot 'agent.log') ('{0:s} the in-session agent failed: {1}' -f (Get-Date), $_); break }
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
. (Join-Path $PSScriptRoot 'uia.ps1')
# This process runs with -ExecutionPolicy Bypass, which PowerShell keeps in the environment, where
# every program the agent starts would inherit it: a product would run its own scripts that a
# user's Windows (Restricted by default) refuses. Its own scripts are loaded by now.
Remove-Item Env:PSExecutionPolicyPreference -ErrorAction Ignore
# Per-monitor DPI aware, so UI Automation reports physical pixels, the same as the screenshots,
# at any scaling (a DPI-unaware process gets coordinates scaled down at 125 % and up).
Add-Type -Namespace Desk -Name Dpi -MemberDefinition @'
[DllImport("user32.dll")] public static extern bool SetProcessDpiAwarenessContext(IntPtr value);
'@
[void][Desk.Dpi]::SetProcessDpiAwarenessContext([IntPtr]-4)   # PER_MONITOR_AWARE_V2
Add-Type -Namespace Desk -Name Win -MemberDefinition @'
[DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
[DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
[DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int cmd);
[DllImport("user32.dll")] public static extern bool IsIconic(IntPtr h);
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
    # A stopwatch, not the clock: the guest's clock may be moved while this waits (desk clock), which
    # would hold the agent until the clock catches up or end the wait at once.
    $clock = [Diagnostics.Stopwatch]::StartNew()
    while ($clock.Elapsed.TotalSeconds -lt $timeout) {
        $fresh = @(Get-TopWindowInfo | Where-Object { $_.Handle -and ($before -notcontains $_.Handle) })
        $own = $fresh | Where-Object { $_.Pid -eq $procId } | Select-Object -First 1
        $new = if ($own) { $own.Handle }
               elseif ($fresh -and $clock.Elapsed.TotalSeconds -gt 3) { $fresh[0].Handle }
        if ($new) { return Show-Window $new }
        Start-Sleep -Milliseconds 250
    }
    $false
}

function Show-Window($handle) {
    [Desk.Win]::keybd_event(0x87, 0, 0, [UIntPtr]::Zero)
    [Desk.Win]::keybd_event(0x87, 0, 2, [UIntPtr]::Zero)
    # SW_RESTORE only for a minimized window: it would also take a maximized one back to its normal size.
    $h = [IntPtr][long]$handle
    [void][Desk.Win]::ShowWindow($h, $(if ([Desk.Win]::IsIconic($h)) { 9 } else { 5 }))
    [void][Desk.Win]::SetForegroundWindow($h)
    # Whether it is in front now, as the user would see it: SetForegroundWindow's own answer only
    # says whether Windows accepted the request.
    $clock = [Diagnostics.Stopwatch]::StartNew()
    while ($clock.Elapsed.TotalSeconds -lt 2) {
        if ([Desk.Win]::GetForegroundWindow() -eq [IntPtr][long]$handle) { return $true }
        Start-Sleep -Milliseconds 100
    }
    $false
}

# The topmost window whose title or process name contains the query, brought to the front.
function Focus-Window($query) {
    # As written: [ and ] in a title ("Report [draft]") would otherwise be a wildcard character class.
    $like = "*$([WildcardPattern]::Escape($query))*"
    $hit = $AE::RootElement.FindAll([Windows.Automation.TreeScope]::Children, $ControlView) | Where-Object {
        $_.Current.NativeWindowHandle -and (
            $_.Current.Name -like $like -or
            (Get-Process -Id $_.Current.ProcessId -ErrorAction SilentlyContinue).ProcessName -like $like)
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

Add-Type -ReferencedAssemblies System.Web.Extensions -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Net.Sockets;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
namespace Desk {
    public static class Waiter {
        // The requests with an id that were begun, with the answer once there is one: desk sends a
        // request again under its id when the connection was lost, and what was begun is not done
        // twice. Read and written by the loop and by the threads that answer waited programs.
        static readonly Dictionary<string, string> replies = new Dictionary<string, string>();
        static readonly Queue<string> begun = new Queue<string>();

        // False when the id was begun already; otherwise it is now, and the oldest beyond 200 is forgotten.
        public static bool Begin(string id) {
            lock (replies) {
                if (replies.ContainsKey(id)) return false;
                replies[id] = null;
                begun.Enqueue(id);
                if (begun.Count > 200) replies.Remove(begun.Dequeue());
                return true;
            }
        }

        // The answer of a begun id, null while it is still being worked on.
        public static string Reply(string id) {
            lock (replies) {
                string json;
                return replies.TryGetValue(id, out json) ? json : null;
            }
        }

        public static void Store(string id, string json) {
            if (String.IsNullOrEmpty(id)) return;
            lock (replies) { if (replies.ContainsKey(id)) replies[id] = json; }
        }

        // Programs that print UTF-8 (Rust, Python with UTF-8 mode) come through as they are;
        // console tools write in the OEM code page when their output is redirected.
        static string Decode(byte[] bytes) {
            try { return new UTF8Encoding(false, true).GetString(bytes); }
            catch (Exception) { return Encoding.GetEncoding(CultureInfo.CurrentCulture.TextInfo.OEMCodePage).GetString(bytes); }
        }

        // Waits for `p` (at most `ms` milliseconds; -1: for ever) away from the agent's loop and
        // answers the request on `client` with its exit code and output, then closes it.
        public static void Answer(Process p, TcpClient client, int ms, string id) {
            var stdout = new MemoryStream();
            var stderr = new MemoryStream();
            var copies = new Task[] { p.StandardOutput.BaseStream.CopyToAsync(stdout), p.StandardError.BaseStream.CopyToAsync(stderr) };
            ThreadPool.QueueUserWorkItem(unused => {
                var reply = new Dictionary<string, object>();
                try {
                    bool done = p.WaitForExit(ms);
                    if (!done) { try { p.Kill(); } catch (Exception) { } }
                    try { Task.WaitAll(copies, 5000); } catch (Exception) { }
                    reply["ok"] = true;
                    reply["pid"] = p.Id;
                    reply["stdout"] = Decode(stdout.ToArray());
                    reply["stderr"] = Decode(stderr.ToArray());
                    if (done) reply["code"] = p.ExitCode; else reply["timeout"] = true;
                } catch (Exception e) {
                    reply.Clear();
                    reply["error"] = e.Message;
                }
                try {
                    var json = new JavaScriptSerializer { MaxJsonLength = int.MaxValue }.Serialize(reply);
                    Store(id, json);
                    var bytes = new UTF8Encoding(false).GetBytes(json);
                    client.GetStream().Write(bytes, 0, bytes.Length);
                } catch (Exception) { }
                client.Close();
            });
        }
    }
}
'@

# One command line of separate arguments, quoted the way programs split it again. Start-Process
# -ArgumentList joins an array with spaces only, which splits an argument that holds one.
function Join-Arguments($list) {
    (@($list) | Where-Object { $_ -ne $null } | ForEach-Object {
        if ($_ -match '[\s"]' -or $_ -eq '') { '"' + ($_ -replace '(\\*)"', '$1$1\"' -replace '(\\+)$', '$1$1') + '"' } else { $_ }
    }) -join ' '
}

# What Start-Process (ShellExecute) finds and Process.Start without it does not: a program
# registered under App Paths (msedge.exe, chrome.exe), which is not on PATH.
function Resolve-Program($file) {
    if ([IO.Path]::GetDirectoryName($file) -or (Get-Command $file -CommandType Application -ErrorAction SilentlyContinue)) {
        return $file
    }
    $name = if ([IO.Path]::GetExtension($file)) { $file } else { "$file.exe" }
    foreach ($root in 'HKCU:', 'HKLM:') {
        $path = (Get-ItemProperty "$root\Software\Microsoft\Windows\CurrentVersion\App Paths\$name" -ErrorAction SilentlyContinue).'(default)'
        if ($path) { return [Environment]::ExpandEnvironmentVariables($path.Trim('"')) }
    }
    $file
}

# A program in this session with extra environment variables, optionally waiting for it and
# returning its exit code and output (desk exec --gui --wait --env), which SSH cannot see.
function Invoke-Program($req, $client) {
    $psi = New-Object Diagnostics.ProcessStartInfo
    $psi.FileName = Resolve-Program $req.file
    $psi.Arguments = Join-Arguments $req.args
    $psi.UseShellExecute = $false
    if ($req.env) { foreach ($p in $req.env.PSObject.Properties) { $psi.EnvironmentVariables[$p.Name] = [string]$p.Value } }
    if ($req.wait) {
        $psi.RedirectStandardOutput = $true
        $psi.RedirectStandardError = $true
        $psi.CreateNoWindow = $true   # the output comes back to desk, no console window on the desktop
    }
    if (-not $req.wait) {
        $before = Get-TopHandles
        $p = [Diagnostics.Process]::Start($psi)
        return @{ ok = $true; pid = $p.Id; focused = (Focus-NewWindow $before $p.Id) }
    }
    $p = [Diagnostics.Process]::Start($psi)
    $ms = if ($req.timeout_s) { [int][Math]::Min(1000 * [double]$req.timeout_s, [int]::MaxValue) } else { -1 }
    [Desk.Waiter]::Answer($p, $client, $ms, [string]$req.id)
    # From here the connection is the waiting thread's: it answers and closes it. Not before:
    # a hand-off that failed is answered by the loop like any other error.
    $script:handed = $true
}

$port = 47011
$listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Any, $port)
$listener.Start()
$utf8 = New-Object Text.UTF8Encoding($false)
while ($true) {
    $client = $listener.AcceptTcpClient()
    $script:handed = $false
    $reply = $null
    $stream = $null
    $id = $null
    try {
        # A connection that sends no request (a probe, a caller that gave up) is not waited for.
        $client.ReceiveTimeout = 5000
        $client.SendTimeout = 30000
        $stream = $client.GetStream()
        $reader = New-Object IO.StreamReader($stream, $utf8)
        $req = $reader.ReadLine() | ConvertFrom-Json
        # Closed already: the caller gave up while an earlier request held the agent and was told
        # this one failed, so it must not happen now (an action asked again would happen twice).
        # Only for callers that keep the connection open while they wait; older desks half-close it.
        if ($req.open -and $client.Client.Poll(0, [Net.Sockets.SelectMode]::SelectRead) -and $client.Client.Available -eq 0) {
            $client.Close()
            continue
        }
        # desk sends a request again under its id when the connection to it was lost: one begun
        # already is not done twice. Its answer is sent again or, while the program it waits for
        # still runs, that it was begun (the answer goes to the connection that was lost).
        if ($req.id) {
            if (-not [Desk.Waiter]::Begin([string]$req.id)) {
                $json = [Desk.Waiter]::Reply([string]$req.id)
                if ($null -eq $json) {
                    $json = @{ error = "request $($req.id) was begun already and is still being worked on; not begun again"
                               begun = $true } | ConvertTo-Json -Compress
                }
                $bytes = $utf8.GetBytes($json)
                $stream.Write($bytes, 0, $bytes.Length)
                $client.Close()
                continue
            }
            $id = [string]$req.id
        }
        $reply = switch ($req.cmd) {
            # ids: this agent knows request ids, so desk may send a request again under its id.
            'ping' { @{ ok = $true; session = (Get-Process -Id $PID).SessionId; user = $env:USERNAME; ids = $true } }
            'run' {
                if ($req.wait -or $req.env) { Invoke-Program $req $client }
                else {
                    $before = Get-TopHandles
                    $p = if ($req.args) { Start-Process -FilePath $req.file -ArgumentList (Join-Arguments $req.args) -PassThru }
                         else { Start-Process -FilePath $req.file -PassThru }
                    @{ ok = $true; pid = $p.Id; focused = (Focus-NewWindow $before $p.Id) }
                }
            }
            'uia' { Invoke-Uia $req.op $req.arg $req.text }
            'focus' { Focus-Window $req.query }
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
    if ($script:handed) { continue }
    if ($null -eq $reply) { $reply = @{ error = "the agent has no answer to $($req.cmd)" } }
    try {
        $json = $reply | ConvertTo-Json -Depth 20 -Compress
        [Desk.Waiter]::Store($id, $json)
        $bytes = $utf8.GetBytes($json)
        if ($stream) { $stream.Write($bytes, 0, $bytes.Length) }
    } catch { }
    $client.Close()
}
