import os
import sys
import tempfile

import pytest

# allow running tests without installing the package (src-layout on path)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"))

from outline_panel.core.db import DB  # noqa: E402


@pytest.fixture
async def db():
    path = os.path.join(tempfile.mkdtemp(), "test.db")
    d = DB(path)
    await d.init()
    yield d
    await d.close()
