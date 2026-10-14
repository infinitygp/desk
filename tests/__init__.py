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
