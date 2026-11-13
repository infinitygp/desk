import shutil
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from desk import config
from desk.config import CONFIG_DIR, IMAGES, IMAGES_DIR, DeskError, list_images, load_image


class HostFiles(unittest.TestCase):
    """A test's own image files and blocks of the host, gone afterwards."""

    def images(self) -> Path:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        patch = mock.patch("desk.config.IMAGES", tmp)
        patch.start()
        self.addCleanup(patch.stop)
        return tmp

    def addon(self, name: str, toml: str, **files: str) -> Path:
        return self.block("addons", name, toml, **files)

    def block(self, kind: str, name: str, toml: str, **files: str) -> Path:
        d = CONFIG_DIR / kind / name
        d.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, d)
        for f, text in {"block.toml": toml, **files}.items():
            (d / f).write_text(text)
        return d


class Composition(HostFiles):
    def test_an_image_is_its_blocks(self):
        spec = load_image("fedora-44-gnome")
        self.assertEqual((spec.installer, spec.desktop, spec.dir), ("kickstart", "gnome", IMAGES_DIR / "distros" / "fedora"))
        self.assertEqual(spec.distro, "fedora-44")
        self.assertEqual(spec.provision_scripts, [str(IMAGES_DIR / "shared" / "desktop.sh"),
                                                  str(IMAGES_DIR / "desktops" / "gnome" / "provision.sh")])
        shared, files = spec.files
        self.assertTrue((spec.path(shared) / "desk-firefox.js").is_file())
        self.assertTrue((spec.path(files) / "desk-helper@desk.local" / "extension.js").is_file())
        self.assertTrue(spec.path(spec.kickstart).is_file())
        self.assertEqual(load_image("windows-11").desktop, "windows")
        self.assertEqual(load_image("openwrt-25.12").desktop, "")

    def test_a_variant_runs_on_the_install_without_its_addons(self):
        base, ck = load_image("fedora-44-gnome"), load_image("fedora-44-gnome-cockpit")
        self.assertEqual(ck.base, "fedora-44-gnome")
        self.assertIsNone(base.base)
        self.assertEqual((ck.base_disk, ck.base_xml), (base.base_disk, base.base_xml))
        self.assertNotEqual(ck.warm_dir, base.warm_dir)
        cockpit = IMAGES_DIR / "addons" / "cockpit"
        self.assertEqual(ck.provision_scripts, [*base.provision_scripts, str(cockpit / "provision.sh")])
        self.assertEqual(ck.path(ck.prepare), cockpit / "prepare.py")

    def test_builds_are_versioned(self):
        from dataclasses import replace

        from desk.config import BASE, WARM

        ck = load_image("fedora-44-gnome-cockpit")
        built = replace(ck, base_version="20261005-030000", warm_version="20261005-040000")
        self.assertEqual(built.base_disk, BASE / "fedora-44-gnome.20261005-030000" / "disk.qcow2")
        self.assertEqual(built.warm_dir, WARM / "fedora-44-gnome-cockpit.20261005-040000")

    def test_a_name_is_its_blocks(self):
        for name in list_images():
            with self.subTest(name=name):
                self.assertEqual(config.image_name(*config.parse_name(name)), name)
        self.assertEqual(config.parse_name("fedora-44-gnome-cockpit"), ("fedora-44", "gnome", ["cockpit"]))
        self.assertEqual(config.parse_name("openwrt-25.12"), ("openwrt-25.12", None, []))
        self.assertIsNone(config.parse_name("plan9-rio"))
        # Windows comes with its desktop: what follows its name is an addon.
        self.assertEqual(config.parse_name("windows-11-office"), ("windows-11", None, ["office"]))

    def test_an_image_not_defined_here_says_how_to_define_it(self):
        self.images()
        for name, said in (("fedora-44-gnome", "define it with: desk image add fedora-44 gnome$"),
                           ("fedora-44-gnome-cockpit", "desk image add fedora-44 gnome --addon cockpit$"),
                           ("windows-11", "desk image add windows-11$"),
                           ("arch-gnome-cockpit", "unknown image"),
                           ("fedora-44-twm", "unknown image"),
                           ("plan9", "unknown image")):
            with self.subTest(name=name), self.assertRaisesRegex(DeskError, said):
                load_image(name)

    def test_what_does_not_go_together_is_refused(self):
        self.images()
        for blocks, said in ((("windows-11", "gnome", []), "comes with its desktop"),
                             (("fedora-44", None, []), "needs one of the desktops gnome"),
                             (("fedora-44", "twm", []), "needs one of the desktops gnome"),
                             (("arch", "gnome", ["cockpit"]), "cockpit works on fedora-44, not on arch"),
                             (("fedora-44", "gnome", ["cockpit", "cockpit"]), "an addon named twice"),
                             (("plan9", None, []), "no distro 'plan9'")):
            with self.subTest(blocks=blocks), self.assertRaisesRegex(DeskError, said):
                config.add_image(*blocks, {})
        self.assertEqual(list_images(), [])


