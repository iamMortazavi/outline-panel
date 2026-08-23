"""
Structural rules, enforced instead of remembered.

`MODERNIZATION.md` proposes moving the domain out of `web/routers/keys.py`. A
layering that is only a convention decays the first time someone is in a hurry,
so each rule below is a test. They are deliberately weak today and tighten as
the steps land — a rule that cannot pass yet is worse than no rule, because it
gets skipped and then deleted.
"""

import ast
import pathlib

SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "outline_panel"


def _imports(path: pathlib.Path) -> set[str]:
    """Every module this file imports, as dotted names relative to the package.

    Relative imports are resolved by hand: `from ..core import db` inside
    `web/routers/keys.py` is `core.db`, and that is the string the rules below
    are written against.
    """
    tree = ast.parse(path.read_text())
    pkg = path.relative_to(SRC).parts[:-1]          # ('web', 'routers')
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level:                          # relative
                base = list(pkg[: len(pkg) - (node.level - 1)])
                mod = (node.module or "").split(".") if node.module else []
                target = ".".join([*base, *mod])
            else:
                target = node.module or ""
                if not target.startswith("outline_panel"):
                    continue
                target = target[len("outline_panel."):]
            for alias in node.names:
                out.add(f"{target}.{alias.name}" if target else alias.name)
            out.add(target)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("outline_panel"):
                    out.add(alias.name[len("outline_panel."):])
    return {t for t in out if t}


def _files(*parts: str) -> list[pathlib.Path]:
    return sorted((SRC.joinpath(*parts)).rglob("*.py"))


def test_only_the_composition_root_opens_the_database():
    """One process, one connection to one SQLite file.

    `web/deps.py` builds the singletons; `cli.py` and the test fixtures open
    their own on purpose. A second `DB(...)` anywhere in the web layer means two
    connections to the same file, which is how "database is locked" and the
    write-serialisation bugs the WAL setup exists to avoid get reintroduced.
    """
    offenders = []
    for path in _files("web"):
        if path.name == "deps.py":
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "DB"):
                offenders.append(f"{path.relative_to(SRC)}:{node.lineno}")
    assert not offenders, "a second database connection: " + ", ".join(offenders)


def test_routers_reach_the_database_through_deps():
    """A router imports `db` from `web/deps.py`, never `core.db` directly.

    Weak today — `deps` re-exports the same object, so this only keeps the
    seam visible. Step 2 turns it into the real rule: a router may not name
    `db` at all, only a use case.
    """
    offenders = []
    for path in _files("web", "routers"):
        if "core.db" in _imports(path):
            offenders.append(str(path.relative_to(SRC)))
    assert not offenders, (
        "these reach past web/deps.py straight into core.db: " + ", ".join(offenders))


def test_core_never_imports_the_web_or_the_bot():
    """`core/` is the part that is already right: framework-free, importable
    from the dashboard, the scheduler, the bot and the CLI alike. One import in
    the wrong direction is what would end that."""
    offenders = []
    for path in _files("core"):
        bad = [i for i in _imports(path) if i.startswith(("web", "bot"))]
        if bad:
            offenders.append(f"{path.relative_to(SRC)} -> {bad}")
    assert not offenders, "core must not depend on a delivery mechanism: " + "; ".join(offenders)


def test_the_rules_have_exactly_one_definition():
    """`core/rights.py` is the single authority the dashboard, the bot and the
    Mini App all decide access from. A second copy is how Telegram quietly
    becomes a back door with laxer permissions — the S1/S2 bugs in
    REFACTOR_PLAN.md were exactly that.
    """
    names = ("def can_see", "def has_cap", "def owns", "def on_credit", "def price_for")
    for name in names:
        defined_in = [p.relative_to(SRC) for p in SRC.rglob("*.py")
                      if name in p.read_text()]
        assert len(defined_in) == 1 and defined_in[0].as_posix() == "core/rights.py", (
            f"`{name}` is defined in {defined_in} — it belongs only in core/rights.py")


