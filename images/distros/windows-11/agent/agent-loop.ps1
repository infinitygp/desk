# Keeps the in-session agent running: started from the user's Run key (provision.ps1), it starts
# agent.ps1 and starts it again whenever it ends. Without it an agent that ended stays gone until
# the next logon, and every a11y query and exec --gui of the run fails. Each end goes to
# agent.log, which the report lists among the failures.
# -MTA: UI Automation clients belong in the multithreaded apartment; in PowerShell's default STA
# the MSAA proxies of classic Win32 windows (installers) return no children.
$log = Join-Path $PSScriptRoot 'agent.log'
while ($true) {
    & powershell.exe -MTA -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'agent.ps1')
    Add-Content $log ('{0:s} the in-session agent ended (exit {1}); started again' -f (Get-Date), $LASTEXITCODE)
    Start-Sleep -Seconds 2
}
