# desk: observing what a Windows program does

Part of the desk skill ([SKILL.md](SKILL.md)). `desk observe` records, inside a Windows guest and
with Windows' own or the user's tools, what a program does that the screen does not show: files,
registry and processes (Process Monitor), ETW events (WPR, logman), packets (pktmon, netsh trace),
USB traffic (USBPcap), named pipes and sockets with their processes, and it serves debugger servers
to the host. Every capture is a session of the run; `desk observe stop` copies its files to the
host, compares them with the guest's by SHA-256 and reads them as their format, and the report
lists them. desk never claims more than it checked: a session is "complete" only when every file
it needs was copied whole and read.

```bash
desk observe providers -v                  # what this run's guest can use, the options, what each misses
desk observe start procmon --process app.exe --duration 300
desk exec --gui -- C:/Users/tester/Downloads/app.exe
desk observe mark "clicked Save" --shot    # this moment, in the capture, the timeline and a screenshot
desk observe stop                          # stop, copy, check: prints each file and what was checked
desk down                                  # the report has an Observation section and observe.json
```

| Provider | What it records | Needs |
|---|---|---|
| `procmon` | file system, registry, processes and threads of the whole guest, as PML (`--set csv=true` also CSV) | the user's copy: `desk observe setup procmon ProcessMonitor.zip --accept-eula` (only when the user accepted the Sysinternals licence terms; ask) |
| `wpr` | ETW by WPR profile (`--set profile=GeneralProfile,CPU.Light`), one ETL | Windows |
| `etw` | ETW of the providers named (`--set providers=Microsoft-Windows-Kernel-Process:0x10:4`), one ETL, desk's marks as events | Windows |
| `pktmon` | packets through the network adapters, ETL and PCAPNG; 128 bytes of each packet unless `--set pkt_size=0` | Windows 10 1809+ |
| `netsh` | packets with netsh's capture filters (`--set filters=IPv4.Address=10.0.2.2`) | Windows |
| `ipc` | named pipes, TCP and UDP endpoints with their processes, listed at start, every mark and stop | Windows |
| `usbpcap` | USB packets of one root hub, pcap | USBPcap installed in the guest (driver, reboot); a device handed over with `desk usb add` |
| `dbgsrv`, `ida` | a debugger server in the guest, forwarded to 127.0.0.1 on the host; desk debugs nothing | Debugging Tools for Windows in the guest; IDA's server set up from the user's IDA |

- Start before the program does what matters: a capture sees nothing from before its start.
  `--process app.exe` (or `--pid N`) follows that program and its children by PID and creation
  time; the report lists them, so traces can be filtered by the right PIDs.
- Every capture stops itself (30 min, 4 GiB unless `--duration`/`--max-mb`), also when desk is
  gone; `desk down` stops and collects whatever is still open. One ProcMon, one packet capture
  and one WPR at a time per run.
- desk stops only what it started. A ProcMon or packet capture already running in the guest
  makes `start` refuse (exit 2): close it, or report it; never stop it by hand unless you started it.
- `desk observe status` while it runs (file size, lost events); `desk observe list` the sessions.
- A reboot or a snapshot restore ends or rewinds captures: the session says "interrupted" or
  that what was recorded after the snapshot's moment is gone; `stop` collects what is left.
- Exit 2 from any `desk observe` command, or a scenario step that observes, is desk's failure:
  the tool was missing, refused, or its file is not whole. It is no finding about the product;
  say what failed and that the capture is missing.
- Network captures hold packet metadata and only the first bytes of payload; TLS stays
  encrypted, loopback traffic does not pass the adapter. IPC listings show endpoints, never the
  data sent through them; `desk observe pipe NAME --send TEXT --read 64` connects to one pipe as a
  client, which the server sees like any client: use it only on the product's own pipes.
- Open the files with the user's tools (ProcMon for PML, WPA for ETL, Wireshark for PCAPNG and
  USBPcap's pcap). You cannot read them yourself beyond what the report says of them.

In a scenario:

```toml
[[observe]]
provider = "procmon"
label = "app"              # steps name it; default: the provider's name
process = "app.exe"
duration = 600
options = {csv = true}     # the provider's options, as --set takes them
# start = "step"           # instead of before the first step: at an observe_start step
[[step]]
mark = "before saving"     # in every running session
[[step]]
observe_stop = "app"       # or true: every open session; the run's end stops the others
```

`observe_start = "LABEL"` starts a `start = "step"` session. `desk scenario check` checks the
providers and their options without a guest.
