from __future__ import annotations

import os
from aiogram import Bot, Dispatcher
from . import handlers


def build_dispatcher() -> Dispatcher:
    dp = Dispatcher()
    dp.include_router(handlers.router)
    return dp


def build_bot() -> Bot:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
    return Bot(token=token)



