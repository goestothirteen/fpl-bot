from __future__ import annotations

import asyncio
import contextlib

from .bot.app import run


def main() -> None:
    with contextlib.suppress(ImportError):
        import uvloop

        uvloop.install()
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run())


if __name__ == "__main__":
    main()
