"""
The shape of the package, asserted rather than described.

`web.deps` used to import `web.routers.keys` from inside a function body — the
bot needs the panel's key-creation logic, that logic lived in a route, and
routes import `deps`. A lazy import hides a cycle; it does not remove one, and
it meant the import order of the package depended on which function ran first.

These tests pin the layering that replaced it:

    subcache / registry  ->  state  ->  idempotency  ->  services  ->  deps  ->  routers

and pin the property the old arrangement got right, which any fix had to keep:
the Telegram bot manager is fully wired by importing `deps`, so no entry point
can construct a half-configured one.
"""
import ast
import pathlib

SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
PKG = SRC / "outline_panel"


def _modules() -> dict[str, pathlib.Path]:
    out = {}
    for f in sorted(PKG.rglob("*.py")):
        name = ".".join(f.relative_to(SRC).with_suffix("").parts)
        out[name.removesuffix(".__init__")] = f
    return out


def _imports(name: str, path: pathlib.Path, known: set[str]) -> set[str]:
    """Every in-package module `name` imports — including inside a function.

    Walking the whole tree rather than the module's top level is the point: a
    deferred import is exactly the thing being tested for.
    """
    pkg = name if path.name == "__init__.py" else name.rsplit(".", 1)[0]
    found = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom):
            if node.level == 0:
                target = node.module or ""
            else:
                base = pkg
                for _ in range(node.level - 1):
                    base = base.rsplit(".", 1)[0]
                target = f"{base}.{node.module}" if node.module else base
            if not target.startswith("outline_panel"):
                continue
            found.add(target)
            # `from x import y` where y is itself a module
            found.update(f"{target}.{a.name}" for a in node.names
                         if f"{target}.{a.name}" in known)
        elif isinstance(node, ast.Import):
            found.update(a.name for a in node.names
                         if a.name.startswith("outline_panel"))
    return {d for d in found if d in known and d != name}


def _graph() -> dict[str, set[str]]:
    mods = _modules()
    known = set(mods)
    return {m: _imports(m, p, known) for m, p in mods.items()}


def _cycles(graph: dict[str, set[str]]) -> list[list[str]]:
    """Every strongly connected component with more than one module, plus any
    module that imports itself. Iterative Tarjan — the recursive form blows the
    stack on a package this size."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    found: list[list[str]] = []

    for root in sorted(graph):
        if root in index:
            continue
        work = [(root, 0)]
        while work:
            node, child = work[-1]
            if child == 0:
                index[node] = low[node] = len(index)
                stack.append(node)
                on_stack.add(node)
            descended = False
            for i, nxt in enumerate(sorted(graph[node])[child:], start=child):
                if nxt not in index:
                    work[-1] = (node, i + 1)
                    work.append((nxt, 0))
                    descended = True
                    break
                if nxt in on_stack:
                    low[node] = min(low[node], index[nxt])
            if descended:
                continue
            if low[node] == index[node]:
                component = []
                while True:
                    popped = stack.pop()
                    on_stack.discard(popped)
                    component.append(popped)
                    if popped == node:
                        break
                if len(component) > 1:
                    found.append(sorted(component))
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[node])

    return found + [[m] for m in sorted(graph) if m in graph[m]]


def test_the_package_has_no_import_cycles():
    assert _cycles(_graph()) == []


def test_deps_does_not_import_a_router():
    """The specific arrow that was inverted. `deps` is what routers import; it
    must not import them back, at module level or from inside a function."""
    graph = _graph()
    assert not {m for m in graph["outline_panel.web.deps"] if ".routers" in m}


def test_the_shared_objects_import_nothing_from_the_web_layer():
    """`state` is the bottom of the package. It may reach into core and the
    registry; anything else means the bottom has grown a dependency on a layer
    above it and the cycle is on its way back."""
    allowed = {"outline_panel.web.registry"}
    reached = {m for m in _graph()["outline_panel.web.state"]
               if m.startswith("outline_panel.web")}
    assert reached <= allowed, f"state reaches {reached - allowed}"


def test_the_key_service_does_not_import_the_http_layer():
    """The service is what the bot calls. If it needs `deps`, the bot needs
    FastAPI's dependency machinery, and the split bought nothing."""
    reached = _graph()["outline_panel.web.services.keys"]
    assert not {m for m in reached if ".routers" in m or m.endswith(".deps")}


def test_importing_deps_is_enough_to_get_a_wired_bot_manager():
    """The property the lazy import was protecting, and the reason the wiring
    stays in one module: `bot/run.py` and the web app both get the bot manager
    by importing `deps`. A standalone bot once built its own dispatcher without
    `create_key` or `resolve_admin`, which broke key creation and made every
    bot admin a panel owner (REFACTOR_PLAN S2).
    """
    from outline_panel.web import deps
    from outline_panel.web.services import keys as key_service

    assert deps.botmgr.create_key is key_service.create_key_as
    assert deps.botmgr.resolve_admin is not None
    assert deps.botmgr.db is deps.db
    assert deps.botmgr.registry is deps.reg


def test_the_standalone_bot_never_builds_its_own_manager():
    """S2 itself: `bot/run.py` hand-built a dispatcher and passed neither
    `resolve_admin` nor `create_key`, so every bot admin became a panel owner
    and creating a user answered "unavailable". It must use the one `deps`
    wires and construct nothing.

    Checked against the source, not by attribute: run.py imports `deps` inside
    `main()` on purpose, so that `--help` does not pay for building the DB and
    registry singletons.
    """
    source = (PKG / "bot" / "run.py").read_text()
    tree = ast.parse(source)
    built = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id in {"BotManager", "build_dispatcher"}]
    assert not built, "run.py builds its own bot wiring again"
    assert "deps.botmgr" in source


def test_deps_still_re_exports_what_the_routers_import():
    """Moving the singletons into `state` must stay invisible to the routers:
    they have always imported these from `..deps` and none of them changed."""
    from outline_panel.web import deps

    for name in ("db", "reg", "settings", "signer", "botmgr", "COOKIE_NAME",
                 "STATIC_DIR", "api_or_404", "sids_or_404", "scoped_ids", "host",
                 "assert_cap", "assert_key_access", "enforce_scope", "require",
                 "require_owner", "current_admin", "admin_for_telegram", "CAPS",
                 "can_see", "csv_list", "_csv", "has_cap", "is_owner",
                 "on_credit", "owns", "price_for"):
        assert hasattr(deps, name), f"deps no longer exports {name}"
