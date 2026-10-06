"""Log in to Cockpit in Firefox and turn on administrative access, as a user would. Also brings
a run back to that state later (desk prepare), e.g. after a reboot: whatever of it is missing."""

URL = "https://localhost:9090/"
ADMIN = ("Administrative access", "button")


def prepare(s):
    if s.running("firefox"):
        s.focus("Firefox")
        s.key("ctrl+l")
        s.type(URL)
        s.key("ret")
        s.settle()
    else:
        s.gui("firefox", URL)
    if s.first(("User name", "entry"), ADMIN, ("Limited access", "button")) == 0:
        s.click("User name", role="entry")
        s.type("tester")
        s.click("Password", role="password text")
        s.type("tester")
        s.key("ret")
    state = s.first(ADMIN, ("Turn on administrative access", "button"), ("Limited access", "button"))
    if state:
        s.click("Turn on administrative access" if state == 1 else "Limited access", role="button")
        s.wait_for("Authenticate", role="button")
        s.type("tester")
        s.click("Authenticate", role="button")
        s.wait_for(*ADMIN)
