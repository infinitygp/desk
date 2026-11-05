import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from desk import evidence, runs
from desk.config import DeskError


class NoDomains:
    def listAllDomains(self, _flags):
        return []


class IdleTtl(unittest.TestCase):
    def test_seconds_or_the_default(self):
        with mock.patch.dict(os.environ, {"DESK_TTL": "5"}):
            self.assertEqual(runs.idle_ttl(), 5)
        with mock.patch.dict(os.environ, {"DESK_TTL": ""}):
            self.assertEqual(runs.idle_ttl(), 7200)

    def test_anything_else_is_desks_error_and_only_where_it_is_used(self):
        with mock.patch.dict(os.environ, {"DESK_TTL": "2h"}), self.assertRaisesRegex(DeskError, "DESK_TTL.*'2h'"):
            runs.idle_ttl()


class RunFile(unittest.TestCase):
    def test_commands_side_by_side_keep_each_others_changes(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch("desk.runs.RUNS", Path(tmp)):
            first = runs.Run(id="r1", image="fedora-44-gnome", domain="desk-r1", ssh_port=1, user="tester",
                             created=1.0)
            first.dir.mkdir()
            first.save()
            # Two commands that loaded the run before either wrote: desk clock and desk shot.
            clock, shot = runs.load("r1"), runs.load("r1")
            with clock.edit() as r:
                r.clock_offset = -7200.0
            shot.touch()
            paths = {shot.next_shot_path(), runs.load("r1").next_shot_path()}
            self.assertEqual(len(paths), 2)
            saved = runs.load("r1")
            self.assertEqual((saved.clock_offset, saved.shots), (-7200.0, 2))
            self.assertEqual(shot.clock_offset, -7200.0)  # taken over by the object too

    def test_a_command_stuck_with_the_run_file_is_not_waited_for_ever(self):
        import fcntl
        import itertools

        from desk.config import Busy

        with tempfile.TemporaryDirectory() as tmp, mock.patch("desk.runs.RUNS", Path(tmp)), \
                mock.patch("desk.runs.HOME", Path(tmp)):
            run = runs.Run(id="r1", image="fedora-44-gnome", domain="desk-r1", ssh_port=1, user="tester", created=1.0)
            run.dir.mkdir()
            run.save()
            with open(run.dir / ".run.lock", "a+") as other:  # as a stopped desk process holds it
                fcntl.flock(other, fcntl.LOCK_EX)
                with mock.patch("desk.runs.time.monotonic", side_effect=itertools.count(0, 30)), \
                        mock.patch("desk.runs.time.sleep"), mock.patch("sys.stderr"), \
                        self.assertRaisesRegex(Busy, "another command changing run r1"):
                    run.touch()


class RunNames(unittest.TestCase):
    def test_only_names_that_stay_in_the_runs_directory(self):
        for bad in ("a/b", "../x", "", ".hidden", "with space", "x" * 41):
            with self.subTest(bad=bad), self.assertRaisesRegex(DeskError, "run names are"):
                runs.up(mock.Mock(), run_id=bad)
        for good in ("ab12cd", "selftest-0f3a9c", "my_run-2", "f44.test"):
            self.assertEqual(runs._checked_id(good), good)

    def test_runs_named_before_the_check_are_still_found_but_nothing_outside(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch("desk.runs.RUNS", Path(tmp) / "runs"):
            old = runs.Run(id="my test", image="fedora-44-gnome", domain="desk-my test", ssh_port=1, user="tester",
                           created=1.0)
            old.dir.mkdir(parents=True)
            old.save()
            self.assertEqual([r.id for r in runs.all_runs()], ["my test"])
            with mock.patch.object(runs.Run, "hold"), mock.patch.object(runs.Run, "log_event"):
                self.assertEqual(runs.resolve("my test").id, "my test")
            for bad in ("../runs/my test", "..", "."):
                with self.subTest(bad=bad), self.assertRaisesRegex(DeskError, "no run"):
                    runs.resolve(bad)


class LiveDomain(unittest.TestCase):
    def test_a_domain_destroyed_meanwhile_is_not_running(self):
        import libvirt

        gone = mock.Mock()
        gone.isActive.side_effect = libvirt.libvirtError("Domain not found: no domain with matching uuid")
        run = runs.Run(id="r1", image="fedora-44-gnome", domain="desk-r1", ssh_port=1, user="tester", created=1.0)
        with mock.patch("desk.runs.lookup", return_value=gone):
            self.assertIsNone(run.live_domain())


class Gc(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        for target, value in (("desk.runs.RUNS", self.home / "runs"), ("desk.runs.HOME", self.home),
                              ("desk.evidence.REPORTS", self.home / "reports"),
                              ("desk.runs.conn", NoDomains), ("desk.runs.lookup", lambda name: None)):
            patch = mock.patch(target, value)
            patch.start()
            self.addCleanup(patch.stop)

    def run_idle_for(self, run_id: str, hours: float) -> runs.Run:
        then = time.time() - hours * 3600
        run = runs.Run(id=run_id, image="fedora-44-gnome", domain=f"desk-{run_id}", ssh_port=1, user="tester",
                       created=then, last_used=then)
        (run.dir / "shots").mkdir(parents=True)
        (run.dir / "shots" / "0001.png").write_bytes(b"png")
        run.save()
        run.log_event(argv=["shot"])
        run.log_event(shot=str(run.dir / "shots" / "0001.png"))
        return run

    def test_an_idle_run_leaves_a_report_behind(self):
        old, fresh = self.run_idle_for("old", 3), self.run_idle_for("fresh", 0.1)
        removed = runs.gc_idle(verbose=False)
        self.assertEqual(len(removed), 1)
        self.assertRegex(removed[0], r"^old \(stopped, idle 3\.0 h\); report: .*/reports/old/report\.md$")
        self.assertFalse(old.dir.exists())
        self.assertTrue(fresh.dir.exists())
        report = (self.home / "reports" / "old" / "report.md").read_text()
        self.assertIn("desk gc takes the run down (stopped, idle 3.0 h)", report)
        self.assertIn("shots/0001.png", report)
        self.assertTrue((self.home / "reports" / "old" / "shots" / "0001.png").exists())

    def test_a_run_reported_before_is_not_reported_again(self):
        old = self.run_idle_for("old", 3)
        report = evidence.collect(old) / "report.md"  # desk down --keep wrote it then
        # The run's last command before it, gc's own line after it.
        os.utime(old.dir / "log.jsonl", (time.time() - 10,) * 2)
        os.utime(report, (time.time() - 5,) * 2)
        written = report.stat().st_mtime
        runs.gc_idle(verbose=False)
        self.assertFalse(old.dir.exists())
        self.assertEqual([p.name for p in (self.home / "reports").iterdir()], ["old"])
        self.assertEqual(report.stat().st_mtime, written)

    def test_an_earlier_run_of_the_same_name_does_not_count_as_its_report(self):
        yesterday = self.home / "reports" / "old"
        yesterday.mkdir(parents=True)
        os.utime(yesterday, (time.time() - 86400,) * 2)
        self.run_idle_for("old", 3)
        self.assertRegex(runs.gc_idle(verbose=False)[0], "report: ")
        self.assertEqual(len(list((self.home / "reports").iterdir())), 2)

    def test_old_reports_go_on_the_way(self):
        reports = self.home / "reports"
        for name, days in (("stale", 15), ("recent", 13)):
            (reports / name).mkdir(parents=True)
            os.utime(reports / name, (time.time() - days * 86400,) * 2)
        runs.gc_idle(verbose=False)
        self.assertEqual([p.name for p in reports.iterdir()], ["recent"])

    def test_a_run_whose_owner_came_back_meanwhile_stays(self):
        old = self.run_idle_for("old", 3)

        def collect_slowly(run, diff=False):
            runs.load(run.id).touch()  # desk shot -r old, while the report is written
            return self.home / "reports" / run.id

        with mock.patch("desk.evidence.collect", collect_slowly):
            self.assertEqual(runs.gc_idle(verbose=False), [])
        self.assertTrue((old.dir / "run.json").exists())

    def test_a_report_that_cannot_be_written_leaves_the_files_it_would_be_made_of(self):
        old = self.run_idle_for("old", 3)
        with mock.patch("desk.evidence.collect", side_effect=KeyError("name")), mock.patch("sys.stderr"):
            report, _ = evidence.finish(old, stopped=True)
        self.assertIsNone(report)
        self.assertTrue((old.dir / "shots" / "0001.png").exists())

    def test_one_teardown_at_a_time(self):
        old = self.run_idle_for("old", 3)

        def second_down(run, diff=False):
            with self.assertRaisesRegex(runs.DeskError, "being taken down by another"):
                evidence.finish(run)
            return self.home / "reports" / run.id

        with mock.patch("desk.evidence.collect", second_down):
            evidence.finish(old, stopped=True)
        self.assertFalse(old.dir.exists())
        with self.assertRaisesRegex(runs.DeskError, "gone already"):
            evidence.finish(old)


class WarmupDomain:
    def __init__(self, home: Path):
        self.xml = (f"<domain><devices><disk device='disk'><source file='{home}/warm/fedora-44-gnome."
                    "20261013-021139/disk.qcow2'/></disk></devices></domain>")
        self.destroyed = False

    def name(self):
        return "desk-warm-fedora-44-gnome-20261013-021139"

    def XMLDesc(self, _flags):
        return self.xml

    def destroy(self):
        self.destroyed = True


class GcWarmups(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.dom = WarmupDomain(self.home)
        conn = mock.Mock()
        conn.listAllDomains.return_value = [self.dom]
        for target, value in (("desk.runs.RUNS", self.home / "runs"), ("desk.runs.HOME", self.home),
                              ("desk.config.WARM", self.home / "warm"), ("desk.runs.conn", lambda: conn)):
            patch = mock.patch(target, value)
            patch.start()
            self.addCleanup(patch.stop)

    def test_spared_while_somebody_warms_the_image(self):
        import fcntl

        with open(self.home / ".lock-fedora-44-gnome", "a+") as warming:
            fcntl.flock(warming, fcntl.LOCK_EX)
            self.assertEqual(runs.gc(verbose=False), [])
        self.assertFalse(self.dom.destroyed)

    def test_destroyed_when_nobody_does(self):
        self.assertEqual(runs.gc(verbose=False), ["abandoned warm-up domain desk-warm-fedora-44-gnome-20261013-021139"])
        self.assertTrue(self.dom.destroyed)


class ColdReason(unittest.TestCase):
    def test_no_warm_state_and_an_outdated_one(self):
        from desk.config import load_image

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            spec = load_image("fedora-44-gnome")
            with mock.patch("desk.config.WARM", home / "warm"), mock.patch("desk.config.BASE", home / "base"):
                self.assertIn("no warm state", runs.cold_reason(spec))
                (home / "warm" / "fedora-44-gnome").mkdir(parents=True)
                (home / "warm" / "fedora-44-gnome" / "memory.save").write_bytes(b"")
                (home / "warm" / "fedora-44-gnome" / "meta.json").write_text('{"base": "/some/other/build"}')
                (home / "base" / "fedora-44-gnome").mkdir(parents=True)
                self.assertIn("predates its base image", runs.cold_reason(spec))
                base = str((home / "base" / "fedora-44-gnome").resolve())
                # Made by a desk that did not record its guest's time zone and layout.
                (home / "warm" / "fedora-44-gnome" / "meta.json").write_text(json.dumps({"base": base}))
                self.assertIn("does not say which time zone and keyboard layout", runs.cold_reason(spec))
                self.assertFalse(runs.warm_is_current(spec))


class Up(unittest.TestCase):
    """The state lock is held until the domain runs, and not while its guest boots."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.events = []
        base = self.home / "base" / "fedora-44-gnome"
        base.mkdir(parents=True)
        (base / "domain.xml").write_text("<domain/>")

        def held(what):
            return lambda *a, **k: self.events.append((what, bool(runs._locked.get(self.home / ".lock"))))

        conn = self.conn = mock.Mock()
        conn.createXML.side_effect = held("domain started")
        conn.listAllDomains.return_value = []
        for target, value in (
                ("desk.runs.RUNS", self.home / "runs"), ("desk.runs.HOME", self.home),
                ("desk.config.BASE", self.home / "base"), ("desk.config.WARM", self.home / "warm"),
                ("desk.runs.conn", lambda: conn), ("desk.runs.lookup", lambda name: None),
                ("desk.runs.check_memory", lambda spec: None), ("desk.maintain.check_usable", lambda spec: []),
                ("desk.runs._gc_idle_in_background", lambda: None),
                ("desk.runs.qemu_img_overlay", lambda backing, overlay: overlay.write_bytes(b"")),
                ("desk.runs.reflink_copy", lambda src, dst: dst.write_bytes(b"")),
                ("desk.runs.prepare_domain_xml", lambda *a, **k: "<domain/>"), ("desk.runs.free_port", lambda: 1),
                ("desk.runs.guest.wait_ssh", held("waiting for the guest")),
                ("desk.runs.guest.wait_ready", held("waiting for the session")),
                ("desk.runs.images.provision", held("provisioning")), ("desk.steps.run_prepare", held("preparing"))):
            patch = mock.patch(target, value)
            patch.start()
            self.addCleanup(patch.stop)

    def test_lock_given_back_once_the_domain_runs(self):
        from desk.config import load_image

        with mock.patch("sys.stdout"):
            run = runs.up(load_image("fedora-44-gnome"), run_id="t")
        self.assertEqual(self.events, [("domain started", True), ("waiting for the guest", False),
                                       ("waiting for the session", False), ("provisioning", False),
                                       ("preparing", False)])
        self.assertTrue(run.ready)
        self.assertTrue(run.in_use())  # held by this process, so gc leaves it to it
        self.assertFalse(runs._locked)

    def test_other_sizes_than_the_warm_states_boot_cold(self):
        from dataclasses import replace

        from desk.config import load_image

        spec = load_image("fedora-44-gnome")
        warm = self.home / "warm" / "fedora-44-gnome"
        warm.mkdir(parents=True)
        (warm / "memory.save").write_bytes(b"")
        (warm / "meta.json").write_text(json.dumps({"base": str((self.home / "base" / "fedora-44-gnome").resolve()),
                                                    "timezone": "UTC", "layout": "us"}))
        self.conn.saveImageGetXMLDesc.return_value = ("<domain><name>desk-warm-x</name><uuid>u</uuid>"
                                                      "<memory unit='KiB'>4194304</memory><vcpu>4</vcpu></domain>")
        prepared = mock.Mock(return_value="<domain/>")
        with mock.patch("desk.runs.uuid_active", return_value=False), mock.patch("desk.runs.prepare_domain_xml", prepared), \
                mock.patch("sys.stdout") as out:
            runs.up(replace(spec, memory_mib=8192), run_id="t")
        self.assertIn("booting cold: the warm state has 4096 MiB and 4 vCPUs, this run 8192 MiB and 4",
                      "".join(c.args[0] for c in out.write.call_args_list))
        self.conn.restoreFlags.assert_not_called()
        self.assertEqual(self.events[0], ("domain started", True))
        self.assertEqual((prepared.call_args.kwargs["memory_mib"], prepared.call_args.kwargs["vcpus"]), (8192, 4))

    def test_shared_memory_boots_cold_whatever_warm_state_there_is(self):
        from desk.config import load_image

        spec = load_image("fedora-44-gnome")
        warm = self.home / "warm" / "fedora-44-gnome"
        warm.mkdir(parents=True)
        (warm / "memory.save").write_bytes(b"")
        prepared = mock.Mock(return_value="<domain/>")
        with mock.patch("desk.runs.prepare_domain_xml", prepared), mock.patch("sys.stdout") as out:
            runs.up(spec, run_id="t", shared_memory=True)
        self.assertIn("booting cold: the warm state's RAM is not shared memory",
                      "".join(c.args[0] for c in out.write.call_args_list))
        self.conn.restoreFlags.assert_not_called()
        self.assertTrue(prepared.call_args.kwargs["shared_memory"])
        self.assertTrue(runs.load("t").shared_memory)

    def test_the_usbvfiod_controller_boots_cold_with_shared_memory_and_comes_after_the_guest_is_ready(self):
        from desk.config import load_image

        spec = load_image("fedora-44-gnome")
        warm = self.home / "warm" / "fedora-44-gnome"
        warm.mkdir(parents=True)
        (warm / "memory.save").write_bytes(b"")
        prepared = mock.Mock(return_value="<domain/>")
        held = lambda what: lambda *a, **k: self.events.append((what, False))  # noqa: E731
        with mock.patch("desk.runs.prepare_domain_xml", prepared), mock.patch("sys.stdout") as out, \
                mock.patch("desk.usbvfiod.start", side_effect=held("server started")) as start, \
                mock.patch("desk.usbvfiod.attach_controller", side_effect=held("controller added")) as add:
            run = runs.up(spec, run_id="t", usbvfiod=True)
        self.assertEqual([e for e, _ in self.events],
                         ["server started", "domain started", "waiting for the guest", "waiting for the session",
                          "provisioning", "preparing", "controller added"])
        start.assert_called_once_with(run)
        self.assertEqual(add.call_args.args[0], run)
        self.assertIn("booting cold: the warm state's RAM is not shared memory, which the usbvfiod controller needs",
                      "".join(c.args[0] for c in out.write.call_args_list))
        self.conn.restoreFlags.assert_not_called()
        self.assertTrue(prepared.call_args.kwargs["shared_memory"])
        self.assertTrue(runs.load("t").shared_memory)

    def test_a_run_without_the_controller_never_touches_usbvfiod(self):
        from desk.config import load_image

        with mock.patch("sys.stdout"), mock.patch("desk.usbvfiod.start") as start, \
                mock.patch("desk.usbvfiod.attach_controller") as add:
            runs.up(load_image("fedora-44-gnome"), run_id="t")
        start.assert_not_called()
        add.assert_not_called()

    @staticmethod
    def noted(run, then=None):
        """What usbvfiod.start does before it waits for the server: note it in run.json."""
        with run.edit() as r:
            r.usbvfiod = {"pid": 1, "vfio": "v", "hotplug": "h", "log": "l", "pcap": "p", "bus": ""}
        if then:
            raise then

    def test_a_server_that_does_not_start_leaves_no_domain_and_no_server(self):
        from desk.config import load_image

        with mock.patch("sys.stdout"), mock.patch("sys.stderr"), mock.patch(
                "desk.usbvfiod.start", side_effect=lambda run: self.noted(run, DeskError("usbvfiod ended"))), \
                mock.patch("desk.usbvfiod.alive", return_value=False), \
                mock.patch("desk.usbvfiod.stop") as stop, self.assertRaisesRegex(DeskError, "usbvfiod ended"):
            runs.up(load_image("fedora-44-gnome"), run_id="t", usbvfiod=True)
        self.conn.createXML.assert_not_called()
        stop.assert_called_once()
        self.assertTrue((self.home / "runs" / "t").exists())  # kept, with the log

    def test_a_controller_that_cannot_be_added_takes_the_run_down_and_stops_the_server_after_the_domain(self):
        from desk.config import load_image

        order = []
        dom = mock.Mock()
        dom.isActive.return_value = True
        dom.XMLDesc.return_value = str(self.home / "runs" / "t" / "disk.qcow2")
        dom.destroy.side_effect = lambda: order.append("domain destroyed")
        self.conn.createXML.return_value = dom
        with mock.patch("sys.stdout"), mock.patch("sys.stderr"), mock.patch("desk.runs.lookup", return_value=dom), \
                mock.patch("desk.usbvfiod.start", side_effect=self.noted), \
                mock.patch("desk.usbvfiod.attach_controller", side_effect=DeskError("no PCIe port")), \
                mock.patch("desk.display.screenshot", side_effect=DeskError("no screen")), \
                mock.patch("desk.usbvfiod.stop", side_effect=lambda run: order.append("server stopped")), \
                mock.patch("desk.usbvfiod.alive", return_value=True), \
                mock.patch("desk.runs.Run.live_domain", return_value=dom), \
                self.assertRaisesRegex(DeskError, "no PCIe port"):
            runs.up(load_image("fedora-44-gnome"), run_id="t", usbvfiod=True)
        self.assertEqual(order, ["domain destroyed", "server stopped"])

    def test_a_run_that_provisions_keeps_the_images_zone_and_layout(self):
        from dataclasses import replace

        from desk.config import load_image

        with mock.patch("sys.stdout"):
            run = runs.up(replace(load_image("fedora-44-gnome"), timezone="Asia/Kolkata", layout="pl"), run_id="t")
        self.assertIn(("provisioning", False), self.events)
        self.assertEqual((runs.load("t").timezone, runs.load("t").layout), ("Asia/Kolkata", "pl"))
        self.assertEqual(run.timezone, "Asia/Kolkata")

    def test_a_run_from_a_warm_build_keeps_the_zone_and_layout_it_was_made_with(self):
        from dataclasses import replace

        from desk import images
        from desk.config import load_image

        warm = self.home / "warm" / "fedora-44-gnome"
        warm.mkdir(parents=True)
        (warm / images.PROVISIONED).write_bytes(b"")
        (warm / "VARS.qcow2").write_bytes(b"")
        (warm / "meta.json").write_text(json.dumps({"base": str((self.home / "base" / "fedora-44-gnome").resolve()),
                                                    "timezone": "America/New_York", "layout": "us"}))
        # config.toml says otherwise now; the guest has what its build was provisioned with.
        spec = replace(load_image("fedora-44-gnome"), timezone="Asia/Kolkata", layout="pl")
        with mock.patch("sys.stdout"):
            runs.up(spec, run_id="t", cold=True)
        self.assertNotIn(("provisioning", False), self.events)
        self.assertEqual((runs.load("t").timezone, runs.load("t").layout), ("America/New_York", "us"))

    def test_a_warm_build_that_does_not_say_what_its_guest_has_is_provisioned_again(self):
        from desk import images
        from desk.config import load_image

        warm = self.home / "warm" / "fedora-44-gnome"
        warm.mkdir(parents=True)
        (warm / images.PROVISIONED).write_bytes(b"")
        (warm / "meta.json").write_text(json.dumps({"base": str((self.home / "base" / "fedora-44-gnome").resolve())}))
        with mock.patch("sys.stdout"):
            runs.up(load_image("fedora-44-gnome"), run_id="t", cold=True)
        self.assertIn(("provisioning", False), self.events)

    def test_a_start_that_fails_gives_the_lock_back_too(self):
        from desk.config import load_image

        with mock.patch("desk.runs.guest.wait_ssh", side_effect=runs.DeskError("SSH on port 1 not reachable")), \
                mock.patch("sys.stdout"), self.assertRaises(runs.DeskError):
            runs.up(load_image("fedora-44-gnome"), run_id="t")
        self.assertFalse(runs._locked)
        with runs.state_lock():
            pass


class Memory(unittest.TestCase):
    """Guests that are still booting have not taken the memory they will take."""

    def booting(self, run_id: str, domain: str, ready: float, max_mib: int, rss_mib: int):
        dom = mock.Mock()
        dom.maxMemory.return_value = max_mib * 1024
        dom.memoryStats.return_value = {"rss": rss_mib * 1024}
        return mock.Mock(id=run_id, domain=domain, ready=ready, live_domain=lambda: dom)

    def test_promised(self):
        fleet = [
            self.booting("a", "desk-a", 0.0, 6144, 1024),                    # booting cold: 5120 to come
            self.booting("b", "desk-b", 1.0, 4096, 2048),                    # handed over: as MemAvailable has it
            self.booting("c", "desk-warm-fedora-44-gnome-1", 0.0, 4096, 512),  # restored: its memory is loaded
            mock.Mock(id="d", domain="desk-d", ready=0.0, live_domain=lambda: None),  # a start that failed
        ]
        with mock.patch("desk.runs.all_runs", return_value=fleet):
            self.assertEqual(runs._promised_mib(), 5120)

    def test_a_second_cold_boot_does_not_count_the_firsts_memory_as_free(self):
        from desk.config import load_image

        spec = load_image("fedora-44-gnome")
        need = spec.memory_mib + runs.HOST_RESERVE_MIB
        first = [self.booting("a", "desk-a", 0.0, spec.memory_mib, 200)]
        with mock.patch("desk.runs._mem_available_mib", return_value=need + 500):
            with mock.patch("desk.runs.all_runs", return_value=[]):
                runs.check_memory(spec)
            with mock.patch("desk.runs.all_runs", return_value=first), self.assertRaisesRegex(runs.DeskError, "memory"):
                runs.check_memory(spec)


class Locks(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / ".lock"
        patch = mock.patch("desk.runs.HOME", Path(tmp.name))
        patch.start()
        self.addCleanup(patch.stop)

    def test_held_again_by_what_the_holder_calls(self):
        with runs._flock(self.path, "the test", 1):
            self.assertIn("pid", self.path.read_text())
            with runs._flock(self.path, "the test", 1):
                pass
            self.assertEqual(runs._locked[self.path], 1)
        self.assertNotIn(self.path, runs._locked)
        self.assertEqual(self.path.read_text(), "")

    def test_waiting_ends_and_names_the_holder(self):
        import fcntl

        with open(self.path, "a+") as other:  # another open file: as another process holds it
            fcntl.flock(other, fcntl.LOCK_EX)
            other.write("desk up windows-11 (pid 1, since 03:00:00)")
            other.flush()
            with mock.patch("sys.stderr"), self.assertRaisesRegex(runs.DeskError, r"held by desk up windows-11 \(pid 1"):
                with runs._flock(self.path, "the test", 0.2):
                    self.fail("got a lock somebody else holds")
        with runs._flock(self.path, "the test", 1):
            pass


class PruneVersions(unittest.TestCase):
    def test_nothing_is_deleted_while_a_run_disk_cannot_be_read(self):
        def unreachable():
            raise AssertionError("went on to look for builds to delete")

        with tempfile.TemporaryDirectory() as tmp:
            disk = Path(tmp) / "runs" / "kept" / "disk.qcow2"
            disk.parent.mkdir(parents=True)
            disk.write_bytes(b"")
            # As when qemu-img cannot read it (half deleted, qemu-img itself failing).
            with mock.patch("desk.runs.RUNS", Path(tmp) / "runs"), mock.patch("desk.runs.conn", unreachable), \
                    mock.patch("desk.virt.backing_files", return_value=[]), mock.patch("sys.stderr"):
                self.assertEqual(runs.prune_versions(verbose=False), [])

    def test_the_base_under_a_warm_up_that_runs_stays(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            old, new = (home / "base" / f"fedora-44-gnome.2026100{i}-000000" for i in (1, 2))
            warming = home / "warm" / "fedora-44-gnome-pl.20261003-000000"
            for d in (old, new, warming):
                d.mkdir(parents=True)
                (d / "disk.qcow2").write_bytes(b"")
            (home / "base" / "fedora-44-gnome").symlink_to(new)  # rebuilt while the variant warms up
            xml = f"<domain><devices><disk><source file='{warming / 'disk.qcow2'}'/></disk></devices></domain>"
            chains = {warming / "disk.qcow2": [warming / "disk.qcow2", old / "disk.qcow2"]}
            domain = mock.Mock(XMLDesc=lambda _flags: xml)
            with mock.patch("desk.runs.RUNS", home / "runs"), mock.patch("desk.runs.HOME", home), \
                    mock.patch("desk.config.BASE", home / "base"), mock.patch("desk.config.WARM", home / "warm"), \
                    mock.patch("desk.config.list_images", return_value=["fedora-44-gnome"]), \
                    mock.patch("desk.runs.conn", return_value=mock.Mock(listAllDomains=lambda _f: [domain])), \
                    mock.patch("desk.virt.backing_files", side_effect=lambda f: chains.get(f, [f])):
                self.assertEqual(runs.prune_versions(verbose=False), [])
            self.assertTrue(old.exists())


class AbandonedWarmup(unittest.TestCase):
    def xml(self, disk: Path) -> str:
        return f"<domain><devices><disk device='disk'><source file='{disk}'/></disk></devices></domain>"

    def test_only_a_warm_up_guest_is_one(self):
        from desk.config import RUNS, WARM

        name = "desk-warm-openwrt-25.12-20261013-021139"
        warming = self.xml(WARM / "openwrt-25.12.20261013-021139" / "disk.qcow2")
        self.assertEqual(runs.abandoned_warmup(name, warming), "openwrt-25.12")
        # A run restored from that warm state has its name, and is somebody's.
        self.assertIsNone(runs.abandoned_warmup(name, self.xml(RUNS / "ab12cd" / "disk.qcow2")))
        self.assertIsNone(runs.abandoned_warmup("desk-ab12cd", warming))

    def test_so_is_the_guest_of_install_scripts(self):
        from desk.config import BASE, WARM

        name = "desk-install-arch-kde-20261025-020452"
        self.assertEqual(runs.abandoned_warmup(name, self.xml(BASE / "arch-kde.20261025-020452" / "disk.qcow2")),
                         "arch-kde")
        self.assertIsNone(runs.abandoned_warmup(name, self.xml(WARM / "arch-kde.20261025-020452" / "disk.qcow2")))


if __name__ == "__main__":
    unittest.main()
