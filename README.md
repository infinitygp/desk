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
/var/lib/vmlab/      base/, warm/, runs/, reports/, cache/, logs/, keys/  (DESK_HOME)
```

## Images

| Image | Install | Notes |
|---|---|---|
| `fedora-44-gnome` | kickstart over the network | default |
| `fedora-44-gnome-pl` | variant of `fedora-44-gnome` | Polish locale; shares the base disk, own warm state |
| `ubuntu-26.04-gnome` | desktop ISO autoinstall (cloud-init NoCloud seed) | apt/snap timers off |

```bash
bin/desk image build fedora-44-gnome   # unattended install, ~10 min, needs network
bin/desk image warm fedora-44-gnome    # boot, provision, save the running desktop
bin/desk image shot ubuntu-26.04-gnome # look at a graphical installer while it runs
```

Shared provisioning files live in `images/_shared/`. A variant (`base = "..."` in its
`image.toml`) inherits the base image's settings and adds provisioning scripts.

## Evidence

`desk down` writes `reports/<run>/report.md` (failed units, SELinux denials, coredumps, journal
warnings, package changes, timeline with screenshots); `--diff` adds the files the run changed,
from `virt-diff` against the image the run started from. Reports are pruned after 14 days.

Provisioning (`provision.sh`, `files/`) runs before the warm state is saved and after cold
boots; it installs the shell extension, the Polish (programmer's) layout and a11y settings.

## Checks

```bash
/usr/bin/python3 -m unittest discover -s tests -t .   # unit tests, no VM
bin/desk selftest fedora-44-gnome --reboot            # end to end in a real run, ~1 min
```

## Host requirements

Fedora with `/dev/kvm`, `libvirt-daemon-driver-qemu`, `libvirt-client`, `python3-libvirt`,
`passt`, `virt-install`, `edk2-ovmf`, `guestfs-tools`; the user in the `kvm` group.
`/var/lib/vmlab` is a dedicated XFS disk labelled `svirt_home_t`.