def test_the_frontend_decides_layout_in_css_not_javascript():
    """UI-1 from the plan, now a rule rather than a ratchet.

    The dashboard used to branch on `innerWidth` in nine places to pick between
    three hand-written layout strings, and keep them honest with a debounced
    `resize` handler that re-rendered the whole app — so a rotation showed the
    wrong layout for 200ms and destroyed any open dialog. Container queries do
    it continuously, and measure the element rather than the window, which is
    what makes one stylesheet correct in a 320px Telegram webview and on a
    desktop.
    """
    import re
    text = (SRC / "static" / "index.html").read_text()
    # Comments talk *about* the debt; only code creates it. Without this the
    # ratchet counts the note explaining why the rule exists.
    code = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    code = "\n".join(ln for ln in code.splitlines() if not ln.lstrip().startswith("//"))
    uses = code.count("innerWidth")
    assert uses == 0, (
        f"{uses} uses of innerWidth in index.html. Layout is CSS here now — "
        f"@container and clamp() — and it stays that way: a JavaScript "
        f"measurement is taken once and is wrong the moment the window moves. "
        f"See MODERNIZATION.md §3.1.")


def test_the_api_does_not_describe_itself_to_the_internet():
    """A12: `/docs` and `/openapi.json` are off unless PANEL_DOCS is set.

    They are a map of every route of an admin panel, handed to anyone who asks.
    Nothing needs them at runtime — the schema the frontend's types come from is
    dumped offline by scripts/dump_openapi.py.
    """
    import importlib
    import os
    import sys
    for m in [m for m in list(sys.modules) if m.startswith("outline_panel")]:
        del sys.modules[m]
    os.environ.pop("PANEL_DOCS", None)
    os.environ.setdefault("ADMIN_PASSWORD", "pw")
    appmod = importlib.import_module("outline_panel.web.app")
    assert appmod.app.docs_url is None
    assert appmod.app.redoc_url is None
    assert appmod.app.openapi_url is None

    os.environ["PANEL_DOCS"] = "1"
    for m in [m for m in list(sys.modules) if m.startswith("outline_panel")]:
        del sys.modules[m]
    appmod = importlib.import_module("outline_panel.web.app")
    assert appmod.app.openapi_url == "/openapi.json", "the dev escape hatch broke"
    os.environ.pop("PANEL_DOCS", None)
    for m in [m for m in list(sys.modules) if m.startswith("outline_panel")]:
        del sys.modules[m]


def test_the_domain_layer_has_no_io():
    """`domain/` decides; something else acts.

    A transition returns commands and nothing more. The moment this package can
    reach a database or an HTTP client, the rules become untestable without one
    and the second caller — the scheduler, the bot — grows its own copy.
    """
    banned = ("aiosqlite", "httpx", "fastapi", "aiogram", "web", "bot",
              "core.db", "core.outline_api")
    offenders = []
    for path in _files("domain"):
        for name in _imports(path):
            if any(name == b or name.startswith(b + ".") for b in banned):
                offenders.append(f"{path.relative_to(SRC)} -> {name}")
    assert not offenders, "the domain reached for I/O: " + "; ".join(offenders)


def test_the_application_layer_does_not_know_about_http():
    """A use case is entered from the dashboard, the bot, the Mini App and the
    scheduler. If it raises `HTTPException` it only really works from one of
    them — which is how `web/deps.py` ended up lazily importing a router so the
    bot could create a key."""
    offenders = []
    for path in _files("application"):
        for name in _imports(path):
            if name.startswith(("fastapi", "web", "bot", "aiogram")):
                offenders.append(f"{path.relative_to(SRC)} -> {name}")
    assert not offenders, "the application layer imported a delivery mechanism: " + \
        "; ".join(offenders)


