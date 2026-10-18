"""Run the HTTP server. Ollama is not required to be up for the process to listen."""

import logging
import sys

import uvicorn

from .app import announce, create_app, startup_status
from .settings import SettingsError, load_settings


def main() -> int:
    """Load settings, warn about Ollama, and serve. Returns 1 when settings are unusable."""
    try:
        settings = load_settings()
    except SettingsError as exc:
        print(exc, file=sys.stderr)
        return 1
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(levelname)s %(name)s: %(message)s",
    )
    announce(settings, startup_status(settings))
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        log_level="info",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
