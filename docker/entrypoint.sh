#!/usr/bin/env bash
set -euo pipefail

echo "waiting for postgres…"
python - <<'PY'
import asyncio, os, sys, time
import asyncpg

url = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")

async def wait():
    for _ in range(60):
        try:
            conn = await asyncpg.connect(url)
            await conn.close()
            return
        except Exception:
            await asyncio.sleep(1)
    sys.exit("postgres never came up")

asyncio.run(wait())
PY

echo "running migrations…"
alembic upgrade head

exec "$@"
