"""The tests never touch the host's own desk files: its home and its config are a directory that
goes away with the test run. Set here, before anything imports desk.config."""

import atexit
import os
import shutil
import tempfile

_home = tempfile.mkdtemp(prefix="desk-tests-")
atexit.register(shutil.rmtree, _home, ignore_errors=True)
os.environ["DESK_HOME"] = os.path.join(_home, "home")
os.environ["XDG_CONFIG_HOME"] = os.path.join(_home, "config")
for name in ("DESK_URI", "DESK_RUN", "DESK_LABEL", "DESK_TTL", "DESK_DEBUG"):
    os.environ.pop(name, None)

# The images tests load: the shipped blocks put together as `desk image add` would.
_images = os.path.join(_home, "config", "desk", "images")
os.makedirs(_images)
for _name, _blocks in {"fedora-44-gnome": 'distro = "fedora-44"\ndesktop = "gnome"\n',
                       "fedora-44-gnome-cockpit": 'distro = "fedora-44"\ndesktop = "gnome"\naddons = ["cockpit"]\n',
                       "fedora-45-gnome": 'distro = "fedora-45"\ndesktop = "gnome"\n',
                       "arch-gnome": 'distro = "arch"\ndesktop = "gnome"\n',
                       "windows-11": 'distro = "windows-11"\n',
                       "openwrt-25.12": 'distro = "openwrt-25.12"\n'}.items():
    with open(os.path.join(_images, f"{_name}.toml"), "w") as _f:
        _f.write(_blocks)