class Definitions(HostFiles):
    def test_add_set_rm(self):
        images = self.images()
        self.assertEqual(config.add_image("fedora-44", "gnome", ["cockpit"], {}), ["fedora-44-gnome", "fedora-44-gnome-cockpit"])
        with (images / "fedora-44-gnome-cockpit.toml").open("rb") as f:
            self.assertEqual(tomllib.load(f), {"distro": "fedora-44", "desktop": "gnome", "addons": ["cockpit"]})
        with self.assertRaisesRegex(DeskError, "defined already"):
            config.add_image("fedora-44", "gnome", [], {})
        config.set_image("fedora-44-gnome", {"memory_mib": 8192, "disk_gb": 60})
        ck = load_image("fedora-44-gnome-cockpit")
        self.assertEqual((ck.memory_mib, ck.disk_gb), (8192, 60))
        config.set_image("fedora-44-gnome-cockpit", {"memory_mib": 6144})
        self.assertEqual(load_image("fedora-44-gnome-cockpit").memory_mib, 6144)
        self.assertEqual(load_image("fedora-44-gnome").memory_mib, 8192)
        before = (images / "fedora-44-gnome-cockpit.toml").read_text()
        with self.assertRaisesRegex(DeskError, "runs on the install of fedora-44-gnome: set disk_gb there"):
            config.set_image("fedora-44-gnome-cockpit", {"disk_gb": 80})
        self.assertEqual((images / "fedora-44-gnome-cockpit.toml").read_text(), before)
        with self.assertRaisesRegex(DeskError, "fedora-44-gnome-cockpit runs on the install of fedora-44-gnome"):
            config.remove_image("fedora-44-gnome")
        config.remove_image("fedora-44-gnome-cockpit")
        config.remove_image("fedora-44-gnome")
        self.assertEqual(list_images(), [])
        with self.assertRaisesRegex(DeskError, "unknown image|not defined on this host"):
            config.remove_image("fedora-44-gnome")

    def test_rm_of_a_broken_definition(self):
        images = self.images()
        config.add_image("fedora-44", "gnome", [], {})
        # A variant whose addon was deleted still runs on its base's install.
        (images / "fedora-44-gnome-gone.toml").write_text('distro = "fedora-44"\ndesktop = "gnome"\naddons = ["gone"]\n')
        with self.assertRaisesRegex(DeskError, "fedora-44-gnome-gone runs on the install of fedora-44-gnome"):
            config.remove_image("fedora-44-gnome")
        config.remove_image("fedora-44-gnome-gone")
        # A variant with a value of another type, and one whose file is not TOML, likewise.
        config.add_image("fedora-44", "gnome", ["cockpit"], {})
        for text in ('distro = "fedora-44"\ndesktop = "gnome"\naddons = ["cockpit"]\nmemory_mib = "8G"\n', "distro = \n"):
            (images / "fedora-44-gnome-cockpit.toml").write_text(text)
            with self.assertRaisesRegex(DeskError, "fedora-44-gnome-cockpit runs on the install of fedora-44-gnome"):
                config.remove_image("fedora-44-gnome")
        config.remove_image("fedora-44-gnome-cockpit")
        # One that is an install of its own does not, broken or not.
        self.addon("yy", 'install = ["i.sh"]\n', **{"i.sh": "true\n"})
        config.add_image("fedora-44", "gnome", ["yy"], {})
        (images / "fedora-44-gnome-yy.toml").write_text('distro = "fedora-44"\ndesktop = "gnome"\naddons = ["yy"]\n'
                                                       'timezone = "Nowhere/Else"\n')
        (images / "x.toml").write_text("distro = \n")
        config.remove_image("x")
        config.remove_image("fedora-44-gnome")
        config.remove_image("fedora-44-gnome-yy")
        self.assertEqual(list_images(), [])

    def test_an_images_file_names_blocks_and_sets_sizes(self):
        images = self.images()
        (images / "fedora-44-gnome.toml").write_text('distro = "fedora-44"\ndesktop = "gnome"\nprovision = ["x.sh"]\n')
        with self.assertRaisesRegex(DeskError, "not provision"):
            load_image("fedora-44-gnome")
        (images / "fedora-44-gnome.toml").write_text('distro = "arch"\ndesktop = "gnome"\n')
        with self.assertRaisesRegex(DeskError, "make the image arch-gnome, so the file is arch-gnome.toml"):
            load_image("fedora-44-gnome")
        (images / "fedora-44-gnome.toml").write_text('distro = "fedora-44"\ndesktop = "gnome"\nwallpaper = "x"\n')
        with self.assertRaisesRegex(DeskError, "wallpaper"):
            load_image("fedora-44-gnome")
        for text, why in (('desktop = 5\n', "desktop is the name of a block"),
                          ('desktop = "gnome"\naddons = "cockpit"\n', "addons a list of names"),
                          ('desktop = "gnome"\nmemory_mib = "lots"\n', 'memory_mib = "lots" is not int'),
                          ('desktop = "gnome"\nvcpus = 2.5\n', "vcpus = 2.5 is not int"),
                          ('desktop = "gnome"\nvcpus = true\n', "vcpus = true is not int"),
                          ('desktop = "gnome"\nrebuild_days = "7"\n', 'rebuild_days = "7" is not int | None')):
            (images / "fedora-44-gnome.toml").write_text(f'distro = "fedora-44"\n{text}')
            with self.assertRaisesRegex(DeskError, why):
                load_image("fedora-44-gnome")
        (images / "fedora-44-gnome.toml").write_text('distro = "fedora-44"\ndesktop = "gnome"\nrebuild_days = 7\n'
                                                     'secure_boot = false\nlayout = "pl"\n')
        self.assertEqual(load_image("fedora-44-gnome").rebuild_days, 7)

    def test_what_it_writes_reads_back(self):
        self.images()
        settings = {"memory_mib": 4096, "secure_boot": False, "ready": 'test "$(id -u)" = 1000 \\ ż'}
        f = config.write_image("x", {"distro": "fedora-44", "desktop": None, "addons": [], **settings})
        with f.open("rb") as fh:
            self.assertEqual(tomllib.load(fh), {"distro": "fedora-44", **settings})

    def test_definitions_of_an_older_desk_are_refused(self):
        images = self.images()
        (images / "fedora-44-gnome-pl").mkdir()
        (images / "fedora-44-gnome-pl" / "image.toml").write_text('base = "fedora-44-gnome"\n')
        with self.assertRaisesRegex(DeskError, "definition of an older desk"):
            list_images()


