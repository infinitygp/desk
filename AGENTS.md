# Working on desk

`desk` runs disposable VMs that an agent drives like a person would. `README.md` explains how it
is built and why; `skill/SKILL.md` is what the agents that *use* desk read. This file is for
changing desk itself.

## What must stay true

- **No green without a look.** A command that could not ask the guest, reach the host tool or
  finish in time says so and fails. It never prints a count of zero, "done" or "showing ..." for
  something it did not see. A new check goes with a test for the case where it cannot check.
- **Exit codes.** 1: the check a command stands for failed (a scenario step, `selftest`, `find`,
  `wait`, `browser open`, `doctor`). 2: desk could not do what was asked; raise `DeskError` with
  one line that says what failed and what to do next. Agents read 1 as a finding about the
  product under test, so nothing else may exit 1.
- **Every wait has a limit**, and what a guest command may take is enforced in the guest
  (`guest.command`), not only by closing SSH.
- **Nothing of one machine in the repository.** Where files are kept, which libvirt is used and
  which images exist is the host's business (`~/.config/desk/config.toml`, `DESK_HOME`). Hosts
  are Arch Linux and Fedora and what is built on them: a host requirement gets its package name
  for both in `desk/doctor.py` (checked with `pacman -Qo` and `dnf repoquery --whatprovides`) and
  in the README's table.
- **Nothing of the projects that use desk.** Their apps, emulators, rigs and scenarios stay out of
  code, tests, docs and commit messages; a feature is described for what it does, and examples
  use neutral names.
- **Optional stays optional.** Images, warm states and the maintenance timer exist where
  somebody asked for them. No command builds, warms or schedules anything unasked, and their
  absence is not an error.
- **The state lock is for starting and destroying domains only.** Booting, reports, videos and
  anything else slow happen outside it (`runs.up`, `evidence.finish`, `runs.gc_idle`).
- A behaviour that agents rely on is described in `skill/SKILL.md`; change both together. Design
  and reasons go into `README.md`.

## Checks

```bash
/usr/bin/python3 -m unittest discover -s tests -t .   # no VM, a second; uses a home of its own
bin/desk doctor                                       # what this host has
bin/desk selftest fedora-44-gnome --reboot            # the harness end to end, ~1 min
bin/desk selftest windows-11 --reboot                 # the Windows paths (agent, UI Automation), 3 min of calm
```

Run the unit tests after every change, and the selftest of an image of each kind the change
touches (`desk image list` shows which are built here; `desk maintain --only NAME` builds, warms
and tests one that is missing, 10-25 minutes). Unit tests cover what can be decided without a
guest; whatever talks to one is tried in one before it is committed:

- A change to a command: start a run (`bin/desk up IMAGE --label "desk check"`), use the command
  for what it is for and for what it should refuse, take the run down.
- Failure paths count: make the thing fail (a fake `gst-launch-1.0` first in `PATH`, a second
  process holding a lock, `DESK_TTL=5 bin/desk gc`) and look at what is left afterwards
  (`bin/desk ls`, `virsh -c qemu:///session list`).
- A change to the scenario format: the scenario files that projects already have (their
  `test/desk/*.toml`) must still pass `bin/desk scenario check`.
