from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.oauth import router as oauth_router
from app.debug import router as debug_router
from app.tasks.scheduler import create_scheduler
from app.bot import build_bot, build_dispatcher
import os


app = FastAPI(title=os.getenv("PROJECT_NAME", "meet-bot"))
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(oauth_router)
app.include_router(debug_router)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)

else:
    scheduler = create_scheduler()
    scheduler.start()

    # Запуск aiogram polling в фоне (без блокировки ASGI)
    import asyncio

    bot = build_bot()
    dp = build_dispatcher()

    async def _run_bot():
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

    asyncio.get_event_loop().create_task(_run_bot())


