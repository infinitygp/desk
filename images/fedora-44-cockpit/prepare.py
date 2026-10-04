"""Log in to Cockpit in Firefox and turn on administrative access, as a user would."""


def prepare(s):
    s.gui("firefox", "https://localhost:9090/")
    s.click("User name", role="entry")
    s.type("tester")
    s.click("Password", role="password text")
    s.type("tester")
    s.key("ret")
    s.click("Turn on administrative access", role="button")
    s.wait_for("Authenticate", role="button")
    s.type("tester")
    s.click("Authenticate", role="button")
    s.wait_for("Administrative access", role="button")
