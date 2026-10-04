# desk in-session agent. Started from the user's Run key, so it lives in the interactive desktop
# session that SSH cannot reach: it answers UI Automation queries and starts programs there.
# One JSON request per TCP connection, one JSON reply. The port is forwarded by the host only.
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
. (Join-Path $PSScriptRoot 'uia.ps1')
Add-Type -Namespace Desk -Name Win -MemberDefinition @'
[DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
[DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int cmd);
[DllImport("user32.dll")] public static extern void keybd_event(byte vk, byte scan, uint flags, UIntPtr extra);
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
        if ($new) {
            [Desk.Win]::keybd_event(0x87, 0, 0, [UIntPtr]::Zero)
            [Desk.Win]::keybd_event(0x87, 0, 2, [UIntPtr]::Zero)
            [void][Desk.Win]::ShowWindow([IntPtr][long]$new, 9)
            return [Desk.Win]::SetForegroundWindow([IntPtr][long]$new)
        }
        Start-Sleep -Milliseconds 250
    }
    $false
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
                $before = Get-TopHandles
                $p = if ($req.args) { Start-Process -FilePath $req.file -ArgumentList $req.args -PassThru }
                     else { Start-Process -FilePath $req.file -PassThru }
                @{ ok = $true; pid = $p.Id; focused = (Focus-NewWindow $before $p.Id) }
            }
            'uia' { Invoke-Uia $req.op $req.arg $req.text }
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
