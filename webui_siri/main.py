from __future__ import annotations

import sys

import uvicorn

from webui_siri.api import create_app
from webui_siri.config import load_config
from webui_siri.logging_setup import get_logger, setup_logging
from webui_siri.openwebui import OpenWebUIClient, OpenWebUIConfig


def main() -> None:
    config = load_config()
    setup_logging(secrets=(config.open_webui_token, config.api_key))
    logger = get_logger("webui_siri")

    openwebui = OpenWebUIClient(
        OpenWebUIConfig(
            base_url=str(config.open_webui_url),
            token=config.open_webui_token,
            folder=config.open_webui_folder,
        )
    )

    try:
        logger.info("verifying Open WebUI API access at %s", config.open_webui_url)
        openwebui.verify_access_sync()
        logger.info("Open WebUI API access verified")
    except Exception as exc:
        logger.error("Open WebUI API access check failed: %s", exc)
        sys.exit(1)

    app = create_app(config=config, openwebui=openwebui)
    logger.info("starting API server on port %d", config.api_port)
    uvicorn.run(app, host="0.0.0.0", port=config.api_port, log_level="warning")


if __name__ == "__main__":
    main()
