# desk: OpenWrt packages and LuCI apps

Part of the desk skill ([SKILL.md](SKILL.md)); the commands and pitfalls there apply.

## OpenWrt packages and LuCI apps

`openwrt-25.12` is the OpenWrt 25.12 release image for x86-64 (apk, BusyBox ash, procd, LuCI), as
root (password `root`), without a desktop: the screen is the text console (`desk shot` shows boot
messages and kernel panics, `desk key`/`type` reach it), and a11y, clicks by name, `desk browser`
and `exec --gui` refuse. Build the package with the OpenWrt SDK for x86-64 of the same release on
the host (or in CI), not in the guest; packages built that way are unsigned:

```bash
desk up openwrt-25.12                         # ~1 s from the warm state
desk push bin/packages/x86_64/base/foo-1.0-r1.apk /tmp/
desk exec -- 'apk update && apk add --allow-untrusted /tmp/foo-1.0-r1.apk'   # dependencies from the feeds
desk exec -- '/etc/init.d/foo status; logread -e foo; uci show foo'
desk forward 80                               # LuCI: http://127.0.0.1:PORT/, log in as root / root
```

- Commands run in ash as root: no bash, no systemd. Services: `/etc/init.d/foo
  start|stop|restart|status|enabled`, `ubus call service list`; logs: `logread`, kept across
  reboots in `/desk/syslog`; configuration: `uci show|set|commit`.
- In a scenario: `image = "openwrt-25.12"`, `[build] artifacts =
  ["bin/packages/x86_64/*/luci-app-foo-*.apk"]` (copied to `/tmp/desk/`), then
  `root = "apk update && apk add --allow-untrusted /tmp/desk/luci-app-foo-*.apk"` and
  `root = "/etc/init.d/foo running"` with `until = true`.
- LuCI pages are tested with a browser on the host (agent-browser) at the address `desk forward 80`
  prints. A page that embeds another guest port (luci-app-ttyd's terminal on 7681) needs that port
  forwarded too, under the same number (`desk forward 7681`, which takes it when it is free);
  forwards lead to the router's LAN address, where such services listen.
- The report lists services procd keeps that are not running, crash loops (before reboots too),
  warnings and errors from the log, segfaults, and packages installed or removed since the warm
  state. `desk clock` works (it stops sysntpd until `desk clock reset`).
