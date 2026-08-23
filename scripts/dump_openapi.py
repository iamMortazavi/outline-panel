#!/usr/bin/env python3
"""
Write the OpenAPI schema to a file, without serving it.

`/openapi.json` is disabled on a running panel (it describes every route of an
admin panel to anyone who asks). The schema is still wanted at build time — it
is what the frontend's types are generated from — so it is produced here, from
the app object, offline.

    python scripts/dump_openapi.py openapi.json
"""

import json
import os
import sys

os.environ.setdefault("ADMIN_PASSWORD", "build-only")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))

from outline_panel.web.app import app  # noqa: E402


def main() -> None:
    out = sys.argv[1] if len(sys.argv) > 1 else "openapi.json"
    schema = app.openapi()
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(schema, fh, indent=2, ensure_ascii=False, sort_keys=True)
        fh.write("\n")
    print(f"{out}: {len(schema.get('paths', {}))} paths")


if __name__ == "__main__":
    main()
