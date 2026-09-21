"""Process entry point for the generic Wren HTTP service."""

import uvicorn

from .bootstrap import create_app
from .settings import ServiceSettings


def main() -> None:
    settings = ServiceSettings()
    app = create_app(settings)
    uvicorn.run(
        app,
        host=settings.http_host,
        port=settings.http_port,
        workers=settings.http_workers,
    )
