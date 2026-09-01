"""Wires the whole thing together."""
from __future__ import annotations

import asyncio

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand
from aiohttp import web
from redis.asyncio import Redis

from ..config import Settings, get_settings
from ..db.session import dispose, init_engine
from ..fpl.client import FPLClient
from ..live.notifier import Notifier
from ..live.poller import LivePoller
from ..logging_conf import configure_logging, get_logger
from ..scheduler.jobs import build_scheduler
from ..services.live import LiveEngine
from .handlers import analysis_cmds, info, live, misc, wager
from .handlers import setup as setup_handlers
from .middlewares import ChatContextMiddleware, ThrottleMiddleware

log = get_logger(__name__)

COMMANDS = [
    ("live", "This gameweek's live table"),
    ("season", "Full season standings"),
    ("left", "Who each team is still waiting on"),
    ("edge", "Unique players you still have to play"),
    ("wager", "League side-bet balances"),
    ("settle", "Who owes whom"),
    ("diff", "Differentials, or head-to-head"),
    ("captains", "Captain spread and returns"),
    ("bench", "Points left on benches"),
    ("chips", "Chips remaining this half"),
    ("deadline", "Next deadline countdown"),
    ("news", "Injury flags for owned players"),
    ("awards", "Gameweek awards"),
    ("transfers", "This week's transfers"),
    ("form", "Last four gameweeks"),
    ("template", "League template and bravest picks"),
    ("player", "Player card"),
    ("fixtures", "Upcoming fixtures"),
    ("link", "Link an FPL league to this chat"),
    ("leagues", "Manage linked leagues"),
    ("me", "Claim your FPL team"),
    ("settings", "Alert preferences"),
    ("topic", "Send alerts to this topic only"),
    ("help", "All commands"),
]


def build_dispatcher(engine: LiveEngine) -> Dispatcher:
    dp = Dispatcher()
    dp.message.middleware(ThrottleMiddleware())
    dp.message.middleware(ChatContextMiddleware())
    dp.callback_query.middleware(ChatContextMiddleware())
    dp["engine"] = engine
    for router in (setup_handlers.router, live.router, info.router,
                   analysis_cmds.router, wager.router, misc.router):
        dp.include_router(router)
    return dp


async def _healthz(request: web.Request) -> web.Response:
    engine: LiveEngine = request.app["engine"]
    try:
        await engine.client.bootstrap()
        return web.json_response({"status": "ok"})
    except Exception as exc:  # noqa: BLE001
        return web.json_response({"status": "degraded", "error": str(exc)}, status=503)


async def run(settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    if not settings.bot_token:
        raise SystemExit("BOT_TOKEN is not set — copy .env.example to .env and fill it in.")

    init_engine(settings.database_url)
    redis = Redis.from_url(settings.redis_url, decode_responses=False)
    client = FPLClient(
        redis,
        max_concurrency=settings.fpl_max_concurrency,
        rate_per_sec=settings.fpl_rate_per_sec,
    )
    engine = LiveEngine(client)

    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = build_dispatcher(engine)

    notifier = Notifier(bot)
    poller = LivePoller(engine, bot, notifier, base_interval=settings.live_poll_seconds)
    scheduler = build_scheduler(engine, bot)

    await bot.set_my_commands([BotCommand(command=c, description=d) for c, d in COMMANDS])
    await notifier.start()
    await poller.start()
    scheduler.start()

    # The health endpoint runs in both modes — the container healthcheck needs
    # it, and in polling mode there is otherwise no web server at all.
    health_runner = None
    try:
        if settings.use_polling:
            log.info("bot.starting", mode="polling")
            health_runner = await _serve_health(engine, settings)
            await bot.delete_webhook(drop_pending_updates=True)
            await dp.start_polling(bot, engine=engine)
        else:
            log.info("bot.starting", mode="webhook", url=settings.webhook_url)
            await _run_webhook(bot, dp, engine, settings)
    finally:
        if health_runner is not None:
            await health_runner.cleanup()
        scheduler.shutdown(wait=False)
        await poller.stop()
        await notifier.stop()
        await client.aclose()
        await bot.session.close()
        await redis.aclose()
        await dispose()


async def _serve_health(engine: LiveEngine, settings: Settings) -> web.AppRunner:
    """Just /healthz, for the Docker healthcheck when we're long-polling."""
    app = web.Application()
    app["engine"] = engine
    app.router.add_get("/healthz", _healthz)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, settings.web_host, settings.web_port).start()
    log.info("health.listening", port=settings.web_port)
    return runner


async def _run_webhook(bot: Bot, dp: Dispatcher, engine: LiveEngine, settings: Settings) -> None:
    from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

    await bot.set_webhook(
        settings.webhook_url,
        secret_token=settings.webhook_secret,
        drop_pending_updates=True,
        allowed_updates=["message", "callback_query", "inline_query"],
    )

    app = web.Application()
    app["engine"] = engine
    app.router.add_get("/healthz", _healthz)
    SimpleRequestHandler(
        dispatcher=dp, bot=bot, secret_token=settings.webhook_secret, engine=engine
    ).register(app, path=settings.webhook_path)
    setup_application(app, dp, bot=bot)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, settings.web_host, settings.web_port)
    await site.start()
    log.info("webhook.listening", host=settings.web_host, port=settings.web_port)
    try:
        await asyncio.Event().wait()   # run until cancelled
    finally:
        await runner.cleanup()
