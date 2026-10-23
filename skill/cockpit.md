# desk: Cockpit plugins

Part of the desk skill ([SKILL.md](SKILL.md)); the commands and pitfalls there apply.

## Cockpit plugins

Use `fedora-44-cockpit` to test what the plugin does: Firefox shows Cockpit at
`https://localhost:9090`, logged in with administrative access. Install the plugin's package with
`desk exec --root -- dnf install -y /tmp/foo.rpm`, then press F5 in Firefox: the new entry appears
in Cockpit's menu without logging in again. After `desk reboot` (or when Cockpit asks to log in
again) run `desk prepare`, in a scenario `prepare = true`: it opens Firefox at Cockpit, logs in and
turns on administrative access, whichever of that is missing. To get administrative access back
in the middle of a test (after limiting it, or on a page that asks for it) without leaving the page:
`desk prepare admin`, in a scenario `prepare = "admin"`; it waits for sudo's password prompt and
types the password again when sudo answers "Sorry, try again"; a sudo dialog the plugin itself
opened is used as it is. `desk browser open /podman` opens a Cockpit page (a path is taken on the
site shown now; a whole address needs `https://`) without typing into the address bar;
`/example#/tasks` reaches a plugin's own page, also from another page of the same plugin (Cockpit
loads afresh then). Cockpit ends its session when none of its pages is open for a few seconds:
after `desk browser open` of another site, or a tab left on an error page, it asks to log in
again (`desk prepare`).
`desk reboot --prepare` reboots and logs in again in one go. Test packaging and dependencies on `fedora-44-gnome`
instead, where Cockpit is not installed, as on a fresh Workstation: a plugin that depends only on
`cockpit-bridge` installs there without the web console a user needs to reach it.

## Cockpit on phones

- `desk browser viewport 390x700` gives pages 390x700 CSS pixels through Firefox's responsive
  design mode (Firefox's window cannot get that narrow) and prints the `desk shot --region` of the
  page area; `desk browser viewport reset` closes the mode. Cockpit collapses its menu at that
  width, so the plugin's iframe gets the whole width. The screen is 800 pixels high: taller
  viewports run past its bottom (`desk display --resolution` to a taller mode the monitor offers).
- The plugin's iframe is same-origin, so the web console (`desk key ctrl+shift+k`) can measure it:
  `[...document.querySelectorAll('iframe')].find(i => i.src.includes('<plugin>')).contentDocument`.
- Desktop regression check: the same crop before and after a change, compared on the host with
  `magick compare -fuzz 2%`.
- A browser on the host: `desk forward 9090` prints the local address of the guest's Cockpit (its
  certificate comes from the image's own CA, so accept it). Clicking "Turn on administrative access"
  does nothing in a headless browser; set localStorage `superuser:tester`=`sudo` and
  `superuser-key`=`superuser:tester` on the login page before logging in.
