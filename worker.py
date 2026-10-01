"""WayPoint durable investigation worker entry point."""
from __future__ import annotations

import asyncio

from config.settings import settings
from database.session import close_db, init_db
from utils.job_queue import LocalWorker


async def main() -> None:
    if settings.is_production and settings.run_migrations_on_startup:
        # The web service is the migration owner. The worker only waits for the
        # same schema to exist and then begins consuming durable jobs.
        await init_db()
    worker = LocalWorker(
        poll_interval=settings.job_poll_interval,
        attempts_max=settings.job_attempts_max,
    )
    try:
        await worker.run()
    finally:
        await close_db()


if __name__ == "__main__":
    asyncio.run(main())
