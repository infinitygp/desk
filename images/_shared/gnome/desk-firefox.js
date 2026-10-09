// Installed by desk into Firefox's default preferences: the web console of every page goes to
// Firefox's output, which lands in the journal (desk browser console, the report).
// console.* calls:
pref("devtools.console.stdout.content", true);
// uncaught exceptions and rejected promises ("JavaScript error: URL, line N: ..."):
pref("browser.dom.window.dump.enabled", true);
// No hidden new-tab page loading in the background with console output of its own.
pref("browser.newtab.preload", false);
// An address opened from outside (`firefox URL`: desk browser open, prepare scripts) replaces the
// page in the current tab, as typing it would, instead of piling up tabs.
pref("browser.link.open_newwindow.override.external", 1);
// Promises remember where they were rejected also without the devtools open, so an uncaught
// rejection with a plain object ("uncaught exception: Object") names its file and line.
pref("javascript.options.asyncstack_capture_debuggee_only", false);