- The usbvfiod controller (`desk/usbvfiod.py`, `hotplug.py`, `hubs.py`): the unit tests talk to a
  fake server, so try the real one in a guest too: `DESK_USBVFIOD=PATH bin/desk up windows-11
  --usbvfiod --name uv`, the controller and its root hub in `Get-PnpDevice` (status OK, problem
  code 0); with the fork's `remote` on the socket (`remote --socket "$(bin/desk -r uv usb socket)"
  --hub 5 --ports 4 --speed high --vid 0409 --pid 55aa`, then `--virtual-hid 5.1 --speed full`, or
  `--external` with its
  `examples/external_hid.rs`), the hub and the device in `Get-PnpDevice` with their
  `DEVPKEY_Device_LocationPaths`; `bin/desk down uv`, and `pgrep -x usbvfiod` shows nothing. A host
  device only when the user named one: the host's controller often serves its keyboard and mouse
  too.

### Blocks (`images/`)

- Images are made of blocks (`images/distros/`, `desktops/`, `addons/`), and a host has the
  images it defined (`desk image list`). After a change to a block run `bin/desk maintain --only
  NAME` for an image of each kind that uses it: it reinstalls or re-warms what the change touches,
  selftests the new build and switches to it only then. `desk image build --force` switches
  without testing.
- A desktop goes into the `desktops` list of the distro's version it was tried on (its
  `[versions.N]` table in the distro's `block.toml`; the block's own list only when every version
  without a list of its own was tried with it), an addon's distro into its `distros` (the block's
  name, `fedora`, only when it was tried on every version), once an image of the two was built and
  passed its selftest. A new version of a distro goes into its `[versions]` the same way. An image
  whose blocks are new to this host is added for the check (`desk image add`) and removed with its
  builds afterwards unless the user asked for it.
- `bin/desk image shot NAME` shows the screen of an install that is running; a graphical
  installer that waits for an answer is the usual reason an install hangs.
- A build takes long enough for the code to change under it: run it from a copy of the commit,
  `git archive HEAD | tar -x -C ~/.cache/desk-check/tree`, then `~/.cache/desk-check/tree/bin/desk ...`,
  and remove the copy afterwards.

### The Windows agent (`images/distros/windows-11/agent/`)

It is copied into the guest at every warm-up, so it needs no reinstall. To try a change:

```bash
bin/desk up windows-11 --name wagent
bin/desk -r wagent push images/distros/windows-11/agent/agent.ps1 C:/desk/agent.ps1   # uia.ps1 likewise
bin/desk -r wagent reboot                                                    # logs in again, starts it
```

It serves one request at a time and compiles its C# at logon (Windows PowerShell 5.1, C# 5): a
construct that does not compile leaves the guest without an agent. Try a long
`exec --gui --wait` with `desk a11y` next to it before `desk maintain --only windows-11`.

### A host of the other kind

A host is one of Arch or Fedora; the other is tried in a guest, with desk at its defaults
there (SELinux enforcing on Fedora):

```bash
bin/desk up fedora-44-gnome --name host
git ls-files -co --exclude-standard | tar -czf ~/.cache/desk-tree.tgz -T -
bin/desk -r host push ~/.cache/desk-tree.tgz /tmp/
bin/desk -r host exec -- 'mkdir -p ~/desk && tar -xzf /tmp/desk-tree.tgz -C ~/desk && ~/desk/bin/desk doctor'
# install what doctor names (desk exec --root -- dnf install -y ...), then the smallest image:
bin/desk -r host exec --timeout 3000 -- 'cd ~/desk && bin/desk image add openwrt-25.12 && bin/desk maintain --only openwrt-25.12'
```

## Releases

A release is the single file `desk.pyz`; hosts that only use desk download it and update it with
`desk update` (README, "Installing"). The chain, run whole when a release is asked for:

1. Choose the version from what changed since the last tag (`git log $(git describe --tags --abbrev=0)..`):
   `vX.Y.Z`, minor for new commands or behaviour, patch for fixes. The tag is the only place the
   version lives.
2. On this host: the unit tests, then the build as the workflow runs it,
   `/usr/bin/python3 scripts/build.py --version vX.Y.Z --output ~/.cache/desk-release/desk.pyz`,
   and `~/.cache/desk-release/desk.pyz --version`.
3. Try that file where no checkout is: push it into a run of `fedora-44-gnome`, put it at
   `~/.local/bin/desk`, run `desk install` and `desk doctor` there (the procedure of "A host of
   the other kind", with the file instead of the tree).
4. `git tag vX.Y.Z && git push origin main vX.Y.Z`; the tag starts `.github/workflows/release.yml`
   (Fedora 44 container: unit tests, build, `gh release create`). Watch it:
   `gh run watch --repo infinitygp/desk $(gh run list --repo infinitygp/desk --workflow release.yml --limit 1 --json databaseId --jq '.[0].databaseId')`.
5. Check the release: `gh release view vX.Y.Z --repo infinitygp/desk` lists `desk.pyz`, and
   `desk update` of an installed release (a copy in `~/.cache/desk-release/`, run with its own
   `HOME`) reaches the new version. Remove `~/.cache/desk-release` afterwards.

## Working next to others

- Runs of other agents and of the user may exist on the same host. Touch only runs you started
  (`bin/desk ls` shows who holds each); never `desk down --all`, never `virsh destroy` a domain
  you did not create.
- Take down what you started, and delete the reports of your own check runs
  (`<home>/reports/<run>`) when they hold nothing worth keeping.

## Commits

One per fix, a single line `type: what is different now` in the words of the README, no body.
The tests of a fix go into its commit; run the suite before committing, not after.