def test_customer_state_changes_go_through_the_aggregate():
    """A1 stays fixed.

    The per-member writes (`set_disabled`, `set_expiry`, `set_limit`, …) belong
    to the executor now. A router calling one directly is acting on one member
    of a customer again, which is the exact shape of the bug: suspend that only
    suspends the primary, renew that only renews it, delete that leaves the
    mirrors live.

    `add_key`, `set_sub_token`, `set_key_owner` and `activate` are *not* listed:
    creating, adopting, mirroring and re-attributing a key really are per-member
    operations.
    """
    per_member = ("db.set_disabled(", "db.set_expiry(", "db.set_duration(",
                  "db.set_monthly(", "db.set_limit(")
    allowed = {"web/routers/keys.py"}          # mirror_onto seeds a new member
    offenders = []
    for path in [*_files("web"), *_files("bot")]:
        rel = path.relative_to(SRC).as_posix()
        text = path.read_text()
        for call in per_member:
            if call in text and rel not in allowed:
                offenders.append(f"{rel} calls {call}")
    assert not offenders, (
        "these change one member of a customer directly instead of going "
        "through application.customer: " + "; ".join(offenders))


def test_the_node_port_has_no_outline_in_it():
    """A port named after one implementation is not a port.

    `ports/node.py` describes what the panel needs from *a* VPN server. The
    moment it mentions Outline's endpoints, its config format or its
    experimental metrics shape, writing the Xray adapter means satisfying
    Outline rather than satisfying the panel.
    """
    text = (SRC / "ports" / "node.py").read_text()
    body = "\n".join(ln for ln in text.splitlines()
                     if not ln.lstrip().startswith("#"))
    for banned in ("shadowbox", "/access-keys", "certSha256", "apiUrl"):
        assert banned not in body, f"the port leaks an Outline detail: {banned}"


def test_every_json_route_says_what_it_returns():
    """A8, closed.

    No route declared a `response_model`; every response was a hand-built dict
    and the frontend read it by convention, so a renamed field was invisible
    until the UI blanked. They all do now — except the handful below, which
    return something that is not a JSON object at all.

    That list is the point of this test. It is an allowlist, not a threshold:
    adding a route without a model fails here and has to be argued for by name,
    which is the opposite of a count quietly drifting upward.

    The models are only safe because the golden master went in first. FastAPI
    *drops* a field a model does not declare — silently, not as a validation
    error — and 93 response snapshots are the only thing that notices.
    """
    import importlib
    import os
    import sys
    for m in [m for m in list(sys.modules) if m.startswith("outline_panel")]:
        del sys.modules[m]
    os.environ.setdefault("ADMIN_PASSWORD", "pw")
    appmod = importlib.import_module("outline_panel.web.app")
    schema = appmod.app.openapi()

    # Every one of these returns a file, a stream or plain text. A response
    # model would be a lie about the content type.
    NOT_JSON = {
        ("get", "/"),                        # the dashboard itself
        ("get", "/tma"),                     # the Mini App page
        ("get", "/{token}"),                 # the customer's page
        ("get", "/sub/{token}"),             # base64 config list, or that page
        ("get", "/metrics"),                 # Prometheus text exposition
        ("get", "/api/stream"),              # server-sent events
        ("get", "/api/snapshots/{name}"),    # a database file
        # The whole database as JSON. Modelling it means modelling every table
        # twice, and it is a download rather than an API response.
        ("get", "/api/backup"),
        # Delegates to /sub/{token}/info, which validates through SubInfo by
        # hand so it can keep its own Cache-Control header — the middleware's
        # no-store rule covers /api/ paths only, and this is not one.
        ("get", "/{token}/info"),
    }

    untyped = set()
    for path, methods in schema["paths"].items():
        for method, op in methods.items():
            body = (op.get("responses", {}).get("200", {})
                    .get("content", {}).get("application/json", {}))
            if "$ref" not in str(body.get("schema", {})):
                untyped.add((method, path))

    unexpected = untyped - NOT_JSON
    assert not unexpected, (
        "these routes do not declare what they return: "
        + ", ".join(f"{m.upper()} {p}" for m, p in sorted(unexpected)))
    stale = NOT_JSON - untyped
    assert not stale, (
        "the allowlist names routes that are typed now — drop them from it: "
        + ", ".join(f"{m.upper()} {p}" for m, p in sorted(stale)))
