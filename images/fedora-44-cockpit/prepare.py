"""Log in to Cockpit in Firefox and turn on administrative access, as a user would. Also brings
a run back to that state later (desk prepare), e.g. after a reboot: whatever of it is missing.
admin() alone turns on administrative access on whatever Cockpit page is open (desk prepare admin)."""

URL = "https://localhost:9090/"
# Cockpit's names in English and in Polish, which a test may have switched it to (Session, Display
# language); from /usr/share/cockpit/{static,shell}/po.pl.js.
NAMES = [
    {"user": "User name", "password": "Password", "admin": "Administrative access", "limited": "Limited access",
     "turn on": "Turn on administrative access", "authenticate": "Authenticate"},
    {"user": "Nazwa użytkownika", "password": "Hasło", "admin": "Dostęp administracyjny",
     "limited": "Ograniczony dostęp", "turn on": "Włącz dostęp administracyjny", "authenticate": "Uwierzytelnij"},
]
# sudo's prompt and complaint, in the system's language rather than Cockpit's (sudoers.mo).
SUDO_PASSWORD = ("Password", "Hasło")
SUDO_SORRY = ("Sorry", "Niestety")
# A cold boot starts Firefox and Cockpit for the first time, with the host busy with other runs.
FIRST_PAGE = 180


def _first(s, *wanted, timeout=60):
    """The first of these (key, role) on screen in either language: the key, and the names of that
    language."""
    found = [(names, key, role) for key, role in wanted for names in NAMES]
    names, key, _ = found[s.first(*[(names[key], role) for names, key, role in found], timeout=timeout)]
    return key, names


def prepare(s):
    # A running Firefox opens the address in its current tab (desk-firefox.js), like a link from
    # another program; typing it into the address bar raced with the page taking the focus.
    running = s.running("firefox")
    s.gui("firefox", URL)
    if running:
        s.focus("Firefox")
    key, names = _first(s, ("user", "entry"), ("admin", "button"), ("limited", "button"), timeout=FIRST_PAGE)
    if key == "user":
        s.click(names["user"], role="entry")
        s.type("tester")
        s.click(names["password"], role="password text")
        s.type("tester")
        s.key("ret")
    admin(s)


def admin(s):
    # The dialog may be open already: a page asks for administrative access with a button of its own.
    key, names = _first(s, ("admin", "button"), ("limited", "button"), ("turn on", "button"),
                        ("authenticate", "button"))
    if key == "admin":
        return
    if key != "authenticate":
        s.click(names[key], role="button")
    for _ in range(3):
        # The field is named after sudo's prompt once sudo asks; typing earlier is lost when sudo
        # starts over with "Sorry, try again".
        field = SUDO_PASSWORD[s.first(*[(p, "password text") for p in SUDO_PASSWORD], exact=False)]
        s.click(field, role="password text", exact=False)
        s.type("tester")
        s.click(names["authenticate"], role="button")
        s.settle()
        if s.first((names["admin"], "button"), *[(w, None) for w in SUDO_SORRY], exact=False) == 0:
            return
    raise RuntimeError("Cockpit did not grant administrative access after three attempts")
