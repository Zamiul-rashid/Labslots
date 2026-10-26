"""Entry point: `uvicorn labslots.main:app`."""

import logging

from .config import Config
from .web import create_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
app = create_app(Config.from_env())