class DerivedDefinitions(HostFiles):
    """An image saved from a run (`desk image save`): a file that names the image it came from."""

    def derive(self, name: str, source: str) -> Path:
        f = self.images_dir / f"{name}.toml"
        f.write_text(f'derived_from = "{source}"\n')
        return f

    def setUp(self):
        self.images_dir = self.images()
        config.add_image("windows-11", None, [], {"memory_mib": 8192})
        config.add_image("fedora-44", "gnome", ["cockpit"], {})

    def test_it_has_the_settings_of_its_source_and_a_build_of_its_own(self):
        from desk.config import BASE, WARM

        self.derive("xlu", "windows-11")
        spec, source = load_image("xlu"), load_image("windows-11")
        self.assertEqual((spec.name, spec.derived_from, spec.memory_mib, spec.family, spec.dir),
                         ("xlu", "windows-11", 8192, "windows", source.dir))
        self.assertEqual((spec.base, spec.base_name, spec.base_dir, spec.warm_dir),
                         (None, "xlu", BASE / "xlu", WARM / "xlu"))
        self.assertEqual((spec.base_xml, spec.base_nvram), (BASE / "xlu" / "domain.xml", BASE / "xlu" / "VARS.qcow2"))
        self.assertEqual(spec.provision_scripts, source.provision_scripts)
        self.assertIsNone(source.derived_from)

    def test_a_variant_as_its_source_is_a_build_of_its_own_not_a_variant(self):
        self.derive("ck", "fedora-44-gnome-cockpit")
        spec = load_image("ck")
        self.assertEqual((spec.derived_from, spec.base, spec.base_name), ("fedora-44-gnome-cockpit", None, "ck"))
        self.assertEqual(spec.provision_scripts, load_image("fedora-44-gnome-cockpit").provision_scripts)
        self.assertIsNone(config.base_of("ck"))
        self.assertEqual(config.base_of("fedora-44-gnome-cockpit"), "fedora-44-gnome")

    def test_a_derived_image_can_be_the_source_of_another(self):
        self.derive("xlu", "windows-11")
        self.derive("xlu2", "xlu")
        spec = load_image("xlu2")
        self.assertEqual((spec.name, spec.derived_from, spec.memory_mib, spec.family),
                         ("xlu2", "xlu", 8192, "windows"))

    def test_it_is_listed_and_composition_does_not_apply_to_it(self):
        self.derive("xlu", "windows-11")
        # Its name looks like an image with an addon that is not there.
        self.derive("windows-11-xlu", "windows-11")
        self.assertEqual(list_images(), ["fedora-44-gnome", "fedora-44-gnome-cockpit", "windows-11", "windows-11-xlu",
                                         "xlu"])
        self.assertEqual(load_image("windows-11-xlu").derived_from, "windows-11")
        self.assertEqual((config.base_of("windows-11-xlu"), config.derived_from("windows-11-xlu")),
                         (None, "windows-11"))
        self.assertIsNone(config.derived_from("windows-11"))
        self.assertIsNone(config.derived_from("missing"))

    def test_the_file_holds_the_source_and_nothing_else(self):
        f = self.derive("xlu", "windows-11")
        for text, said in (('derived_from = "windows-11"\nmemory_mib = 4096\n', "holds derived_from"),
                           ("derived_from = 5\n", "holds derived_from"),
                           ('derived_from = "xlu"\n', "xlu -> xlu derives from itself")):
            f.write_text(text)
            with self.subTest(text=text), self.assertRaisesRegex(DeskError, said):
                load_image("xlu")
        self.derive("a", "b")
        self.derive("b", "a")
        with self.assertRaisesRegex(DeskError, "a -> b -> a derives from itself"):
            load_image("a")

    def test_it_needs_the_definition_of_its_source_not_its_builds(self):
        self.derive("xlu", "windows-11")
        config.remove_image("windows-11")  # allowed: the disk of xlu is whole
        with self.assertRaisesRegex(DeskError, r"xlu\.toml: derived from windows-11, whose definition cannot be "
                                               r"loaded \(image 'windows-11' is not defined.*desk image add windows-11"):
            load_image("xlu")
        # It can be removed all the same, and it is no variant of anything its name looks like.
        self.derive("windows-11-xlu", "windows-11")
        self.assertIsNone(config.base_of("windows-11-xlu"))
        config.remove_image("windows-11-xlu")
        config.remove_image("xlu")
        self.assertEqual(list_images(), ["fedora-44-gnome", "fedora-44-gnome-cockpit"])

    def test_a_source_can_be_removed_while_derived_images_name_it(self):
        self.derive("xlu", "windows-11")
        config.remove_image("windows-11")
        self.assertEqual(list_images(), ["fedora-44-gnome", "fedora-44-gnome-cockpit", "xlu"])

    def test_it_is_not_changed_with_set_or_defined_again_with_add(self):
        f = self.derive("xlu", "windows-11")
        with self.assertRaisesRegex(DeskError, "xlu is derived from windows-11 and has its settings; change them"):
            config.set_image("xlu", {"memory_mib": 4096})
        self.assertEqual(f.read_text(), 'derived_from = "windows-11"\n')
        # An image of blocks whose file is a derived one (written by hand): neither is it replaced.
        self.derive("windows-11", "xlu")
        with self.assertRaisesRegex(DeskError, "windows-11 is defined already.*derived from xlu, and desk image rm"):
            config.add_image("windows-11", None, [], {})

    def test_what_desk_writes_for_it_reads_back(self):
        f = config.write_image("xlu", {"derived_from": "windows-11"})
        with f.open("rb") as fh:
            self.assertEqual(tomllib.load(fh), {"derived_from": "windows-11"})
        self.assertEqual(load_image("xlu").derived_from, "windows-11")


