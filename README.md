# agent-visual-analysis

`desk`: disposable desktop VMs that an agent drives through screenshots and real
mouse/keyboard input, for testing desktop apps and system packages end to end.

- Nested KVM, libvirt `qemu:///session` (no root), passt networking.
- Out of band: screenshots through libvirt, pointer and keyboard through QMP, so it works
  regardless of what runs in the guest (login screen, Wayland, polkit, crashes).
- In band: SSH as the desktop user, qemu-guest-agent as root.
- Golden image -> warm state (saved RAM of a logged-in desktop) -> per-run qcow2 overlay.
  `desk up` restores in seconds, `desk down` discards everything.

Agent instructions: [skill/SKILL.md](skill/SKILL.md), linked as `~/.agents/skills/desk` and
`~/.claude/skills/desk`; `bin/desk` is linked into `~/.local/bin`.

## Layout

```text
bin/desk             entry point (system python3 + python3-libvirt)
skill/SKILL.md       agent skill
desk/                harness code
images/<name>/       image.toml, kickstart, provision.sh
/var/lib/vmlab/      base/, warm/, runs/, cache/, logs/, keys/  (DESK_HOME)
```

## Images

```bash
bin/desk image build fedora-44-gnome   # unattended install, ~10 min, needs network
bin/desk image warm fedora-44-gnome    # boot, provision, save the running desktop
```

## Host requirements

Fedora with `/dev/kvm`, `libvirt-daemon-driver-qemu`, `libvirt-client`, `python3-libvirt`,
`passt`, `virt-install`, `edk2-ovmf`, `guestfs-tools`; the user in the `kvm` group.
`/var/lib/vmlab` is a dedicated XFS disk labelled `svirt_home_t`.
