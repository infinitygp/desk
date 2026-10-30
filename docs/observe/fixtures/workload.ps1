# desk observe validation workload (Windows PowerShell 5.1). Does a fixed set of things, each
# carrying one token, and writes a manifest of what it did and when (UTC ms), so expect.py on the
# host can look for exactly those events in a capture rather than for the capture's mere existence.
#
#   desk -r RUN push docs/observe/fixtures/workload.ps1 C:/desk/workload.ps1
#   desk -r RUN exec -- powershell -NoProfile -ExecutionPolicy Bypass -File C:\desk\workload.ps1 -Token ab12cd34
#   desk -r RUN pull C:/desk/observe/workload/ab12cd34/manifest.json ./manifest.json
#
# -Steps picks steps (default all but crash); -HoldSeconds keeps a pipe server and a TCP listener
# open at the end, so IPC listings taken meanwhile (desk observe mark) see them.
param(
    [Parameter(Mandatory = $true)][ValidatePattern('^[a-z0-9]{4,16}$')][string]$Token,
    [string[]]$Steps = @('process', 'files', 'registry', 'pipe', 'tcp', 'dns', 'cpu', 'io'),
    [int]$HoldSeconds = 0,
    [string]$Out = 'C:\desk\observe\workload'
)
$ErrorActionPreference = 'Stop'
# powershell -File passes "-Steps cpu,io" as one string.
$Steps = @($Steps | ForEach-Object { $_ -split ',' } | Where-Object { $_ })
$dir = Join-Path $Out $Token
New-Item -ItemType Directory -Force -Path $dir | Out-Null
$manifest = [ordered]@{ token = $Token; pid = $PID; started_ms = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds(); steps = [ordered]@{} }
function Now { [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() }

foreach ($step in $Steps) {
    $t0 = Now
    $r = [ordered]@{}
    switch ($step) {
        'process' {
            # A child that exits with a known code, and one that is ended from outside.
            $p = Start-Process cmd.exe -ArgumentList "/c exit 7" -PassThru -WindowStyle Hidden -Wait
            $r.exit_child = @{ pid = $p.Id; image = 'cmd.exe'; exit_code = $p.ExitCode }
            $k = Start-Process ping.exe -ArgumentList '-n 30 127.0.0.1' -PassThru -WindowStyle Hidden
            Start-Sleep -Milliseconds 500
            Stop-Process -Id $k.Id -Force
            $k.WaitForExit()
            $r.killed_child = @{ pid = $k.Id; image = 'PING.EXE' }
        }
        'files' {
            $a = Join-Path $dir "$Token-a.txt"; $b = Join-Path $dir "$Token-b.txt"
            [IO.File]::WriteAllText($a, "desk fixture $Token")
            Rename-Item -LiteralPath $a -NewName (Split-Path $b -Leaf)
            Remove-Item -LiteralPath $b
            $r.created = $a; $r.renamed_to = $b; $r.deleted = $b
        }
        'registry' {
            $key = "HKCU:\Software\DeskObserveFixture\$Token"
            New-Item -Path $key -Force | Out-Null
            Set-ItemProperty -Path $key -Name "value-$Token" -Value 42 -Type DWord
            Remove-ItemProperty -Path $key -Name "value-$Token"
            Remove-Item -Path $key
            $r.key = "HKCU\Software\DeskObserveFixture\$Token"; $r.value = "value-$Token"
        }
        'pipe' {
            # A server in its own process, a client here: one message each way.
            $name = "desk-fixture-$Token"
            $server = Start-Process powershell.exe -PassThru -WindowStyle Hidden -ArgumentList @('-NoProfile', '-Command',
                "`$s = New-Object IO.Pipes.NamedPipeServerStream('$name'); `$s.WaitForConnection(); `$b = New-Object byte[] 64; " +
                "`$n = `$s.Read(`$b, 0, 64); `$reply = [Text.Encoding]::ASCII.GetBytes('pong-$Token'); `$s.Write(`$reply, 0, `$reply.Length); " +
                "`$s.Flush(); `$s.WaitForPipeDrain(); `$s.Dispose()")
            $c = New-Object IO.Pipes.NamedPipeClientStream('.', $name, [IO.Pipes.PipeDirection]::InOut)
            $c.Connect(15000)
            $msg = [Text.Encoding]::ASCII.GetBytes("ping-$Token")
            $c.Write($msg, 0, $msg.Length); $c.Flush()
            $buf = New-Object byte[] 64
            $n = $c.Read($buf, 0, 64)
            $c.Dispose()
            $server.WaitForExit(15000) | Out-Null
            $r.pipe = $name; $r.server_pid = $server.Id; $r.reply = [Text.Encoding]::ASCII.GetString($buf, 0, $n)
        }
        'tcp' {
            $l = New-Object Net.Sockets.TcpListener([Net.IPAddress]::Loopback, 0)
            $l.Start()
            $port = $l.LocalEndpoint.Port
            $accept = $l.AcceptTcpClientAsync()
            $c = New-Object Net.Sockets.TcpClient('127.0.0.1', $port)
            $srv = $accept.Result
            $msg = [Text.Encoding]::ASCII.GetBytes("tcp-$Token")
            $c.GetStream().Write($msg, 0, $msg.Length)
            $buf = New-Object byte[] 64
            $n = $srv.GetStream().Read($buf, 0, 64)
            $c.Close(); $srv.Close(); $l.Stop()
            $r.port = $port; $r.received = [Text.Encoding]::ASCII.GetString($buf, 0, $n)
        }
        'dns' {
            # A query that leaves through the network adapter (loopback does not), with the token in its name.
            $q = "$Token.desk-observe.invalid"
            try { [Net.Dns]::GetHostAddresses($q) | Out-Null; $r.resolved = $true } catch { $r.resolved = $false }
            $r.query = $q
        }
        'cpu' {
            # Deterministic: the same digest on every machine, for timing comparisons between captures.
            $sha = [Security.Cryptography.SHA256]::Create()
            $h = [Text.Encoding]::ASCII.GetBytes($Token)
            for ($i = 0; $i -lt 200000; $i++) { $h = $sha.ComputeHash($h) }
            $r.iterations = 200000; $r.digest = ([BitConverter]::ToString($h) -replace '-', '').ToLower()
        }
        'io' {
            $f = Join-Path $dir "$Token-io.bin"
            $chunk = New-Object byte[] (1MB)
            (New-Object Random 42).NextBytes($chunk)
            $s = [IO.File]::Open($f, 'Create')
            for ($i = 0; $i -lt 32; $i++) { $s.Write($chunk, 0, $chunk.Length) }
            $s.Flush($true); $s.Dispose()
            $read = [IO.File]::ReadAllBytes($f).Length
            Remove-Item -LiteralPath $f
            $r.file = $f; $r.bytes_written = 32MB; $r.bytes_read = $read
        }
        'crash' {
            # An unhandled failure Windows reports (Application Error, WER): FailFast in a child.
            $p = Start-Process powershell.exe -PassThru -WindowStyle Hidden -Wait -ArgumentList @('-NoProfile', '-Command',
                "[Environment]::FailFast('desk-fixture-$Token')")
            $r.pid = $p.Id; $r.exit_code = $p.ExitCode; $r.exit_hex = ('0x{0:X8}' -f $p.ExitCode)
        }
        default { throw "unknown step $step" }
    }
    $r.start_ms = $t0; $r.end_ms = Now
    $manifest.steps[$step] = $r
}

if ($HoldSeconds -gt 0) {
    $name = "desk-fixture-hold-$Token"
    $pipe = New-Object IO.Pipes.NamedPipeServerStream($name, [IO.Pipes.PipeDirection]::InOut, 1,
        [IO.Pipes.PipeTransmissionMode]::Byte, [IO.Pipes.PipeOptions]::Asynchronous)
    $l = New-Object Net.Sockets.TcpListener([Net.IPAddress]::Loopback, 0)
    $l.Start()
    $manifest.hold = @{ pipe = $name; port = $l.LocalEndpoint.Port; pid = $PID; from_ms = (Now); seconds = $HoldSeconds }
    $manifest | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $dir 'manifest.json') -Encoding UTF8
    Start-Sleep -Seconds $HoldSeconds
    $l.Stop(); $pipe.Dispose()
}
$manifest.ended_ms = Now
$manifest | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $dir 'manifest.json') -Encoding UTF8
"workload $Token done: $(Join-Path $dir 'manifest.json')"