class HostBlocks(HostFiles):
    """Blocks of the host's own, beside config.toml."""

    def test_an_addon_of_the_hosts_own(self):
        d = self.addon("xx", '# Another language.\ndistros = ["fedora-44"]\nprovision = "provision.sh"\n',
                       **{"provision.sh": "true\n"})
        self.images()
        config.add_image("fedora-44", "gnome", ["xx"], {})
        xx = load_image("fedora-44-gnome-xx")
        self.assertEqual(xx.base, "fedora-44-gnome")
        self.assertEqual(xx.provision_scripts[-1], str(d / "provision.sh"))
        self.assertEqual(config.describe(d), "Another language.")

    def test_a_variants_addons_win_over_its_bases_file(self):
        self.addon("xx", 'memory_mib = 8192\nlayout = "pl"\n')
        self.images()
        config.add_image("fedora-44", "gnome", ["xx"], {})
        config.set_image("fedora-44-gnome", {"memory_mib": 6144, "vcpus": 3, "disk_gb": 60})
        config.write_image("fedora-44-gnome", {**config.read_image("fedora-44-gnome"), "layout": "us"})
        xx = load_image("fedora-44-gnome-xx")
        self.assertEqual((xx.memory_mib, xx.layout, xx.vcpus, xx.disk_gb), (8192, "pl", 3, 60))
        config.set_image("fedora-44-gnome-xx", {"memory_mib": 4096})
        self.assertEqual(load_image("fedora-44-gnome-xx").memory_mib, 4096)

    def test_an_addon_that_installs_is_an_install_of_its_own(self):
        self.addon("xx", 'install = ["install.sh"]\n', **{"install.sh": "true\n"})
        self.images()
        self.assertEqual(config.add_image("fedora-44", "gnome", ["xx"], {}), ["fedora-44-gnome-xx"])
        xx = load_image("fedora-44-gnome-xx")
        self.assertIsNone(xx.base)
        self.assertEqual(xx.install[-1], str(CONFIG_DIR / "addons" / "xx" / "install.sh"))

    def test_a_name_defined_twice_is_refused(self):
        self.addon("cockpit", "")
        with self.assertRaisesRegex(DeskError, "addon 'cockpit' is defined twice"):
            load_image("fedora-44-gnome-cockpit")
        self.block("desktops", "gnome", "")
        with self.assertRaisesRegex(DeskError, "desktop 'gnome' is defined twice"):
            load_image("fedora-44-gnome")
        self.assertEqual(load_image("windows-11").name, "windows-11")

    def test_a_name_that_cannot_be_read_back_fails_only_its_images(self):
        self.addon("my-lang", "")
        self.block("distros", "arch-x", 'desktops = ["gnome"]\n')
        self.assertEqual(load_image("arch-gnome").name, "arch-gnome")
        self.assertEqual(config.parse_name("arch-x-gnome"), ("arch", "x", ["gnome"]))
        self.images()
        with self.assertRaisesRegex(DeskError, "my-lang: the name of a desktop or an addon has no '-'"):
            config.add_image("fedora-44", "gnome", ["my-lang"], {})
        with self.assertRaisesRegex(DeskError, "arch-x: the names of the images of arch-x and of arch can be"):
            config.add_image("arch-x", "gnome", [], {})

    def test_the_shipped_blocks_are_all_usable(self):
        for kind in config.KINDS:
            self.assertEqual(config.scan_blocks(kind)[1], {}, kind)

    def test_a_file_that_is_not_there_is_refused(self):
        self.addon("xx", 'provision = "missing.sh"\n')
        with self.assertRaisesRegex(DeskError, "missing.sh does not exist"):
            config.add_image("fedora-44", "gnome", ["xx"], {})

    def test_a_block_moved_to_the_host_keeps_its_builds(self):
        from desk import images

        shipped = load_image("fedora-44-gnome-cockpit")
        expected = images.install_fingerprint(shipped), images.warm_fingerprint(shipped)
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        shutil.copytree(IMAGES_DIR, tmp / "shipped", ignore=shutil.ignore_patterns("cockpit"))
        shutil.copytree(IMAGES_DIR / "addons" / "cockpit", tmp / "own" / "addons" / "cockpit")
        shutil.copytree(IMAGES, tmp / "own" / "images")
        roots = (tmp / "shipped", tmp / "own")
        with mock.patch("desk.config.BLOCK_ROOTS", roots), mock.patch("desk.images.BLOCK_ROOTS", roots), \
                mock.patch("desk.config.IMAGES", tmp / "own" / "images"):
            moved = load_image("fedora-44-gnome-cockpit")
            self.assertEqual(moved.path(moved.prepare), tmp / "own" / "addons" / "cockpit" / "prepare.py")
            self.assertEqual((images.install_fingerprint(moved), images.warm_fingerprint(moved)), expected)


