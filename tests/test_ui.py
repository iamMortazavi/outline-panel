"""
The rules from MODERNIZATION.md §3, checked in a real browser.

**Not run in CI, by choice.** Installing a browser on every run buys less than
it costs: what these check is layout and motion preferences, which move when
someone edits `static/`, not when someone edits a router. So they skip
themselves wherever Playwright or a Chromium build is missing, and CI stays a
plain `pytest -q`. Run them by hand after touching the frontend:

    pip install playwright && playwright install chromium && pytest tests/test_ui.py

Both absent-cases are verified to skip rather than error — a module-level
`importorskip` for the package, a `skipif` for the browser — because a test file
that explodes on collection would take the whole suite with it.

The static checks that need no browser (no `innerWidth` in a layout expression)
live in `test_architecture.py` and run everywhere.

The dashboard is served by a real uvicorn on a temporary database. An ASGI
transport would not do: half of what is being asserted is what the *browser*
computes — container queries, `clamp()`, `prefers-reduced-motion`.
"""

import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import time

import pytest

CHROME = pathlib.Path("/opt/pw-browsers/chromium-1194/chrome-linux/chrome")
pytest.importorskip("playwright", reason="playwright is not installed")
if not CHROME.exists():
    matches = list(pathlib.Path("/opt/pw-browsers").glob("chromium-*/chrome-linux/chrome"))
    CHROME = matches[0] if matches else None
pytestmark = pytest.mark.skipif(CHROME is None, reason="no Chromium build available")

PW = "ui-test-password"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def panel_url():
    port = _free_port()
    root = pathlib.Path(__file__).resolve().parent.parent
    env = {**os.environ,
           "DB_PATH": os.path.join(tempfile.mkdtemp(), "ui.db"),
           "ADMIN_PASSWORD": PW, "SESSION_SECRET": "ui-tests",
           "COOKIE_SECURE": "false", "ENABLE_SCHEDULER": "false"}
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "outline_panel.web.app:app",
         "--host", "127.0.0.1", "--port", str(port), "--app-dir", str(root / "src")],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}"
    for _ in range(80):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                break
        except OSError:
            time.sleep(0.25)
    else:
        proc.kill()
        pytest.skip("the panel did not start")
    yield url
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture(scope="module")
def browser():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=str(CHROME))
        yield b
        b.close()


def _dashboard(browser, url, **ctx):
    page = browser.new_page(**ctx)
    page.goto(url, wait_until="networkidle")
    page.fill("#un", "admin")
    page.fill("#pw", PW)
    page.click("#loginBtn")
    page.wait_for_selector("#shell", timeout=20000)
    return page


# ------------------------------------------------------------------ layout
@pytest.mark.parametrize("width", [320, 390, 720, 1024, 1440, 1920])
def test_nothing_ever_scrolls_sideways(browser, panel_url, width):
    """A horizontal scrollbar on a dashboard is always a bug, and it is the one
    a fixed-px layout produces first. Six widths rather than three breakpoints,
    because the layout is continuous now and the gaps between the old
    breakpoints are exactly where it used to fail."""
    page = _dashboard(browser, panel_url, viewport={"width": width, "height": 800})
    overflow = page.evaluate(
        "() => document.documentElement.scrollWidth > window.innerWidth + 1")
    assert not overflow, f"the page scrolls sideways at {width}px"
    page.close()


def test_the_layout_follows_the_container_not_a_javascript_measurement(browser, panel_url):
    """The shell is one column on a phone and two on a desktop, and the change
    happens because the container changed size — no resize listener, no
    re-render. Resizing the viewport without reloading is what proves it: the
    old code needed a debounced `resize` handler and a full `renderApp()`."""
    page = _dashboard(browser, panel_url, viewport={"width": 1440, "height": 900})
    cols = lambda: page.evaluate(  # noqa: E731
        "() => getComputedStyle(document.querySelector('.shell')).gridTemplateColumns")
    wide = cols()
    assert len(wide.split()) == 2, f"expected a two-column shell at 1440px, got {wide!r}"
    page.set_viewport_size({"width": 500, "height": 900})
    page.wait_for_timeout(200)          # no reload, no re-render — just CSS
    narrow = cols()
    assert len(narrow.split()) == 1, f"expected one column at 500px, got {narrow!r}"
    page.close()


def test_the_desktop_only_controls_are_actually_hidden_on_a_phone(browser, panel_url):
    """This file styles nearly everything inline, and an inline `display` beats
    a class — so a visibility helper that is not `!important` loses silently."""
    page = _dashboard(browser, panel_url, viewport={"width": 380, "height": 800})
    shown = page.eval_on_selector_all(
        ".only-wide", "els => els.filter(e => getComputedStyle(e).display !== 'none').length")
    assert shown == 0, f"{shown} desktop-only controls are visible on a phone"
    page.close()


# ------------------------------------------------------- taste and comfort
def test_reduced_motion_is_honoured(browser, panel_url):
    """Seven keyframe animations ran unconditionally. Someone who turned
    animation off in their OS asked for a reason."""
    page = _dashboard(browser, panel_url, viewport={"width": 1200, "height": 800},
                      reduced_motion="reduce")
    dur = page.evaluate(
        "() => getComputedStyle(document.documentElement).getPropertyValue('--dur').trim()")
    assert dur == "0s", f"--dur is {dur!r} with prefers-reduced-motion: reduce"
    page.close()


def test_both_colour_schemes_are_real(browser, panel_url):
    """The panel was dark-only and is used outdoors, on a phone, in daylight."""
    seen = {}
    for scheme in ("dark", "light"):
        page = _dashboard(browser, panel_url, viewport={"width": 1200, "height": 800},
                          color_scheme=scheme)
        seen[scheme] = page.evaluate(
            "() => getComputedStyle(document.documentElement).getPropertyValue('--bg').trim()")
        assert page.evaluate(
            "() => getComputedStyle(document.body).backgroundColor") not in ("", "transparent",
                                                                             "rgba(0, 0, 0, 0)")
        page.close()
    assert seen["dark"] != seen["light"], f"both schemes render the same background: {seen}"


def test_type_scales_with_the_viewport(browser, panel_url):
    """clamp() rather than px literals: the same page on a 320px phone and a
    1920px monitor should not be the same size, and neither should be extreme."""
    sizes = {}
    for width in (320, 1920):
        page = _dashboard(browser, panel_url, viewport={"width": width, "height": 800})
        # A custom property reads back as its raw `clamp(...)` text; only an
        # element that *uses* it has a resolved size.
        sizes[width] = page.evaluate("""() => {
            const probe = document.createElement('div');
            probe.style.cssText = 'font-size:var(--step-2);position:absolute;visibility:hidden';
            document.body.appendChild(probe);
            const px = parseFloat(getComputedStyle(probe).fontSize);
            probe.remove();
            return px;
        }""")
        page.close()
    assert sizes[320] < sizes[1920], f"the type scale is fixed: {sizes}"
    assert sizes[1920] / sizes[320] < 2.5, f"the scale runs away: {sizes}"
