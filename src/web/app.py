import os
from aiohttp import web
from src.db import init_db, load_system_settings_into_config
from src.web.middlewares import auth_middleware
from src.web.routes import register_routes
from src.telegram.moderation_bot import start_moderation_bot


async def on_app_startup(app: web.Application) -> None:
    """Initializes the database and loads system settings into config on server start."""
    await init_db()
    await load_system_settings_into_config()


def create_app() -> web.Application:
    """Creates and configures the web application with authentication middleware and routes."""
    app = web.Application(middlewares=[auth_middleware])
    app.on_startup.append(on_app_startup)
    register_routes(app)
    return app


async def start_web_server(host: str = "0.0.0.0", port: int = 8000) -> None:
    """Starts the web dashboard server."""
    app = create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    try:
        await start_moderation_bot()
    except Exception as e:
        print(f"⚠️ Не удалось запустить Telegram-бота модерации: {e}")
    print(f"🌐 Дашборд Viral Hunter запущен на http://localhost:{port} (host: {host})")


def main() -> None:
    """Entry point for running the web dashboard via CLI."""
    port = int(os.environ.get("PORT", 8000))
    host = os.environ.get("HOST", "0.0.0.0")
    app = create_app()
    print(f"🚀 Запуск веб-дашборда на http://localhost:{port}")
    web.run_app(app, host=host, port=port)


if __name__ == "__main__":
    main()