class VersionedDistros(HostFiles):
    """One block per distribution, its versions in [versions]; image names keep the version."""

    PLAN9 = ('# Plan 9 from a test.\ninstaller = "cloudimg"\nos_variant = "plan9-{version}"\nuser_data = "seed"\n'
             'memory_mib = 1024\nvcpus = 1\ndisk_gb = 4\nuser = "glenda"\n'
             'install = ["all.sh", "only-{version}.sh"]\nready = "test -e {version} && echo {0}"\n'
             'desktops = ["gnome"]\n'
             '[versions.4]\ndesktops = ["gnome", "kde"]\nuser_data = "seed-4"\n'
             '[versions."4.1"]\n')

    def plan9(self) -> Path:
        return self.block("distros", "plan9", self.PLAN9, seed="", **{"seed-4": "", "all.sh": "", "only-4.sh": "",
                                                                     "only-4.1.sh": ""})

    def test_a_block_offers_its_versions_each_with_its_own_settings_over_the_blocks(self):
        d = self.plan9()
        found = config.blocks("distros")
        self.assertEqual((found["plan9-4"], found["plan9-4.1"]), (d, d))
        self.assertNotIn("plan9", found)
        four, four_one = config._block("distros", "plan9-4")[1], config._block("distros", "plan9-4.1")[1]
        # A list is replaced, a file of the version's is one of the block's.
        self.assertEqual((four["desktops"], four_one["desktops"]), (["gnome", "kde"], ["gnome"]))
        self.assertEqual((four["user_data"], four_one["user_data"]), (str(d / "seed-4"), str(d / "seed")))
        # {version} in strings and lists; other braces are left as they are.
        self.assertEqual((four["os_variant"], four_one["os_variant"]), ("plan9-4", "plan9-4.1"))
        self.assertEqual(four_one["install"], [str(d / "all.sh"), str(d / "only-4.1.sh")])
        self.assertEqual(four["ready"], "test -e 4 && echo {0}")
        self.assertNotIn("versions", four)
        self.assertEqual(config.parse_name("plan9-4.1-kde"), ("plan9-4.1", "kde", []))
        self.images()
        with self.assertRaisesRegex(DeskError, "plan9-4.1 needs one of the desktops gnome"):
            config.add_image("plan9-4.1", "kde", [], {})
        self.assertEqual(config.add_image("plan9-4", "kde", [], {}), ["plan9-4-kde"])
        self.assertEqual(config.read_image("plan9-4-kde")["distro"], "plan9-4")
        self.assertEqual((load_image("plan9-4-kde").dir, load_image("plan9-4-kde").distro), (d, "plan9-4"))

    def test_the_shipped_fedora_has_its_versions(self):
        f44, f45 = load_image("fedora-44-gnome"), load_image("fedora-45-gnome")
        # The image files name the distro as they did when each version was a block of its own.
        self.assertEqual(config.read_image("fedora-44-gnome")["distro"], "fedora-44")
        self.assertEqual((f44.release, f45.release), ("44", "45"))
        self.assertEqual(f44.location, "https://dl.fedoraproject.org/pub/fedora/linux/releases/44/Everything/x86_64/os/")
        self.assertIn("/releases/45/", f45.location)
        self.assertEqual(f44.kickstart, f45.kickstart)
        self.assertEqual(config.parse_name("fedora-45-gnome"), ("fedora-45", "gnome", []))
        self.assertEqual(config.add_command("fedora-45-gnome"), "desk image add fedora-45 gnome")
        self.assertIsNone(config.add_command("fedora-45-kde"))

    def test_the_blocks_name_or_a_version_not_tried_is_no_distro_to_build(self):
        self.images()
        with self.assertRaisesRegex(DeskError, "^fedora needs a version: fedora-44, fedora-45$"):
            config.add_image("fedora", "gnome", [], {})
        with self.assertRaisesRegex(DeskError, r"^fedora 46 was not tried: fedora has 44, 45 \(.*/images/distros/fedora/"
                                               r"block.toml, \[versions\]\)$"):
            config.add_image("fedora-46", "gnome", [], {})
        with self.assertRaisesRegex(DeskError, r"no distro 'fedorax' \(there are: .*fedora-44, fedora-45"):
            config.add_image("fedorax", None, [], {})
        self.assertEqual(list_images(), [])

    def test_an_addon_for_the_blocks_name_goes_on_every_version(self):
        self.addon("xx", 'distros = ["fedora"]\nprovision = "p.sh"\n', **{"p.sh": "true\n"})
        self.addon("yy", 'distros = ["fedora-44"]\nprovision = "p.sh"\n', **{"p.sh": "true\n"})
        self.images()
        config.add_image("fedora-44", "gnome", ["xx"], {})
        config.add_image("fedora-45", "gnome", ["xx"], {})
        self.assertEqual(load_image("fedora-45-gnome-xx").base, "fedora-45-gnome")
        with self.assertRaisesRegex(DeskError, "the addon xx works on fedora, not on arch"):
            config.add_image("arch", "gnome", ["xx"], {})
        with self.assertRaisesRegex(DeskError, "the addon yy works on fedora-44, not on fedora-45"):
            config.add_image("fedora-45", "gnome", ["yy"], {})

    def test_names_that_clash_are_refused(self):
        # A host's own block of a name a versioned one has: both, as for any name defined twice.
        self.block("distros", "fedora-44", 'desktops = ["gnome"]\n')
        for name in ("fedora-44-gnome", "fedora-44-gnome-cockpit"):
            with self.assertRaisesRegex(DeskError, r"distro 'fedora-44' is defined twice, in .*/images/distros/fedora "
                                                   r"and in .*/fedora-44: rename the host's own"):
                load_image(name)
        self.assertEqual(load_image("fedora-45-gnome").distro, "fedora-45")
        found, refused = config.scan_blocks("distros")
        self.assertNotIn("fedora-44", found)
        self.assertIn("fedora-44", refused)

    def test_a_versioned_block_defined_twice_is_refused_by_every_name(self):
        self.block("distros", "fedora", '[versions.44]\ndesktops = ["gnome"]\n[versions.46]\ndesktops = ["gnome"]\n')
        for name in ("fedora-44", "fedora-45", "fedora-46"):
            with self.assertRaisesRegex(DeskError, "distro 'fedora' is defined twice"):
                config._block("distros", name)
        self.assertEqual(load_image("arch-gnome").distro, "arch")

    def test_names_that_one_and_a_dash_begins_are_refused(self):
        self.block("distros", "fedora-44-x", 'desktops = ["gnome"]\n')
        self.block("distros", "plan9", '[versions.1]\n[versions.1-2]\n')
        found, refused = config.scan_blocks("distros")
        self.assertRegex(refused["fedora-44-x"], "the names of the images of fedora-44-x and of fedora-44 can be the same")
        self.assertRegex(refused["plan9-1-2"], "the names of the images of plan9-1-2 and of plan9-1 can be the same")
        self.assertNotIn("plan9-1", found)
        self.assertEqual(found["fedora-44"], IMAGES_DIR / "distros" / "fedora")
        # A block of a name of its own beside a versioned one stays a distro of its own.
        self.block("distros", "fedora-46", 'desktops = ["gnome"]\n')
        self.assertEqual(config.blocks("distros")["fedora-46"], CONFIG_DIR / "distros" / "fedora-46")

    def test_versions_that_are_no_tables_are_refused(self):
        self.block("distros", "plan9", 'versions = ["1"]\n')
        with self.assertRaisesRegex(DeskError, r"plan9/block.toml: versions holds a table for each version tried"):
            config._block("distros", "plan9")


