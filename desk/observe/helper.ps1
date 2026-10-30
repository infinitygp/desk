# desk observation helper (Windows PowerShell 5.1). Pushed by desk to C:\desk\observe\helper.ps1
# when its SHA-256 differs from the host's copy, and run as a script block (no execution policy
# applies to that) with one request: base64 of UTF-8 JSON {"op": ..., ...}. It answers with one line,
# @@desk-observe@@ and JSON. It decides nothing about tools: the providers on the host build the
# command lines; this runs them within a time limit, starts captures detached from the SSH
# session, tells processes apart by PID and creation time, and reads what the guest has.
# With -Watch it is the watchdog of one capture: it stops the capture when its time is up or its
# files have grown to their limit, and writes why to a file desk reads.
param([string]$Request, [string]$Watch)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$Root = 'C:\desk\observe'

function Reply($obj) { '@@desk-observe@@' + (ConvertTo-Json -InputObject $obj -Depth 8 -Compress) }
function Decode($b64) { [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($b64)) | ConvertFrom-Json }
function NowMs { [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() }

# Microseconds since 1601 (UTC): WMI keeps creation times to the microsecond, Get-Process to 100 ns.
function Micro($dt) {
    if ($null -eq $dt) { return $null }
    $rest = [long]0
    [Math]::DivRem([long]$dt.ToUniversalTime().ToFileTimeUtc(), [long]10, [ref]$rest)
}

# The same process: same PID and a creation time within $Slack microseconds (WMI and Get-Process
# round differently); a PID reused by another process has another creation time.
$Slack = 2000
function Test-Alive($id) {
    $p = Get-Process -Id ([int]$id.pid) -ErrorAction SilentlyContinue
    if (-not $p) { return $false }
    try { $created = Micro $p.StartTime } catch {
        $c = Get-CimInstance Win32_Process -Filter "ProcessId = $([int]$id.pid)" -ErrorAction SilentlyContinue
        if (-not $c) { return $false }
        $created = Micro $c.CreationDate
    }
    [Math]::Abs([long]$created - [long]$id.created) -le $Slack
}

function Under-Root($path) {
    $full = [IO.Path]::GetFullPath($path)
    $full.StartsWith($Root + '\', [StringComparison]::OrdinalIgnoreCase)
}

function Oem { [Text.Encoding]::GetEncoding([Globalization.CultureInfo]::CurrentCulture.TextInfo.OEMCodePage) }

# A program run to its end, at most $timeout seconds; then it and what it started are ended.
function Invoke-Tool($exe, $arguments, $timeout, $cwd, $encoding) {
    $psi = New-Object Diagnostics.ProcessStartInfo
    $psi.FileName = $exe
    if ($arguments) { $psi.Arguments = $arguments }
    if ($cwd) { $psi.WorkingDirectory = $cwd }
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $enc = switch ($encoding) { 'utf8' { [Text.Encoding]::UTF8 } 'unicode' { [Text.Encoding]::Unicode } default { Oem } }
    $psi.StandardOutputEncoding = $enc
    $psi.StandardErrorEncoding = $enc
    $sw = [Diagnostics.Stopwatch]::StartNew()
    try { $p = [Diagnostics.Process]::Start($psi) } catch {
        return @{ rc = $null; started = $false; error = $_.Exception.Message; stdout = ''; stderr = ''; timed_out = $false; seconds = 0 }
    }
    $out = $p.StandardOutput.ReadToEndAsync()
    $err = $p.StandardError.ReadToEndAsync()
    $timedOut = -not $p.WaitForExit([int]([double]$timeout * 1000))
    if ($timedOut) {
        & taskkill.exe /T /F /PID $p.Id 2>&1 | Out-Null
        [void]$p.WaitForExit(10000)
    } else {
        $p.WaitForExit()
    }
    [void]$out.Wait(10000)
    [void]$err.Wait(10000)
    @{
        rc = $(if ($timedOut) { $null } else { $p.ExitCode })
        started = $true
        timed_out = $timedOut
        seconds = [Math]::Round($sw.Elapsed.TotalSeconds, 1)
        stdout = $(if ($out.IsCompleted) { $out.Result } else { '' })
        stderr = $(if ($err.IsCompleted) { $err.Result } else { '' })
    }
}

function Size-Of($path) {
    if (-not (Test-Path -LiteralPath $path)) { return 0 }
    $item = Get-Item -LiteralPath $path -Force
    if ($item.PSIsContainer) {
        $sum = 0
        Get-ChildItem -LiteralPath $path -Recurse -File -Force -ErrorAction SilentlyContinue | ForEach-Object { $sum += $_.Length }
        return $sum
    }
    $item.Length
}

function Boot { Micro (Get-CimInstance Win32_OperatingSystem).LastBootUpTime }

function Op-Probe($req) {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($id)
    $os = Get-CimInstance Win32_OperatingSystem
    $out = @{
        user = $id.Name
        # True only with an elevated (full) token: the Administrators group is checked for real.
        admin = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
        os = @{ caption = $os.Caption; version = $os.Version; build = $os.BuildNumber; arch = $env:PROCESSOR_ARCHITECTURE }
        session = [Diagnostics.Process]::GetCurrentProcess().SessionId
        boot = Micro $os.LastBootUpTime
        files = @{}; commands = @{}; services = @{}; registry = @{}; processes = @()
    }
    foreach ($f in @($req.files)) {
        if (-not $f) { continue }
        $info = @{ exists = (Test-Path -LiteralPath $f -PathType Leaf) }
        if ($info.exists) {
            $item = Get-Item -LiteralPath $f
            $info.size = $item.Length
            $info.version = $item.VersionInfo.FileVersion
            $info.product = $item.VersionInfo.ProductName
            if ($req.hash) { $info.sha256 = (Get-FileHash -LiteralPath $f -Algorithm SHA256).Hash.ToLower() }
        }
        $out.files[$f] = $info
    }
    foreach ($c in @($req.commands)) {
        if (-not $c) { continue }
        $cmd = Get-Command $c -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        $out.commands[$c] = $(if ($cmd) { @{ path = $cmd.Source; version = $cmd.Version.ToString() } } else { $null })
    }
    foreach ($s in @($req.services)) {
        if (-not $s) { continue }
        $svc = Get-CimInstance Win32_SystemDriver -Filter "Name = '$s'" -ErrorAction SilentlyContinue
        if (-not $svc) { $svc = Get-CimInstance Win32_Service -Filter "Name = '$s'" -ErrorAction SilentlyContinue }
        $out.services[$s] = $(if ($svc) { @{ state = $svc.State; start = $svc.StartMode; path = $svc.PathName } } else { $null })
    }
    foreach ($r in @($req.registry)) {
        if (-not $r) { continue }
        $v = Get-ItemProperty -LiteralPath $r.path -Name $r.name -ErrorAction SilentlyContinue
        $out.registry["$($r.path)\$($r.name)"] = $(if ($v) { $v.($r.name) } else { $null })
    }
    if ($req.processes) {
        $names = @($req.processes)
        $out.processes = @(Get-CimInstance Win32_Process | Where-Object { $names -contains $_.Name } | ForEach-Object {
            @{ pid = [int]$_.ProcessId; ppid = [int]$_.ParentProcessId; name = $_.Name; path = $_.ExecutablePath; created = Micro $_.CreationDate }
        })
    }
    if ($req.etw) {
        if (Get-Command Get-EtwTraceSession -ErrorAction SilentlyContinue) {
            $out.etw_sessions = @(Get-EtwTraceSession -ErrorAction SilentlyContinue | ForEach-Object { $_.Name })
        } else {
            $out.etw_sessions = $null
        }
    }
    if ($req.usb) {
        $out.usb = @(Get-CimInstance Win32_PnPEntity | Where-Object { $_.DeviceID -like 'USB\VID_*' } | ForEach-Object {
            @{ id = $_.DeviceID; name = $_.Name; status = $_.Status; service = $_.Service }
        })
    }
    $out
}

function Op-Launch($req) {
    # Without redirection Start-Process starts it through ShellExecute, which hands over none of
    # this session's handles: with its output redirected the program would inherit the pipe SSH
    # (or whoever started this PowerShell) reads, and the caller would wait until it ended.
    $sp = @{ FilePath = $req.exe; PassThru = $true; WindowStyle = 'Hidden' }
    if ($req.args) { $sp.ArgumentList = $req.args }
    if ($req.cwd) { $sp.WorkingDirectory = $req.cwd }
    $p = Start-Process @sp
    $created = $null
    try { $created = Micro $p.StartTime } catch { }
    if ($null -eq $created) {
        $c = Get-CimInstance Win32_Process -Filter "ProcessId = $($p.Id)" -ErrorAction SilentlyContinue
        if ($c) { $created = Micro $c.CreationDate }
    }
    @{ pid = $p.Id; created = $created; exited = $p.HasExited }
}

function Op-Alive($req) {
    @{ items = @(foreach ($id in @($req.procs)) {
        if ($null -eq $id) { continue }
        @{ pid = [int]$id.pid; created = [long]$id.created; alive = (Test-Alive $id) }
    }) }
}

function Op-StopProc($req) {
    if (-not (Test-Alive $req)) { return @{ stopped = $false; why = 'not running (or its PID is another process now)' } }
    $treeArg = $(if ($req.tree) { '/T' } else { '' })
    $r = Invoke-Tool 'taskkill.exe' "$treeArg /F /PID $([int]$req.pid)".Trim() 30 $null $null
    $deadline = (Get-Date).AddSeconds(15)
    while ((Test-Alive $req) -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 200 }
    @{ stopped = -not (Test-Alive $req); taskkill = $r }
}

function Op-WaitGone($req) {
    $deadline = (Get-Date).AddSeconds([double]$req.timeout)
    while ((Get-Date) -lt $deadline) {
        $left = @(@($req.procs) | Where-Object { $_ -and (Test-Alive $_) })
        if (-not $left) { return @{ gone = $true } }
        Start-Sleep -Milliseconds 250
    }
    @{ gone = $false }
}

function Op-Procs($req) {
    @{ items = @(Get-CimInstance Win32_Process | ForEach-Object {
        $cmd = $_.CommandLine
        if ($cmd -and $cmd.Length -gt 1000) { $cmd = $cmd.Substring(0, 1000) }
        @{ pid = [int]$_.ProcessId; ppid = [int]$_.ParentProcessId; name = $_.Name; path = $_.ExecutablePath
           cmdline = $cmd; created = Micro $_.CreationDate }
    }) }
}

function Op-Files($req) {
    $out = @{}
    foreach ($f in @($req.paths)) {
        if (-not $f) { continue }
        if (-not (Test-Path -LiteralPath $f)) { $out[$f] = @{ exists = $false }; continue }
        $item = Get-Item -LiteralPath $f -Force
        if ($item.PSIsContainer) {
            $out[$f] = @{ exists = $true; dir = $true; files = @(Get-ChildItem -LiteralPath $f -Recurse -File -Force |
                ForEach-Object { @{ path = $_.FullName; size = $_.Length } }) }
            continue
        }
        $info = @{ exists = $true; dir = $false; size = $item.Length; modified = Micro $item.LastWriteTime }
        if ($req.hash) {
            try { $info.sha256 = (Get-FileHash -LiteralPath $f -Algorithm SHA256).Hash.ToLower() }
            catch { $info.hash_error = $_.Exception.Message }
        }
        $out[$f] = $info
    }
    $out
}

function Op-Mkdir($req) {
    foreach ($d in @($req.paths)) {
        if (-not (Under-Root $d)) { throw "refusing to create $d outside $Root" }
        New-Item -ItemType Directory -Force -Path $d | Out-Null
    }
    @{ ok = $true }
}

function Op-Remove($req) {
    $removed = @()
    foreach ($d in @($req.paths)) {
        # Only what desk made: its own folder, never a path elsewhere.
        if (-not (Under-Root $d)) { throw "refusing to remove $d outside $Root" }
        if (Test-Path -LiteralPath $d) { Remove-Item -LiteralPath $d -Recurse -Force; $removed += $d }
    }
    @{ removed = $removed }
}

function Op-Exec($req) {
    Invoke-Tool $req.exe $req.args $req.timeout $req.cwd $req.encoding
}

function Op-Mark($req) {
    if (-not (Under-Root $req.path)) { throw "refusing to write a marker outside $Root" }
    New-Item -ItemType Directory -Force -Path ([IO.Path]::GetDirectoryName($req.path)) | Out-Null
    $at = NowMs
    # The file operation itself is the marker in a Process Monitor log (and in file I/O traces).
    [IO.File]::WriteAllText($req.path, $req.text, (New-Object Text.UTF8Encoding($false)))
    $out = @{ utc_ms = $at; path = $req.path; event = $false }
    if ($req.source) {
        # A self-describing ETW event of an EventSource whose provider GUID follows from its name,
        # so a trace session that enabled that GUID records it.
        try {
            $es = New-Object Diagnostics.Tracing.EventSource($req.source)
            try { $es.Write([string]$req.event) } finally { $es.Dispose() }
            $out.event = $true
        } catch { $out.event_error = $_.Exception.Message }
    }
    $out
}

function Op-Etw($req) {
    if (-not (Get-Command Get-EtwTraceSession -ErrorAction SilentlyContinue)) { return @{ available = $false } }
    $sessions = @{}
    foreach ($n in @($req.names)) {
        if (-not $n) { continue }
        $s = Get-EtwTraceSession -Name $n -ErrorAction SilentlyContinue
        if (-not $s) { $sessions[$n] = $null; continue }
        $props = @{}
        foreach ($p in $s.PSObject.Properties) {
            $v = $null
            try { $v = $p.Value } catch { continue }
            if ($v -is [ValueType] -or $v -is [string]) { $props[$p.Name] = $v }
        }
        $sessions[$n] = $props
    }
    @{ available = $true; sessions = $sessions; names = @(Get-EtwTraceSession -ErrorAction SilentlyContinue | ForEach-Object { $_.Name }) }
}

function Op-Ipc($req) {
    $out = @{}
    if ($req.pipes) {
        try {
            $out.pipes = @([IO.Directory]::GetFiles('\\.\pipe\') | ForEach-Object { $_.Substring(9) })
        } catch {
            # A pipe whose name is no valid path makes the .NET listing fail; this one lists it.
            $out.pipes = @(Get-ChildItem '\\.\pipe\' -ErrorAction SilentlyContinue | ForEach-Object { $_.Name })
            $out.pipes_note = $_.Exception.Message
        }
    }
    if ($req.tcp) {
        $out.tcp = @(Get-NetTCPConnection -ErrorAction SilentlyContinue | ForEach-Object {
            @{ local = $_.LocalAddress; lport = [int]$_.LocalPort; remote = $_.RemoteAddress; rport = [int]$_.RemotePort
               state = [string]$_.State; pid = [int]$_.OwningProcess }
        })
    }
    if ($req.udp) {
        $out.udp = @(Get-NetUDPEndpoint -ErrorAction SilentlyContinue | ForEach-Object {
            @{ local = $_.LocalAddress; lport = [int]$_.LocalPort; pid = [int]$_.OwningProcess }
        })
    }
    $out
}

function Op-PipeProbe($req) {
    Add-Type -Namespace DeskObserve -Name Pipe -MemberDefinition @'
[DllImport("kernel32.dll", SetLastError = true)] public static extern bool GetNamedPipeServerProcessId(IntPtr pipe, out uint pid);
'@ -ErrorAction SilentlyContinue
    $out = @{ connected = $false }
    $pipe = New-Object IO.Pipes.NamedPipeClientStream('.', $req.name, [IO.Pipes.PipeDirection]::InOut, [IO.Pipes.PipeOptions]::Asynchronous)
    try {
        $pipe.Connect([int]$req.timeout_ms)
        $out.connected = $true
        $serverPid = [uint32]0
        if ([DeskObserve.Pipe]::GetNamedPipeServerProcessId($pipe.SafePipeHandle.DangerousGetHandle(), [ref]$serverPid)) {
            $out.server_pid = [int]$serverPid
        }
        if ($req.send_b64) {
            $bytes = [Convert]::FromBase64String($req.send_b64)
            $pipe.Write($bytes, 0, $bytes.Length)
            $pipe.Flush()
            $out.sent = $bytes.Length
        }
        if ([int]$req.read_bytes -gt 0) {
            $buf = New-Object byte[] ([int]$req.read_bytes)
            $task = $pipe.ReadAsync($buf, 0, $buf.Length)
            if ($task.Wait([int]$req.timeout_ms)) {
                $out.received_b64 = [Convert]::ToBase64String($buf, 0, $task.Result)
            } else {
                $out.read_timed_out = $true
            }
        }
    } catch {
        $out.error = $_.Exception.Message
    } finally {
        $pipe.Dispose()
    }
    $out
}

function Op-Read($req) {
    if (-not (Under-Root $req.path)) { throw "refusing to read $($req.path) outside $Root" }
    if (-not (Test-Path -LiteralPath $req.path)) { return @{ exists = $false } }
    @{ exists = $true; text = [IO.File]::ReadAllText($req.path) }
}

# Whether captures are still what desk left: the boot, its processes, its watchdogs' words, the
# trace sessions there are. One round trip for every open session of a run.
function Op-State($req) {
    $out = @{ boot = (Boot); alive = @(); watch = @{} }
    $out.alive = @(foreach ($id in @($req.procs)) { if ($id) { @{ pid = [int]$id.pid; created = [long]$id.created; alive = (Test-Alive $id) } } })
    foreach ($f in @($req.watch)) {
        if (-not $f) { continue }
        $out.watch[$f] = $(if ((Under-Root $f) -and (Test-Path -LiteralPath $f)) { [IO.File]::ReadAllText($f) } else { $null })
    }
    if ($req.etw -and (Get-Command Get-EtwTraceSession -ErrorAction SilentlyContinue)) {
        $out.etw_sessions = @(Get-EtwTraceSession -ErrorAction SilentlyContinue | ForEach-Object { $_.Name })
    }
    $out
}

function Op-Listening($req) {
    @{ items = @(Get-NetTCPConnection -State Listen -LocalPort ([int]$req.port) -ErrorAction SilentlyContinue | ForEach-Object {
        @{ address = $_.LocalAddress; pid = [int]$_.OwningProcess }
    }) }
}

function Run-Watch($w) {
    $reason = $null
    # By a stopwatch: desk clock moves the guest's clock, not the capture's time.
    $sw = [Diagnostics.Stopwatch]::StartNew()
    while ($true) {
        if ($w.seconds -and $sw.Elapsed.TotalSeconds -ge [double]$w.seconds) { $reason = 'duration'; break }
        if ([long]$w.max_bytes -gt 0) {
            $sum = 0
            foreach ($f in @($w.files)) { if ($f) { $sum += Size-Of $f } }
            if ($sum -ge [long]$w.max_bytes) { $reason = 'size'; break }
        }
        if ($w.tool -and -not (Test-Alive $w.tool)) { $reason = 'tool-ended'; break }
        Start-Sleep -Seconds 2
    }
    $result = @{ state = 'stopping'; reason = $reason; at_ms = (NowMs) }
    Set-Content -LiteralPath $w.out -Value (ConvertTo-Json -InputObject $result -Depth 6 -Compress) -Encoding UTF8
    $runs = @()
    if ($reason -ne 'tool-ended') {
        foreach ($c in @($w.stop)) {
            if (-not $c) { continue }
            if ($c.alone) {
                # A command that ends every instance of a tool (procmon /Terminate) only while desk's
                # own are the only ones; otherwise desk's own are ended by their PID alone.
                $mine = @($c.alone.procs)
                $others = @(Get-CimInstance Win32_Process | Where-Object { @($c.alone.names) -contains $_.Name } |
                    Where-Object { $id = [int]$_.ProcessId; -not ($mine | Where-Object { [int]$_.pid -eq $id }) })
                if ($others) {
                    foreach ($m in $mine) { if (Test-Alive $m) { & taskkill.exe /F /PID ([int]$m.pid) 2>&1 | Out-Null } }
                    $runs += ,@{ skipped = "another $($others[0].Name) runs (pid $($others[0].ProcessId)); desk's own was ended by its PID" }
                    $result.forced = $true
                    continue
                }
            }
            $runs += ,(Invoke-Tool $c.exe $c.args $c.timeout $null $c.encoding)
        }
    }
    $result.state = 'done'
    $result.stop = $runs
    $result.done_ms = NowMs
    Set-Content -LiteralPath $w.out -Value (ConvertTo-Json -InputObject $result -Depth 6 -Compress) -Encoding UTF8
}

if ($Watch) { Run-Watch (Decode $Watch); return }
try {
    $req = Decode $Request
    $answer = switch ($req.op) {
        'probe' { Op-Probe $req }
        'launch' { Op-Launch $req }
        'alive' { Op-Alive $req }
        'stopproc' { Op-StopProc $req }
        'waitgone' { Op-WaitGone $req }
        'procs' { Op-Procs $req }
        'files' { Op-Files $req }
        'mkdir' { Op-Mkdir $req }
        'remove' { Op-Remove $req }
        'exec' { Op-Exec $req }
        'mark' { Op-Mark $req }
        'etw' { Op-Etw $req }
        'ipc' { Op-Ipc $req }
        'pipeprobe' { Op-PipeProbe $req }
        'listening' { Op-Listening $req }
        'boot' { @{ boot = (Boot) } }
        'read' { Op-Read $req }
        'state' { Op-State $req }
        default { throw "unknown operation $($req.op)" }
    }
    Reply @{ ok = $true; result = $answer; now_ms = (NowMs) }
} catch {
    Reply @{ ok = $false; error = "$($req.op): $($_.Exception.Message)"; now_ms = (NowMs) }
}
