"""python3 -m builder [--dev]  (dev: UI open to any peer, data in ./dev-data)"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

from .server import serve
from .state import State


def main() -> None:
    dev = "--dev" in sys.argv
    level = os.environ.get("CB_LOG_LEVEL", "info").upper()
    logging.basicConfig(level=getattr(logging, level, logging.INFO), format="%(levelname)s %(name)s: %(message)s")
    data = Path(os.environ.get("CB_DATA", "./dev-data" if dev else "/data"))
    state = State(data)
    ui_port = int(os.environ.get("CB_UI_PORT", "8099"))
    lan_port = int(os.environ.get("CB_LAN_PORT", "8808"))
    asyncio.run(serve(state, ui_port, lan_port, dev))


if __name__ == "__main__":
    main()
