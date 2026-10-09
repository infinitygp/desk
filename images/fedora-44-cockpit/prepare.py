"""Log in to Cockpit in Firefox and turn on administrative access, as a user would. Also brings
a run back to that state later (desk prepare), e.g. after a reboot: whatever of it is missing.
admin() alone turns on administrative access on whatever Cockpit page is open (desk prepare admin)."""

URL = "https://localhost:9090/"
ADMIN = ("Administrative access", "button")
# A cold boot starts Firefox and Cockpit for the first time, with the host busy with other runs.
FIRST_PAGE = 180


def prepare(s):
    # A running Firefox opens the address in its current tab (desk-firefox.js), like a link from
    # another program; typing it into the address bar raced with the page taking the focus.
    running = s.running("firefox")
    s.gui("firefox", URL)
    if running:
        s.focus("Firefox")
    if s.first(("User name", "entry"), ADMIN, ("Limited access", "button"), timeout=FIRST_PAGE) == 0:
        s.click("User name", role="entry")
        s.type("tester")
        s.click("Password", role="password text")
        s.type("tester")
        s.key("ret")
    admin(s)


def admin(s):
    if s.first(ADMIN, ("Limited access", "button"), ("Turn on administrative access", "button")) == 0:
        return
    s.click("Limited access", role="button")
    for _ in range(3):
        # The field is named after sudo's prompt once sudo asks; typing earlier is lost when sudo
        # starts over with "Sorry, try again".
        s.wait_for("Password", role="password text", exact=False)
        s.click("Password", role="password text", exact=False)
        s.type("tester")
        s.click("Authenticate", role="button")
        s.settle()
        if s.first(ADMIN, ("Sorry", None), exact=False) == 0:
            return
    raise RuntimeError("Cockpit did not grant administrative access after three attempts")