class GuestSettings(HostFiles):
    """The guests' time zone and keyboard layout: the image's, else config.toml's, else the host's."""

    def test_the_hosts_zone_is_where_localtime_links(self):
        from desk.config import host_timezone

        with tempfile.TemporaryDirectory() as tmp:
            link = Path(tmp) / "localtime"
            link.symlink_to("../usr/share/zoneinfo/Asia/Kolkata")
            self.assertEqual(host_timezone(link), "Asia/Kolkata")
            link.unlink()
            link.symlink_to("/usr/share/zoneinfo/posix/Asia/Kolkata")
            self.assertEqual(host_timezone(link), "Asia/Kolkata")
            link.unlink()
            link.symlink_to("/usr/share/zoneinfo/right/Asia/Kolkata")
            self.assertIsNone(host_timezone(link))
            link.unlink()
            link.symlink_to("/usr/share/zoneinfo/Nowhere/Fake")
            self.assertIsNone(host_timezone(link))
            link.unlink()
            link.write_bytes(b"TZif2")  # a copy says nothing of its name
            self.assertIsNone(host_timezone(link))
            self.assertIsNone(host_timezone(Path(tmp) / "missing"))

    def test_images_get_the_hosts_unless_they_set_their_own(self):
        with mock.patch("desk.config.TIMEZONE", "America/New_York"), mock.patch("desk.config.LAYOUT", "pl"):
            spec = load_image("fedora-44-gnome")
            self.assertEqual((spec.timezone, spec.layout), ("America/New_York", "pl"))
            images = self.images()
            (images / "fedora-44-gnome.toml").write_text(
                'distro = "fedora-44"\ndesktop = "gnome"\ntimezone = "Asia/Kolkata"\nlayout = "us"\n')
            spec = load_image("fedora-44-gnome")
            self.assertEqual((spec.timezone, spec.layout), ("Asia/Kolkata", "us"))

    def test_what_desk_cannot_apply_is_refused(self):
        images = self.images()
        (images / "fedora-44-gnome.toml").write_text('distro = "fedora-44"\ndesktop = "gnome"\nlayout = "de"\n')
        with self.assertRaisesRegex(DeskError, "layout is one of us, pl"):
            load_image("fedora-44-gnome")
        (images / "fedora-44-gnome.toml").write_text('distro = "fedora-44"\ndesktop = "gnome"\ntimezone = "Mars/Olympus"\n')
        with self.assertRaisesRegex(DeskError, "no time zone this host knows"):
            load_image("fedora-44-gnome")

    def test_config_toml_with_what_desk_cannot_apply_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "config.toml"
            for text, said in (('layout = "de"\n', "layout is one of us, pl"),
                               ('timezone = "Mars/Olympus"\n', "no time zone this host knows"),
                               ('keyboard = "pl"\n', "it takes home")):
                f.write_text(text)
                with self.subTest(text=text), mock.patch("desk.config.CONFIG_FILE", f), \
                        mock.patch("sys.stderr") as err, self.assertRaises(SystemExit) as e:
                    config._local()
                self.assertEqual(e.exception.code, 2)
                self.assertIn(said, "".join(c.args[0] for c in err.write.call_args_list))
            f.write_text('timezone = "Asia/Kolkata"\nlayout = "pl"\n')
            with mock.patch("desk.config.CONFIG_FILE", f):
                self.assertEqual(config._local(), {"timezone": "Asia/Kolkata", "layout": "pl"})
