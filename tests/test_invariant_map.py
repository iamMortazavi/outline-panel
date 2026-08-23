"""
The invariant register is only useful if it stays true.

`INVARIANTS.md` maps each guarantee in MODERNIZATION.md §1.2 to the test that
holds it. Without this check the map rots the first time a test is renamed, and
a rotted map is worse than none: it reads as coverage that is not there.

This asserts the mapping resolves — the named test exists, in the named file. It
deliberately does not assert the test is *good*; that is what mutation-checking
is for, and REFACTOR_PLAN.md already established that practice for the security
fixes.
"""

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent
REGISTER = ROOT / "INVARIANTS.md"
_ROW = re.compile(r"^([A-Za-z0-9-]+)\s*\|\s*(\w+)\s*\|\s*(\S+)\s*$")


def _rows():
    for line in REGISTER.read_text().splitlines():
        m = _ROW.match(line.strip())
        if m:
            yield m.group(1), m.group(2), m.group(3)


def test_the_register_is_not_empty():
    rows = list(_rows())
    assert len(rows) >= 30, f"only {len(rows)} invariants mapped — did the format change?"


def test_every_mapped_invariant_still_has_its_test():
    missing = []
    for inv_id, test_name, rel in _rows():
        path = ROOT.parent / rel
        if not path.exists():
            missing.append(f"{inv_id}: {rel} is gone")
        elif f"def {test_name}(" not in path.read_text():
            missing.append(f"{inv_id}: {rel} no longer defines {test_name}")
    assert not missing, (
        "the invariant register points at tests that no longer exist. Either the "
        "guarantee moved (update tests/INVARIANTS.md) or it was dropped (say so "
        "out loud):\n  " + "\n  ".join(missing))


def test_invariant_ids_are_unique():
    ids = [r[0] for r in _rows()]
    dupes = {i for i in ids if ids.count(i) > 1}
    assert not dupes, f"duplicate invariant ids: {sorted(dupes)}"
