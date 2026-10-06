"""Uvicorn entrypoint."""

import uvicorn

from uni_cockpit.app import create_app

app = create_app()


def run() -> None:
    uvicorn.run("uni_cockpit.main:app", host="127.0.0.1", port=8000, reload=False)
