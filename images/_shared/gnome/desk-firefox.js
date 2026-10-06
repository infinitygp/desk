// Installed by desk into Firefox's default preferences: the web console of every page goes to
// Firefox's output, which lands in the journal (desk browser console, the report).
// console.* calls:
pref("devtools.console.stdout.content", true);
// uncaught exceptions and rejected promises ("JavaScript error: URL, line N: ..."):
pref("browser.dom.window.dump.enabled", true);
// No hidden new-tab page loading in the background with console output of its own.
pref("browser.newtab.preload", false);
