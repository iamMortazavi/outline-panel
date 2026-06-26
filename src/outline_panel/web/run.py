"""Console-script entry point for the web dashboard (`outline-panel`)."""

from __future__ import annotations

import os


def cli() -> None:
    import uvicorn

    uvicorn.run(
        "outline_panel.web.app:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8000")),
    )


if __name__ == "__main__":
    cli()
