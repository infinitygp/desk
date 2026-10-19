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
        evidence.collect(old)  # desk down --keep wrote it then
        runs.gc_idle(verbose=False)
        self.assertFalse(old.dir.exists())
        self.assertEqual([p.name for p in (self.home / "reports").iterdir()], ["old"])

    def test_an_earlier_run_of_the_same_name_does_not_count_as_its_report(self):
        yesterday = self.home / "reports" / "old"
        yesterday.mkdir(parents=True)
        os.utime(yesterday, (time.time() - 86400,) * 2)
        self.run_idle_for("old", 3)
        self.assertRegex(runs.gc_idle(verbose=False)[0], "report: ")
        self.assertEqual(len(list((self.home / "reports").iterdir())), 2)

    def test_a_run_whose_owner_came_back_meanwhile_stays(self):
        old = self.run_idle_for("old", 3)

        def collect_slowly(run, diff=False):
            runs.load(run.id).touch()  # desk shot -r old, while the report is written
            return self.home / "reports" / run.id

        with mock.patch("desk.evidence.collect", collect_slowly):
            self.assertEqual(runs.gc_idle(verbose=False), [])
        self.assertTrue((old.dir / "run.json").exists())

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

        conn = mock.Mock()
        conn.createXML.side_effect = held("domain started")
        conn.listAllDomains.return_value = []
        for target, value in (
                ("desk.runs.RUNS", self.home / "runs"), ("desk.runs.HOME", self.home),
                ("desk.config.BASE", self.home / "base"), ("desk.config.WARM", self.home / "warm"),
                ("desk.runs.conn", lambda: conn), ("desk.runs.lookup", lambda name: None),
                ("desk.runs.check_memory", lambda spec: None), ("desk.maintain.check_usable", lambda spec: []),
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


if __name__ == "__main__":
    unittest.main()