class WindowsCommands(unittest.TestCase):
    def test_linux_commands_untouched(self):
        spec = load_image("fedora-44-gnome")
        self.assertEqual(spec.cmd("ls -l"), "ls -l")

    def test_windows_commands(self):
        import base64

        spec = load_image("windows-11")
        command = spec.cmd('Write-Output "Zażółć"')
        self.assertTrue(command.endswith("; exit $LASTEXITCODE"))
        encoded = command.split("-EncodedCommand ")[1].split(";")[0]
        self.assertIn('Write-Output "Zażółć"', base64.b64decode(encoded).decode("utf-16-le"))

    def test_windows_codes_that_do_not_fit_an_exit_status_fail(self):
        import base64

        # ssh would keep the last byte of -196608: 0, a success.
        command = load_image("windows-11").cmd("& powershell.exe -File missing.ps1")
        self.assertIn("if ($LASTEXITCODE -lt 0 -or $LASTEXITCODE -gt 255) { exit 1 }; exit $LASTEXITCODE", command)
        script = base64.b64decode(command.split("-EncodedCommand ")[1].split(";")[0]).decode("utf-16-le")
        self.assertIn("'desk: exit code {0} (0x{0:X8}) does not fit an exit status: exit 1' -f $LASTEXITCODE", script)

    def test_a_time_limit_is_kept_in_the_guest(self):
        import base64

        from desk import guest

        self.assertEqual(guest.command(load_image("fedora-44-gnome"), ["make check"], limit=90),
                         "DESK_CMD='make check' timeout -k 5 90 bash -c 'c=$DESK_CMD; unset DESK_CMD; eval \"$c\"'")
        self.assertIn("setsid sh -c", guest.command(load_image("openwrt-25.12"), ["apk update"], limit=90))
        command = guest.command(load_image("windows-11"), ["msiexec /i a.msi"], env={"WHO": "o'k"}, limit=90)
        script = base64.b64decode(command.split("-EncodedCommand ")[1].split(";")[0]).decode("utf-16-le")
        self.assertIn("$env:WHO = 'o''k'; msiexec /i a.msi", script)
        self.assertIn('"Start-Sleep 90; taskkill.exe /T /F /PID $PID"', script)
        self.assertIn("finally { Stop-Process -Id $deskWatch.Id", script)


if __name__ == "__main__":
    unittest.main()
