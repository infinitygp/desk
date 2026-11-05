import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from desk import cli, doctor, hubs, runs, usb, usbvfiod
from desk.config import DeskError
from tests.usbvfiod_fake import FakeController, FakeHotplug

FAKE_SERVER = textwrap.dedent("""\
    import os, signal, socket, sys, time
    args = sys.argv[1:]
    mode = os.environ.get("FAKE_USBVFIOD", "")
    print("fake usbvfiod", *args, flush=True)
    if mode == "die":
        print("ERROR no luck", file=sys.stderr, flush=True)
        sys.exit(3)
    if mode == "stubborn":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    held = []
    if mode != "silent":
        for name in ("--socket-path", "--hotplug-socket-path"):
            s = socket.socket(socket.AF_UNIX)
            s.bind(args[args.index(name) + 1])
            s.listen(1)
            held.append(s)
    time.sleep(60)
""")

DECK = {"id": "0fd9:008f", "bus": 5, "dev": 12, "name": "Stream Deck XL", "node": "", "access": True,
        "port": "5-1", "serial": "S"}


class InRuns(unittest.TestCase):
    """A run in a directory of its own, and the pieces desk keeps for it."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        for target, value in (("desk.runs.RUNS", self.tmp / "runs"), ("desk.runs.HOME", self.tmp)):
            patch = mock.patch(target, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.run = self.make("r1")

    def make(self, run_id, **kw) -> runs.Run:
        run = runs.Run(id=run_id, image="windows-11", domain=f"desk-{run_id}", ssh_port=1, user="tester",
                       created=1.0, **kw)
        run.dir.mkdir(parents=True)
        run.save()
        return run

    def fake_binary(self, mode: str = "") -> Path:
        script = self.tmp / "fake-usbvfiod"
        script.write_text(f"#!{sys.executable}\n{FAKE_SERVER}")
        script.chmod(0o755)
        env = mock.patch.dict(os.environ, {usbvfiod.ENV: str(script), "FAKE_USBVFIOD": mode})
        env.start()
        self.addCleanup(env.stop)
        return script


class Binary(unittest.TestCase):
    def test_the_environment_wins_over_the_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / "mine"
            exe.write_text("#!/bin/sh\n")
            exe.chmod(0o755)
            with mock.patch.dict(os.environ, {usbvfiod.ENV: str(exe)}), \
                    mock.patch("shutil.which", return_value="/usr/bin/usbvfiod"):
                self.assertEqual(usbvfiod.binary(), str(exe))

    def test_the_path_is_the_default(self):
        with mock.patch.dict(os.environ, {usbvfiod.ENV: ""}), mock.patch("shutil.which", return_value="/x/usbvfiod"):
            self.assertEqual(usbvfiod.binary(), "/x/usbvfiod")

    def test_a_missing_binary_says_what_to_do(self):
        with mock.patch.dict(os.environ, {usbvfiod.ENV: ""}), mock.patch("shutil.which", return_value=None), \
                self.assertRaisesRegex(DeskError, r"usbvfiod is not installed: install a release of the private fork: "
                                                  r"`gh release download --repo infinitygp/usbvfiod --pattern "
                                                  r"'usbvfiod-\*\.tar\.gz'`, unpack it and run its install\.sh.*"
                                                  r"cargo install --locked --path CHECKOUT-OF-USBVFIOD.*\$DESK_USBVFIOD"):
            usbvfiod.binary()

    def test_a_named_file_that_cannot_run_is_not_replaced_by_the_one_in_path(self):
        with mock.patch.dict(os.environ, {usbvfiod.ENV: "/nonexistent/usbvfiod"}), \
                mock.patch("shutil.which", return_value="/usr/bin/usbvfiod"), \
                self.assertRaisesRegex(DeskError, r"\$DESK_USBVFIOD is '/nonexistent/usbvfiod', which is not an "
                                                  r"executable file; install a release of the private fork: "
                                                  r"`gh release download --repo infinitygp/usbvfiod"):
            usbvfiod.binary()

    def test_up_needs_a_qemu_with_vfio_user_pci(self):
        doctor._qemu_devices.cache_clear()
        self.addCleanup(doctor._qemu_devices.cache_clear)
        with mock.patch.object(usbvfiod, "binary", return_value="/x/usbvfiod"), \
                mock.patch.object(doctor, "_qemu_devices", return_value='name "qemu-xhci", bus PCI\n'):
            with self.assertRaisesRegex(DeskError, "no vfio-user-pci.*QEMU 10.1"):
                usbvfiod.check()
        with mock.patch.object(usbvfiod, "binary", return_value="/x/usbvfiod"), \
                mock.patch.object(doctor, "_qemu_devices", return_value='name "vfio-user-pci", bus PCI\n'):
            self.assertEqual(usbvfiod.check(), "/x/usbvfiod")

    def test_doctor_says_what_there_is_and_never_fails_for_it(self):
        with mock.patch.object(usbvfiod, "binary", side_effect=DeskError("usbvfiod is not installed: build it")):
            self.assertEqual(doctor.usbvfiod_line(), "usbvfiod is not installed: build it")
        with mock.patch.object(usbvfiod, "binary", return_value="/x/usbvfiod"), \
                mock.patch("subprocess.run", return_value=SimpleNamespace(stdout="usbvfiod 0.4.0\n")), \
                mock.patch.object(doctor, "_qemu_devices", return_value='name "vfio-user-pci", bus PCI\n'):
            self.assertEqual(doctor.usbvfiod_line(),
                             "/x/usbvfiod (usbvfiod 0.4.0); QEMU vfio-user-pci: yes")
        with mock.patch.object(usbvfiod, "binary", return_value="/x/usbvfiod"), \
                mock.patch("subprocess.run", side_effect=OSError), \
                mock.patch.object(doctor, "_qemu_devices", return_value=""):
            self.assertIn("QEMU vfio-user-pci: NO, needs QEMU 10.1 or later; desk up --usbvfiod is refused",
                          doctor.usbvfiod_line())

    def test_doctor_prints_the_line_and_a_host_without_usbvfiod_passes(self):
        with mock.patch.object(usbvfiod, "binary", side_effect=DeskError("usbvfiod is not installed: build it")), \
                mock.patch("sys.stdout") as out, mock.patch.object(doctor, "libvirt_connection",
                                                                    return_value=("libvirt", None)), \
                mock.patch.object(doctor, "kvm", return_value=None), \
                mock.patch.object(doctor, "images", return_value=([], {})), \
                mock.patch.object(doctor, "storage", return_value=([], [])), \
                mock.patch.object(doctor, "timer_line", return_value="none"):
            doctor.run()
        printed = "".join(c.args[0] for c in out.write.call_args_list)
        self.assertIn("usbvfiod (optional): usbvfiod is not installed: build it", printed)


class Process(InRuns):
    def tearDown(self):
        usbvfiod.stop(runs.load("r1"))

    def test_start_runs_the_server_in_the_runs_directory_and_notes_it_in_run_json(self):
        self.fake_binary()
        usbvfiod.start(self.run)
        state = runs.load("r1").usbvfiod
        d = self.run.dir / "usbvfiod"
        self.assertEqual((state["vfio"], state["hotplug"], state["log"], state["pcap"]),
                         tuple(str(d / n) for n in ("vfio.sock", "hotplug.sock", "usbvfiod.log", "usb.pcap")))
        self.assertTrue(usbvfiod.alive(self.run))
        self.assertTrue((d / "vfio.sock").exists() and (d / "hotplug.sock").exists())
        first = (d / "usbvfiod.log").read_text().splitlines()[0]
        self.assertEqual(first, f"fake usbvfiod -v --no-color --socket-path {d / 'vfio.sock'} --hotplug-socket-path "
                                f"{d / 'hotplug.sock'} --pcap-path {d / 'usb.pcap'}")
        self.assertEqual(usbvfiod.hotplug_socket(self.run), d / "hotplug.sock")

    def test_it_lives_in_a_session_of_its_own_so_it_outlives_the_command(self):
        self.fake_binary()
        usbvfiod.start(self.run)
        pid = self.run.usbvfiod["pid"]
        self.assertEqual(os.getsid(pid), pid)

    def test_a_server_that_ends_at_once_is_reported_with_the_end_of_its_log(self):
        self.fake_binary("die")
        with self.assertRaisesRegex(DeskError, r"usbvfiod ended with 3: fake usbvfiod .*\nERROR no luck \(.*usbvfiod.log\)"):
            usbvfiod.start(self.run)
        self.assertFalse(usbvfiod.alive(self.run))
        self.assertTrue(self.run.usbvfiod)  # noted, so that desk down finds the log

    def test_a_server_that_opens_no_sockets_is_not_waited_for_for_ever(self):
        self.fake_binary("silent")
        with mock.patch.object(usbvfiod, "START_WAIT", 0.5), \
                self.assertRaisesRegex(DeskError, "opened no sockets in 0 s"):
            usbvfiod.start(self.run)

    def test_a_socket_path_too_long_for_unix_sockets_is_refused_before_starting_anything(self):
        self.fake_binary()
        with mock.patch("desk.runs.RUNS", self.tmp / ("y" * 70) / "runs"):
            long_run = self.make("x" * 39)
            with self.assertRaisesRegex(DeskError, "too long for a unix socket"):
                usbvfiod.start(long_run)
        self.assertFalse(long_run.usbvfiod)

    def test_stop_ends_it_and_a_second_stop_has_nothing_to_do(self):
        self.fake_binary()
        usbvfiod.start(self.run)
        pid = self.run.usbvfiod["pid"]
        self.assertTrue(usbvfiod.stop(self.run))
        self.assertFalse(usbvfiod.alive(self.run))
        try:
            os.waitpid(pid, 0)  # this process started it; a real one is the init's child
        except ChildProcessError:
            pass
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        self.assertFalse(usbvfiod.stop(self.run))

    def test_a_server_that_ignores_sigterm_gets_sigkill(self):
        self.fake_binary("stubborn")
        usbvfiod.start(self.run)
        with mock.patch.object(usbvfiod, "STOP_WAIT", 0.3):
            self.assertTrue(usbvfiod.stop(self.run))
        self.assertFalse(usbvfiod.alive(self.run))

    def test_a_pid_that_is_some_other_process_now_is_never_signalled(self):
        sleeper = subprocess.Popen(["sleep", "30"])
        self.addCleanup(sleeper.wait)
        self.addCleanup(sleeper.kill)
        with self.run.edit() as r:
            r.usbvfiod = {"pid": sleeper.pid, "vfio": str(self.run.dir / "usbvfiod" / "vfio.sock"),
                          "hotplug": "h", "log": "l", "pcap": "p", "bus": ""}
        self.assertFalse(usbvfiod.alive(self.run))
        self.assertFalse(usbvfiod.stop(self.run))
        time.sleep(0.1)
        self.assertIsNone(sleeper.poll())

    def test_a_run_without_the_controller_has_nothing_to_stop(self):
        self.assertFalse(usbvfiod.stop(self.run))
        self.assertFalse(usbvfiod.alive(self.run))


class Controller(InRuns):
    XML = """<domain><devices>
      <controller type='pci' index='0' model='pcie-root'/>
      {ports}
      <controller type='usb' index='0' model='qemu-xhci'><address type='pci' domain='0x0000' bus='0x02' slot='0x00' function='0x0'/></controller>
      <interface type='user'><address type='pci' domain='0x0000' bus='0x01' slot='0x00' function='0x0'/></interface>
      <disk type='file' device='disk'><address type='pci' domain='0x0000' bus='0x06' slot='0x00' function='0x0'/></disk>
    </devices></domain>"""

    def domain(self, indexes=range(1, 15)):
        ports = "".join(f"<controller type='pci' index='{i}' model='pcie-root-port'><alias name='pci.{i}'/></controller>"
                        for i in indexes)
        return mock.Mock(**{"XMLDesc.return_value": self.XML.format(ports=ports)})

    def test_the_highest_free_root_port_is_taken_so_that_extra_disks_find_theirs(self):
        self.assertEqual(usbvfiod.free_root_port(self.domain()), "pci.14")
        self.assertEqual(usbvfiod.free_root_port(self.domain(range(1, 7))), "pci.5")
        self.assertEqual(usbvfiod.free_root_port(self.domain([3, 4])), "pci.4")

    def test_no_free_port_is_refused(self):
        with self.assertRaisesRegex(DeskError, "no free PCIe root port"):
            usbvfiod.free_root_port(self.domain([1, 2, 6]))

    def with_server(self, alive=True):
        with self.run.edit() as r:
            r.usbvfiod = {"pid": 4242, "vfio": "/r/vfio.sock", "hotplug": "/r/hotplug.sock",
                          "log": str(self.run.dir / "usbvfiod.log"), "pcap": "/r/usb.pcap", "bus": ""}
        (self.run.dir / "usbvfiod.log").write_text("starting\nlistening\n")
        patch = mock.patch.object(usbvfiod, "_alive", return_value=alive)
        patch.start()
        self.addCleanup(patch.stop)

    def test_the_controller_is_added_to_the_booted_guest_and_the_port_noted(self):
        self.with_server()
        with mock.patch.object(usbvfiod.virt, "qmp") as qmp:
            usbvfiod.attach_controller(self.run, self.domain())
        qmp.assert_called_once_with(mock.ANY, "device_add", driver="vfio-user-pci", id="usbvfiod0", bus="pci.14",
                                    socket={"type": "unix", "path": "/r/vfio.sock"})
        self.assertEqual(runs.load("r1").usbvfiod["bus"], "pci.14")

    def test_a_refusal_of_qemu_comes_with_the_servers_log(self):
        self.with_server()
        with mock.patch.object(usbvfiod.virt, "qmp", side_effect=DeskError("QMP device_add: connect failed")), \
                self.assertRaisesRegex(DeskError, r"could not add the USB controller on pci\.14: QMP device_add: "
                                                  r"connect failed; usbvfiod says: starting\nlistening"):
            usbvfiod.attach_controller(self.run, self.domain())
        self.assertEqual(runs.load("r1").usbvfiod["bus"], "")

    def test_a_server_that_has_ended_is_named_before_qemu_is_asked(self):
        self.with_server(alive=False)
        with mock.patch.object(usbvfiod.virt, "qmp") as qmp, \
                self.assertRaisesRegex(DeskError, "ended before the controller was added: starting"):
            usbvfiod.attach_controller(self.run, self.domain())
        qmp.assert_not_called()

    def test_a_command_for_a_run_without_the_controller_says_how_to_get_one(self):
        with self.assertRaisesRegex(DeskError, r"run r1 has no usbvfiod controller \(desk up IMAGE --usbvfiod\)"):
            usbvfiod.hotplug_socket(self.run)

    def test_a_command_for_a_run_whose_server_died_says_the_vm_went_with_it(self):
        self.with_server(alive=False)
        with self.assertRaisesRegex(DeskError, r"usbvfiod \(pid 4242\) of run r1 has ended: the guest has lost its USB "
                                               r"controller, and QEMU can abort the VM with it \(desk down r1.*its log "
                                               r"ends with: starting / listening"):
            usbvfiod.hotplug_socket(self.run)

    def test_a_vm_that_is_gone_because_its_server_died_says_so_instead_of_stopped(self):
        self.with_server(alive=False)
        with mock.patch("desk.runs.lookup", return_value=None), \
                self.assertRaisesRegex(DeskError, r"run r1 is not running: usbvfiod \(pid 4242\) of run r1 has ended"):
            self.run.dom()
        self.with_server(alive=True)
        with mock.patch("desk.runs.lookup", return_value=None), \
                self.assertRaisesRegex(DeskError, r"run r1 is not running \(desk down r1 to clean up\)"):
            self.run.dom()

    def test_host_state_tells_whether_the_server_runs(self):
        self.with_server(alive=False)
        with mock.patch("desk.runs.lookup", return_value=None):
            self.assertIn("its usbvfiod has ended, so the guest has lost its USB controller", runs.host_state(self.run))


class Listing(InRuns):
    def test_ls_marks_a_run_whose_server_has_ended(self):
        served = self.make("r2", usbvfiod={"pid": 1, "vfio": "v", "hotplug": "h", "log": "l", "pcap": "p",
                                           "bus": "pci.14"})
        with mock.patch("desk.runs.lookup", return_value=None), mock.patch.object(usbvfiod, "_alive", return_value=False), \
                mock.patch("builtins.print") as out:
            cli.cmd_ls(SimpleNamespace())
        lines = {c.args[0].split("\t")[0]: c.args[0].split("\t")[2] for c in out.call_args_list}
        self.assertEqual(lines, {"r1": "stopped", served.id: "stopped, usbvfiod ended"})


class Down(InRuns):
    def domain(self, events):
        dom = mock.Mock()
        dom.isActive.return_value = True
        dom.XMLDesc.return_value = f"<domain>{self.run.dir / 'disk.qcow2'}</domain>"
        dom.destroy.side_effect = lambda: events.append("domain destroyed")
        return dom

    def served(self, alive=True):
        with self.run.edit() as r:
            r.usbvfiod = {"pid": 4242, "vfio": "/r/vfio.sock", "hotplug": "/r/h", "log": "/r/usbvfiod.log",
                          "pcap": "/r/p", "bus": "pci.14"}
        patch = mock.patch.object(usbvfiod, "_alive", return_value=alive)
        patch.start()
        self.addCleanup(patch.stop)

    def test_the_domain_goes_first_and_the_server_after_it(self):
        events = []
        self.served()
        with mock.patch("desk.runs.lookup", return_value=self.domain(events)), \
                mock.patch.object(usbvfiod, "stop", side_effect=lambda run: events.append("server stopped")):
            runs.down(self.run)
        self.assertEqual(events, ["domain destroyed", "server stopped"])
        self.assertFalse(self.run.dir.exists())

    def test_devices_the_server_left_without_drivers_are_bound_again_after_it_stopped(self):
        events = []
        self.served()
        with self.run.edit() as r:
            r.usbvfiod = {**r.usbvfiod, "held": [{"bus": 5, "dev": 12, "port": "5-1"},
                                                 {"bus": 5, "dev": 13, "port": "5-2"}]}

        def rebind(port, bus, dev):
            events.append(f"rebind {port} {bus}.{dev}")
            return [0] if port == "5-1" else []

        with mock.patch("desk.runs.lookup", return_value=self.domain(events)), \
                mock.patch.object(usbvfiod, "stop", side_effect=lambda run: events.append("server stopped")), \
                mock.patch.object(usb, "rebind", side_effect=rebind), mock.patch("sys.stderr") as err:
            runs.down(self.run)
        self.assertEqual(events, ["domain destroyed", "server stopped", "rebind 5-1 5.12", "rebind 5-2 5.13"])
        printed = "".join(c.args[0] for c in err.write.call_args_list)
        self.assertIn("usbvfiod of run r1 left USB devices without their drivers; desk bound them again: "
                      "5.12 (port:5-1)\n", printed)

    def test_a_domain_that_will_not_be_destroyed_leaves_the_server_running(self):
        events = []
        self.served()
        dom = self.domain(events)
        dom.destroy.side_effect = DeskError("busy")
        with mock.patch("desk.runs.lookup", return_value=dom), \
                mock.patch.object(usbvfiod, "stop", side_effect=lambda run: events.append("server stopped")), \
                self.assertRaisesRegex(DeskError, "busy"):
            runs.down(self.run)
        self.assertEqual(events, [])

    def test_a_server_that_ended_before_is_said_so_on_the_way_down(self):
        self.served(alive=False)
        with mock.patch("desk.runs.lookup", return_value=None), mock.patch("sys.stderr") as err:
            runs.down(self.run)
        printed = "".join(c.args[0] for c in err.write.call_args_list)
        self.assertIn("usbvfiod of run r1 had ended before the run went down (log: /r/usbvfiod.log, and in the report)",
                      printed)

    def test_a_run_without_the_controller_goes_down_as_before(self):
        with mock.patch("desk.runs.lookup", return_value=None), mock.patch.object(usbvfiod, "stop") as stop, \
                mock.patch("sys.stderr") as err:
            runs.down(self.run)
        stop.assert_not_called()
        err.write.assert_not_called()

    def test_the_server_is_stopped_for_a_kept_run_too(self):
        events = []
        self.served()
        with mock.patch("desk.runs.lookup", return_value=self.domain(events)), \
                mock.patch.object(usbvfiod, "stop", side_effect=lambda run: events.append("server stopped")):
            runs.down(self.run, keep=True)
        self.assertEqual(events, ["domain destroyed", "server stopped"])
        self.assertTrue(self.run.dir.exists())


class WithController(InRuns):
    """A run whose usbvfiod is a fake that keeps a tree, on the socket the run names."""

    def setUp(self):
        super().setUp()
        self.node = self.tmp / "node"
        self.node.write_bytes(b"device-node")
        self.deck = {**DECK, "node": str(self.node)}
        self.controller = FakeController()
        self.server = FakeHotplug(self.tmp, lambda *a: self.controller.handler(*a))
        self.addCleanup(self.server.close)
        with self.run.edit() as r:
            r.usbvfiod = {"pid": 4242, "vfio": "/r/vfio.sock", "hotplug": str(self.server.path),
                          "log": str(self.tmp / "usbvfiod.log"), "pcap": "/r/p", "bus": "pci.14"}
        alive = mock.patch.object(usbvfiod, "_alive", return_value=True)
        alive.start()
        self.addCleanup(alive.stop)

    def commands(self):
        return [(b, p) for b, p, _ in self.server.seen]

    def events(self):
        import json

        return [e for e in map(json.loads, (self.run.dir / "log.jsonl").read_text().splitlines()) if "usb" in e]


class Tree(WithController):
    """desk usb add / rm against a controller that keeps a tree."""

    def test_a_device_at_a_path_gets_the_hubs_that_are_missing_top_first(self):
        got = usbvfiod.attach(self.run, self.deck, "5.1.4", hubs.parse_hubs(["5=067b:2586/4/hs"]))
        self.assertEqual([(b, p.get("path")) for b, p in self.commands()],
                         [(0x14, None), (0x11, "5"), (0x11, "5.1"), (0x10, "5.1.4"), (0x14, None)])
        self.assertEqual(self.server.seen[1][1]["spec"], {"vid": 0x067B, "pid": 0x2586, "ports": 4, "speed": "high"})
        self.assertEqual(self.server.seen[2][1]["spec"], {"vid": 0x0409, "pid": 0x55AA, "ports": 8, "speed": "high"})
        self.assertEqual(self.server.seen[3][2], [b"device-node"])  # the fd of the node desk opened
        self.assertEqual(self.server.seen[3][1], {"path": "5.1.4", "bus": 5, "device": 12})
        self.assertEqual((got["at"], got["hubs"]), ("5.1.4", ["5", "5.1"]))
        self.assertEqual(self.controller.paths(), ["5", "5.1", "5.1.4"])
        added = [e for e in self.events() if e["usb"] == "add"]
        self.assertEqual((added[0]["at"], added[0]["hubs"], added[0]["address"]), ("5.1.4", ["5", "5.1"], "5.12"))

    def test_hubs_that_are_there_are_not_made_again(self):
        usbvfiod.attach(self.run, self.deck, "5.1.4", {})
        self.server.seen.clear()
        other = {**self.deck, "dev": 13}
        got = usbvfiod.attach(self.run, other, "5.1.3", {})
        self.assertEqual([b for b, _ in self.commands()], [0x14, 0x10, 0x14])
        self.assertEqual(got["hubs"], [])

    def test_without_a_path_the_device_takes_the_first_free_root_port_and_the_tree_says_which(self):
        got = usbvfiod.attach(self.run, self.deck, None, {})
        self.assertEqual([b for b, _ in self.commands()], [0x14, 0x00, 0x14])
        self.assertEqual(self.server.seen[1][1:], ((5, 12), [b"device-node"]))
        self.assertEqual(got["at"], "5")

    def test_hubs_without_a_path_are_refused_before_the_server_is_asked(self):
        with self.assertRaisesRegex(DeskError, "--hub goes with --at"):
            usbvfiod.attach(self.run, self.deck, None, hubs.parse_hubs(["5=/4"]))
        self.assertEqual(self.server.seen, [])

    def test_a_plan_the_controller_would_refuse_is_refused_before_anything_is_changed(self):
        with self.assertRaisesRegex(DeskError, "hub at 5 has 4 ports"):
            usbvfiod.attach(self.run, self.deck, "5.5", hubs.parse_hubs(["5=/4"]))
        self.assertEqual([b for b, _ in self.commands()], [0x14])
        self.assertEqual(self.controller.tree, [])

    def test_a_device_the_controller_refuses_takes_the_hubs_made_for_it_away_again(self):
        self.controller.refuse_host = "SpeedNotSupported"
        with self.assertRaisesRegex(DeskError, "usbvfiod refused to attach 5.12 at 5.1.4: SpeedNotSupported"):
            usbvfiod.attach(self.run, self.deck, "5.1.4", {})
        self.assertEqual([(b, p.get("path")) for b, p in self.commands()],
                         [(0x14, None), (0x11, "5"), (0x11, "5.1"), (0x10, "5.1.4"), (0x13, "5")])
        self.assertEqual(self.controller.tree, [])

    def test_hubs_that_were_there_before_stay_when_a_device_is_refused(self):
        usbvfiod.attach(self.run, {**self.deck, "dev": 99}, "5.1.1", {})
        self.controller.refuse_host = "SpeedNotSupported"
        with self.assertRaises(DeskError):
            usbvfiod.attach(self.run, self.deck, "5.1.4", {})
        self.assertEqual(self.controller.paths(), ["5", "5.1", "5.1.1"])

    def test_a_device_in_the_tree_already_is_said_with_its_path(self):
        usbvfiod.attach(self.run, self.deck, "5.1.4", {})
        with self.assertRaisesRegex(DeskError, r"0fd9:008f is in run r1 already, at 5\.1\.4 \(desk -r r1 usb rm\)"):
            usbvfiod.attach(self.run, self.deck, "5.2.1", {})

    def test_a_device_the_controller_accepts_but_does_not_list_is_not_reported_as_attached(self):
        self.controller.forget_host = True
        with self.assertRaisesRegex(DeskError, "accepted USB device 0fd9:008f but its tree does not show it"):
            usbvfiod.attach(self.run, self.deck, "5", {})

    def test_a_node_that_cannot_be_opened_changes_nothing(self):
        with self.assertRaisesRegex(DeskError, "cannot open .*gone"):
            usbvfiod.attach(self.run, {**self.deck, "node": str(self.tmp / "gone")}, "5.1.4", {})
        self.assertEqual(self.controller.tree, [])
        self.assertEqual([b for b, _ in self.commands()], [0x14])

    def test_one_desk_command_at_a_time_changes_the_tree(self):
        import fcntl

        with usbvfiod.lock(self.run), open(self.run.dir / ".usbvfiod.lock", "a+") as other, \
                self.assertRaises(BlockingIOError):
            fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_a_hub_is_taken_out_with_everything_below_it(self):
        usbvfiod.attach(self.run, self.deck, "5.1.4", {})
        got = usbvfiod.remove(self.run, "at:5.1")
        self.assertEqual((got["id"], got["below"]), ("at:5.1", ["5.1.4"]))
        self.assertEqual(self.controller.paths(), ["5"])
        self.assertEqual([e for e in self.events() if e["usb"] == "remove"][0]["below"], ["5.1.4"])

    def test_the_run_notes_the_host_devices_it_holds_until_they_are_taken_out(self):
        other = {**self.deck, "dev": 13, "port": "5-2"}
        usbvfiod.attach(self.run, self.deck, "5.1.4", {})
        usbvfiod.attach(self.run, other, "5.2", {})

        def held():
            return [(h["bus"], h["dev"], h["port"]) for h in runs.load("r1").usbvfiod["held"]]

        self.assertEqual(held(), [(5, 12, "5-1"), (5, 13, "5-2")])
        usbvfiod.remove(self.run, "at:5.1")
        self.assertEqual(held(), [(5, 13, "5-2")])
        with mock.patch.object(usb, "host_devices", return_value=[other]):
            usbvfiod.remove(self.run, "5.13")
        self.assertEqual(held(), [])

    def test_a_port_path_with_nothing_is_said_so(self):
        with self.assertRaisesRegex(DeskError, r"run r1 has nothing at port path 5\.1 \(desk usb ls\)"):
            usbvfiod.remove(self.run, "at:5.1")
        self.assertNotIn(0x13, [b for b, _ in self.commands()])

    def test_a_device_is_taken_out_by_what_it_is_called_on_the_host(self):
        usbvfiod.attach(self.run, self.deck, "5.1.4", {})
        for query in ("0fd9:008f", "5.12", "port:5-1", "serial:S"):
            self.controller.tree = [{"path": "5.1.4", "speed": "high", "id": "host 005:012"}]
            with self.subTest(query), mock.patch.object(usb, "host_devices", return_value=[self.deck]):
                got = usbvfiod.remove(self.run, query)
            self.assertEqual((got["id"], got["at"]), ("0fd9:008f", "5.1.4"))
            self.assertEqual(self.controller.tree, [])

    def test_a_device_gone_from_the_host_is_still_taken_out_by_its_address(self):
        self.controller.tree = [{"path": "5.1.4", "speed": "high", "id": "host 005:012"}]
        with mock.patch.object(usb, "host_devices", return_value=[]):
            got = usbvfiod.remove(self.run, "5.12")
        self.assertEqual((got["bus"], got["dev"], got["at"]), (5, 12, "5.1.4"))

    def test_a_device_the_run_does_not_have_is_said_so(self):
        with mock.patch.object(usb, "host_devices", return_value=[self.deck]), \
                self.assertRaisesRegex(DeskError, r"run r1 has no USB device 0fd9:008f \(desk usb ls\)"):
            usbvfiod.remove(self.run, "0fd9:008f")

    def test_a_server_that_is_gone_is_said_so_not_taken_for_an_empty_tree(self):
        self.server.close()
        self.server.path.unlink()
        with self.assertRaisesRegex(DeskError, "nobody listens"):
            usbvfiod.tree(self.run)


class Commands(WithController):
    def test_usb_add_on_a_run_with_the_controller_does_not_need_qemus_usb_host(self):
        with mock.patch.object(doctor, "_qemu_devices", side_effect=AssertionError("usb-host probe")), \
                mock.patch.object(usb, "openable", return_value=self.deck), \
                mock.patch("desk.runs.all_runs", return_value=[self.run]), mock.patch("desk.runs.lookup",
                                                                                      return_value=None):
            got = usb.add(self.run, "0fd9:008f", at="5.1.4", hub_options=["5=/4/hs"])
        self.assertEqual((got["at"], got["hubs"]), ("5.1.4", ["5", "5.1"]))

    def test_a_port_path_on_a_run_without_the_controller_says_how_to_get_one(self):
        plain = self.make("r2")
        for kw in ({"at": "5.1"}, {"hub_options": ["5=/4"]}):
            with self.subTest(kw), self.assertRaisesRegex(DeskError, r"run r2 does not have \(desk up IMAGE "
                                                                     r"--usbvfiod\)"):
                usb.add(plain, "0fd9:008f", **kw)
        with self.assertRaisesRegex(DeskError, r"at:PORTPATH names what is in the controller of usbvfiod, which run "
                                               r"r2 does not have"):
            usb.remove(plain, "at:5.1")

    def test_a_device_another_run_holds_in_its_controller_is_not_handed_out_twice(self):
        other = self.make("r2", usbvfiod={"pid": 1, "vfio": "v", "hotplug": str(self.server.path), "log": "l",
                                         "pcap": "p", "bus": "pci.14"})
        self.controller.tree = [{"path": "5.1.4", "speed": "high", "id": "host 005:012"}]
        live = mock.patch("desk.runs.lookup", return_value=mock.Mock(**{
            "isActive.return_value": True, "XMLDesc.return_value": f"{self.run.dir}/disk.qcow2 {other.dir}/disk.qcow2"}))
        with live, mock.patch("desk.runs.all_runs", return_value=[other]), \
                mock.patch.object(usb, "openable", return_value=self.deck), \
                self.assertRaisesRegex(DeskError, r"0fd9:008f is in run r2 already \(desk -r r2 usb rm\)"):
            usb.add(self.run, "0fd9:008f", at="5.2.1")

    def test_ls_shows_the_tree_and_which_run_has_which_host_device_where(self):
        usbvfiod.attach(self.run, self.deck, "5.1.4", hubs.parse_hubs(["5=/4/hs", "5.1=/4/hs"]))
        live = mock.patch("desk.runs.lookup", return_value=mock.Mock(**{
            "isActive.return_value": True, "XMLDesc.return_value": f"{self.run.dir}/disk.qcow2"}))
        with live, mock.patch("desk.runs.all_runs", return_value=[self.run]), \
                mock.patch.object(usb, "host_devices", return_value=[self.deck]), mock.patch("builtins.print") as out:
            cli.cmd_usb(SimpleNamespace(usb_cmd="ls"))
        lines = [c.args[0] for c in out.call_args_list]
        self.assertEqual(lines, ["0fd9:008f\t5.12\tport:5-1\tStream Deck XL\tin run r1 at 5.1.4",
                                 "hub\t-\tat:5\tUSB hub, 4 ports, high speed\tin run r1 at 5",
                                 "hub\t-\tat:5.1\tUSB hub, 4 ports, high speed\tin run r1 at 5.1"])

    def test_ls_says_when_a_controller_could_not_be_read(self):
        self.server.close()
        self.server.path.unlink()
        live = mock.patch("desk.runs.lookup", return_value=mock.Mock(**{
            "isActive.return_value": True, "XMLDesc.return_value": f"{self.run.dir}/disk.qcow2"}))
        with live, mock.patch("desk.runs.all_runs", return_value=[self.run]), \
                mock.patch.object(usb, "host_devices", return_value=[]), mock.patch("builtins.print") as out:
            cli.cmd_usb(SimpleNamespace(usb_cmd="ls"))
        (line,) = [c.args[0] for c in out.call_args_list]
        self.assertRegex(line, r"^-\t-\t-\t-\tthe USB tree of run r1 could not be read: nobody listens on ")

    def test_socket_prints_the_path_and_nothing_else(self):
        with mock.patch("desk.cli.runs.resolve", return_value=self.run), mock.patch("builtins.print") as out:
            cli.cmd_usb(SimpleNamespace(usb_cmd="socket", run=None))
        out.assert_called_once_with(self.server.path)

    def test_socket_of_a_run_without_the_controller_is_desks_error(self):
        plain = self.make("r2")
        with mock.patch("desk.cli.runs.resolve", return_value=plain), mock.patch("builtins.print") as out, \
                self.assertRaisesRegex(DeskError, "run r2 has no usbvfiod controller"):
            cli.cmd_usb(SimpleNamespace(usb_cmd="socket", run=None))
        out.assert_not_called()

    def test_add_and_rm_print_where_things_went(self):
        with mock.patch("desk.cli.runs.resolve", return_value=self.run), \
                mock.patch.object(usb, "openable", return_value=self.deck), \
                mock.patch("desk.runs.all_runs", return_value=[]), mock.patch("builtins.print") as out:
            cli.cmd_usb(SimpleNamespace(usb_cmd="add", run=None, device="0fd9:008f", at="5.1.4",
                                        hub=["5=/4/hs", "5.1=/4/hs"]))
            cli.cmd_usb(SimpleNamespace(usb_cmd="rm", run=None, device="at:5.1"))
        lines = [c.args[0] for c in out.call_args_list]
        self.assertEqual(lines, ["USB device 0fd9:008f Stream Deck XL handed to the guest at port path 5.1.4, with the "
                                 "hubs 5, 5.1 (the host does without it until desk usb rm or desk down)",
                                 "at:5.1 removed (USB hub, 4 ports, high speed, with 5.1.4 below it)"])


class Report(InRuns):
    def setUp(self):
        super().setUp()
        self.log = self.run.dir / "usbvfiod.log"
        self.log.write_text("INFO started\nWARN something odd\nERROR something worse\nINFO last\n")
        with self.run.edit() as r:
            r.usbvfiod = {"pid": 4242, "vfio": "/r/vfio.sock", "hotplug": "/r/h", "log": str(self.log),
                          "pcap": "/r/p", "bus": "pci.14"}
        self.out = self.tmp / "report"
        self.out.mkdir()

    def report(self, alive: bool, domain: bool) -> str:
        with mock.patch.object(usbvfiod, "_alive", return_value=alive), \
                mock.patch.object(usbvfiod, "tree", return_value=[{"path": "5", "speed": "high", "id": "virtual 1",
                                                                   "hub_ports": 4}]), \
                mock.patch.object(runs.Run, "live_domain", return_value=object() if domain else None):
            return usbvfiod.report(self.run, self.out)

    def test_a_running_server_is_reported_with_its_tree_and_its_log_is_kept(self):
        text = self.report(True, True)
        self.assertIn("- usbvfiod: running (pid 4242), controller on pci.14; 2 warnings and errors in its log "
                      "(see usbvfiod.log)", text)
        self.assertIn("5              USB hub, 4 ports, high speed", text)
        self.assertEqual((self.out / "usbvfiod.log").read_text(), self.log.read_text())
        self.assertNotIn("usb.pcap", text)  # no traffic yet: the server writes the file with the first packet

    def test_the_pcap_goes_into_the_report_unless_it_is_too_big(self):
        pcap = self.tmp / "usb.pcap"
        pcap.write_bytes(b"pcap")
        with self.run.edit() as r:
            r.usbvfiod = {**r.usbvfiod, "pcap": str(pcap)}
        self.assertIn("the USB traffic of the guest is in usb.pcap", self.report(True, True))
        self.assertEqual((self.out / "usb.pcap").read_bytes(), b"pcap")
        (self.out / "usb.pcap").unlink()
        with mock.patch.object(usbvfiod, "PCAP_LIMIT", 2):
            self.assertIn(f"its USB traffic (0 MiB) is too big for the report: {pcap}", self.report(True, True))
        self.assertFalse((self.out / "usb.pcap").exists())

    def test_a_server_that_ended_while_the_vm_runs_is_the_headline(self):
        text = self.report(False, True)
        self.assertIn("usbvfiod: ENDED while the VM runs (pid 4242): the guest has lost its controller, and QEMU can "
                      "abort the VM with it", text)
        self.assertIn("the end of its log:\n\n```\nINFO started\nWARN something odd\nERROR something worse\nINFO last\n```",
                      text)

    def test_a_server_and_a_vm_that_are_both_gone_do_not_blame_either(self):
        text = self.report(False, False)
        self.assertIn("ended (pid 4242), and the VM is gone too", text)
        self.assertIn("so its log tells which came first", text)

    def test_the_report_of_a_run_with_the_controller_has_its_section(self):
        from desk import evidence

        run = mock.Mock(id="r1", dir=self.tmp / "run", image="windows-11", created=time.time() - 60, ready=0.0,
                        usbvfiod=self.run.usbvfiod)
        run.dir.mkdir()
        run.live_domain.return_value = None
        with mock.patch.object(evidence, "REPORTS", self.tmp / "reports"), \
                mock.patch.object(usbvfiod, "report", return_value="- usbvfiod: the line") as line:
            md = (evidence.collect(run) / "report.md").read_text()
        line.assert_called_once()
        self.assertIn("## Found automatically\n\n- usbvfiod: the line\n", md)


class Snapshots(InRuns):
    def test_a_run_with_the_controller_is_not_snapshotted_nor_restored(self):
        from desk import snapshot

        with self.run.edit() as r:
            r.usbvfiod = {"pid": 1, "vfio": "v", "hotplug": "h", "log": "l", "pcap": "p", "bus": ""}
        for call in (lambda: snapshot.save(self.run, "s"), lambda: snapshot.restore(self.run, "s")):
            with self.assertRaisesRegex(DeskError, r"snapshots are not made of runs with the usbvfiod controller "
                                                   r"\(run r1\)"):
                call()
        self.assertFalse((self.run.dir / "snapshots").exists())


if __name__ == "__main__":
    unittest.main()
