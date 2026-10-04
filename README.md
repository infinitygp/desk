# agent-visual-analysis

`desk`: disposable desktop VMs that an agent drives through screenshots and real
mouse/keyboard input, for testing desktop apps and system packages end to end.

- Nested KVM, libvirt `qemu:///session` (no root), passt networking.
- Out of band: screenshots through libvirt, pointer and keyboard through QMP, so it works
  regardless of what runs in the guest (login screen, Wayland, polkit, crashes).
- In band: SSH as the desktop user, qemu-guest-agent as root.
- Accessibility tree (AT-SPI) with refs that map to screen pixels; a GNOME Shell extension in
  the image supplies window positions, which AT-SPI on Wayland does not know.
- Golden image -> warm state (saved RAM of a logged-in desktop) -> per-run qcow2 overlay.
  `desk up` restores in seconds, `desk down` discards everything.

Agent instructions: [skill/SKILL.md](skill/SKILL.md), linked as `~/.agents/skills/desk` and
`~/.claude/skills/desk`; `bin/desk` is linked into `~/.local/bin`.

## Layout

```text
bin/desk             entry point (system python3 + python3-libvirt)
skill/SKILL.md       agent skill
desk/                harness code (guest_a11y.py runs inside the guest)
tests/               unit tests
images/<name>/       image.toml, kickstart, provision.sh
/var/lib/vmlab/      base/, warm/, runs/, reports/, maintain/, cache/, logs/, keys/  (DESK_HOME)
```

## Images

| Image | Install | Notes |
|---|---|---|
| `fedora-44-gnome` | kickstart over the network | default |
| `fedora-44-gnome-pl` | variant of `fedora-44-gnome` | Polish locale; shares the base disk, own warm state |
| `arch-gnome` | official cloud image, GNOME installed by cloud-init (NoCloud seed) | rolling; no Secure Boot |
| `windows-11` | evaluation ISO + autounattend.xml on a second CD | 90-day evaluation, reinstalled by `desk maintain` |

Windows specifics: no TPM (LabConfig bypass), virtio disk with the viostor driver loaded by Setup
(QEMU 10.2's AHCI emulation crashes under Windows), e1000e NIC (inbox driver),
OpenSSH from the Win32-OpenSSH MSI with PowerShell as the shell, virtio-win guest tools for the
display driver and qemu-guest-agent, and an agent in the logged-in session
(`images/_shared/windows/agent.ps1`, port forwarded like SSH) for UI Automation and starting
programs on the desktop, which SSH sessions cannot see. Commands are sent as
`powershell -EncodedCommand`, since Windows' command line mangles quotes.

```bash
bin/desk image build fedora-44-gnome   # unattended install, ~10 min, needs network
bin/desk image warm fedora-44-gnome    # boot, provision, save the running desktop
bin/desk image shot windows-11         # look at a graphical installer while it runs
bin/desk image list                    # state, build date, last selftest, licence, problems
```

Shared provisioning files live in `images/_shared/`. A variant (`base = "..."` in its
`image.toml`) inherits the base image's settings and adds provisioning scripts.

## Builds and maintenance

Every install and every warm state is a build of its own: `base/<image>.<version>/` and
`warm/<image>.<version>/`, with `base/<image>` and `warm/<image>` symlinks to the current ones.
Runs pin the builds they start from, so a new build never disturbs a running or kept run;
`desk gc` deletes builds nothing uses any more.

`desk maintain` reinstalls an image family (a base image and its variants) when the base is
`rebuild_days` old (Fedora and Arch: 14), when its install definition changed or when its licence
(`expiry`, Windows evaluation) has less than 14 days left, and re-warms images whose definition
changed. New builds are selftested next to the current ones and become current only when every
member of the family passes; otherwise the current builds stay, and `desk up` and
`desk image list` show the failure. Reports and screenshots of failures go to
`/var/lib/vmlab/maintain/<date>/` (an install that hangs: `logs/build-<image>-stuck.png`).
An install may take 3 hours before it counts as stuck; the whole run 12 hours.

Builds of images removed from `images/` are not deleted automatically.

```bash
bin/desk maintain --dry-run         # what is due
bin/desk maintain --install-timer   # weekly, Monday 03:00 (systemd user timer, needs lingering)
systemctl --user list-timers desk-maintain.timer
journalctl --user -u desk-maintain  # output of the last runs
```

`desk up` warns three weeks before a licence runs out and refuses an expired image.

## Watching

- `desk view`: every run has a VNC display on 127.0.0.1 with a per-run password; the command
  prints the SSH tunnel to use from a workstation.

## Evidence

`desk down` writes `reports/<run>/report.md` (failed units, SELinux denials, coredumps, journal
warnings, package changes, timeline with screenshots); `--diff` adds the files the run changed,
from `virt-diff` against the image the run started from. Reports are pruned after 14 days.

`desk record start|stop` (or `desk up --record`) takes screenshots at a steady rate (4 fps) in a
background process; unchanged frames are hard links, so a still screen costs nothing. Stopping
encodes them with GStreamer to WebM (VP9) with the wall-clock time in a bar below the picture,
and `desk down` copies the videos next to the report.

Provisioning (`provision.sh`, `files/`) runs before the warm state is saved and after cold
boots; it installs the shell extension, the Polish (programmer's) layout and a11y settings.

## Checks

```bash
/usr/bin/python3 -m unittest discover -s tests -t .   # unit tests, no VM
bin/desk selftest fedora-44-gnome --reboot            # end to end in a real run, ~1 min
```

## Host requirements

Fedora with `/dev/kvm`, `libvirt-daemon-driver-qemu`, `libvirt-client`, `python3-libvirt`,
`passt`, `virt-install`, `edk2-ovmf`, `guestfs-tools`, `gstreamer1-plugins-good` (videos); the
user in the `kvm` group.
`/var/lib/vmlab` is a dedicated XFS disk labelled `svirt_home_t`.
