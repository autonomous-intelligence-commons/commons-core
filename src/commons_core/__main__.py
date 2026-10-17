"""Process entry point: ``python -m commons_core`` starts the API server.

Development command (do NOT run inside the sandbox):

    cd /home/z/commons-core
    .venv/bin/python -m commons_core

Equivalent uvicorn factory form:

    .venv/bin/uvicorn "commons_core.api.app:create_app --factory" \\
        --host 127.0.0.1 --port 8010

Host/port come from ``AIC_API_HOST`` / ``AIC_API_PORT``.
"""

import uvicorn

from commons_core.api.app import create_app
from commons_core.config.settings import get_settings


def main() -> None:
    """Run the API with uvicorn using configured host/port."""
    settings = get_settings()
    uvicorn.run(
        create_app(settings),
        host=settings.api_host,
        port=int(settings.api_port),
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
