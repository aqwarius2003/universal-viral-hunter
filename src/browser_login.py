import asyncio
import json
import logging
import sys
import argparse
from pathlib import Path
from typing import Any, Dict, Optional

from src.config import settings

logger = logging.getLogger("browser_login")


async def dismiss_cookie_banner(page: Any) -> None:
    """Dismisses European GDPR cookie consent banners/dialogs if present."""
    banner_selectors = [
        'button:has-text("Refuse non-essential")',
        'button:has-text("Accept all cookies")',
        'button:has-text("Accept all")',
        'button:has-text("Принять все файлы cookie")',
        'button:has-text("Принять все")',
        'button:has-text("Отклонить необязательные")',
        'button:has-text("Отклонить")',
        'button:has-text("Dismiss")',
        'button[data-testid="xMigrationBottomBar"]',
        'div[role="dialog"] button:has-text("Accept")',
        'div[role="dialog"] button:has-text("Refuse")',
        'div[role="dialog"] button:has-text("Agree")',
    ]
    for sel in banner_selectors:
        try:
            elem = await page.query_selector(sel)
            if elem and await elem.is_visible():
                await elem.click()
                await asyncio.sleep(0.8)
                break
        except Exception:
            pass


async def playwright_twitter_login(
    auth_info_1: str,
    password: str,
    auth_info_2: Optional[str] = None,
    code: Optional[str] = None,
    proxy_url: Optional[str] = None,
    headless: bool = True,
    target_cookies_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """
    Automates Twitter/X.com login using Playwright Chromium browser with stealth evasions.
    Saves auth_token, ct0, and full cookies to target_cookies_path (data/cookies.json).
    Runs from current host/VPS IP address.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return {
            "status": "error",
            "error": "Playwright не установлен. Установите: poetry run playwright install --with-deps chromium",
        }

    if not target_cookies_path:
        target_cookies_path = Path(settings.cookies_path)

    target_cookies_path.parent.mkdir(parents=True, exist_ok=True)
    debug_dir = target_cookies_path.parent

    effective_proxy = proxy_url or settings.twitter_proxy_url
    launch_kwargs: Dict[str, Any] = {
        "headless": headless,
        "args": [
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
            "--disable-features=IsolateOrigins,site-per-process",
            "--lang=en-US,en",
        ],
    }

    if effective_proxy and effective_proxy.strip():
        launch_kwargs["proxy"] = {"server": effective_proxy.strip()}

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(**launch_kwargs)
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 800},
                locale="en-US",
                timezone_id="Europe/Amsterdam",
            )

            # Anti-detection stealth script to pass Kasada/Castle checks
            await context.add_init_script("""
                // Hide navigator.webdriver
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => undefined
                });

                // Mock chrome runtime
                window.chrome = {
                    runtime: {},
                    loadTimes: function() {},
                    csi: function() {},
                    app: {}
                };

                // Mock plugins
                Object.defineProperty(navigator, 'plugins', {
                    get: () => [1, 2, 3, 4, 5]
                });

                // Mock languages
                Object.defineProperty(navigator, 'languages', {
                    get: () => ['en-US', 'en']
                });

                // Mock permissions
                const originalQuery = window.navigator.permissions ? window.navigator.permissions.query : null;
                if (originalQuery) {
                    window.navigator.permissions.query = (parameters) => (
                        parameters.name === 'notifications' ?
                            Promise.resolve({ state: Notification.permission }) :
                            originalQuery(parameters)
                    );
                }
            """)

            page = await context.new_page()

            try:
                # Navigate using domcontentloaded to avoid hanging on persistent network streams
                await page.goto("https://x.com/i/flow/login", wait_until="domcontentloaded", timeout=35000)
                await asyncio.sleep(2)
                await dismiss_cookie_banner(page)

                # Check if page redirected to landing page with a Sign in button
                if "flow" not in page.url:
                    sign_in_sel = 'a[href="/login"], a[href="/i/flow/login"], button:has-text("Sign in"), button:has-text("Log in"), button:has-text("Войти")'
                    sign_btn = await page.query_selector(sign_in_sel)
                    if sign_btn and await sign_btn.is_visible():
                        await sign_btn.click()
                        await asyncio.sleep(2)
                        await dismiss_cookie_banner(page)

                # Step 1: Locate and fill username/email
                username_selectors = [
                    'input[autocomplete="username"]',
                    'input[name="text"]',
                    'input[type="text"]',
                    'input[data-testid="ocfEnterTextTextInput"]',
                ]

                username_elem = None
                for _ in range(15):
                    await dismiss_cookie_banner(page)
                    for sel in username_selectors:
                        el = await page.query_selector(sel)
                        if el and await el.is_visible():
                            username_elem = el
                            break
                    if username_elem:
                        break
                    await asyncio.sleep(1)

                if not username_elem:
                    # Save diagnostic screenshot and HTML
                    screenshot_path = debug_dir / "login_failed.png"
                    await page.screenshot(path=str(screenshot_path), full_page=True)
                    page_html_path = debug_dir / "login_failed.html"
                    with open(page_html_path, "w", encoding="utf-8") as f:
                        f.write(await page.content())

                    curr_title = await page.title()
                    curr_url = page.url
                    await browser.close()
                    return {
                        "status": "error",
                        "error": f"Форма входа X.com не найдена (URL: {curr_url}, Заголовок: '{curr_title}'). Возможно сработала проверка капчи Cloudflare/Kasada. Скриншот сохранен в data/login_failed.png",
                    }

                await username_elem.click()
                await page.keyboard.type(auth_info_1.strip(), delay=40)
                await asyncio.sleep(0.5)

                # Click Next or press Enter
                next_btn_sel = 'button:has-text("Next"), button:has-text("Далее"), //span[text()="Next"]/ancestor::button, //span[text()="Далее"]/ancestor::button'
                next_btn = await page.query_selector(next_btn_sel)
                if next_btn and await next_btn.is_visible():
                    await next_btn.click()
                else:
                    await page.keyboard.press("Enter")
                await asyncio.sleep(2.5)
                await dismiss_cookie_banner(page)

                # Step 2: Additional verification (if X asks for phone or email before password)
                password_selectors = ['input[name="password"]', 'input[type="password"]']
                pwd_found = False
                for _ in range(5):
                    for sel in password_selectors:
                        if await page.is_visible(sel):
                            pwd_found = True
                            break
                    if pwd_found:
                        break
                    # If not found, check if confirmation email/username field is present
                    text_input = 'input[name="text"], input[data-testid="ocfEnterTextTextInput"]'
                    if await page.is_visible(text_input):
                        if auth_info_2:
                            await page.click(text_input)
                            await page.keyboard.type(auth_info_2.strip(), delay=40)
                            await asyncio.sleep(0.5)
                            await page.keyboard.press("Enter")
                            await asyncio.sleep(2.5)
                            break
                    await asyncio.sleep(1)

                # Step 3: Enter password
                pwd_elem = None
                for _ in range(10):
                    for sel in password_selectors:
                        el = await page.query_selector(sel)
                        if el and await el.is_visible():
                            pwd_elem = el
                            break
                    if pwd_elem:
                        break
                    await asyncio.sleep(1)

                if not pwd_elem:
                    screenshot_path = debug_dir / "login_failed.png"
                    await page.screenshot(path=str(screenshot_path), full_page=True)
                    curr_url = page.url
                    await browser.close()
                    return {
                        "status": "error",
                        "error": f"Поле ввода пароля не появилось (URL: {curr_url}). Скриншот сохранен в data/login_failed.png",
                    }

                await pwd_elem.click()
                await page.keyboard.type(password, delay=40)
                await asyncio.sleep(0.5)

                # Click Login or press Enter
                login_btn_sel = 'button:has-text("Log in"), button:has-text("Войти"), button[data-testid="LoginForm_Login_Button"]'
                login_btn = await page.query_selector(login_btn_sel)
                if login_btn and await login_btn.is_visible():
                    await login_btn.click()
                else:
                    await page.keyboard.press("Enter")
                await asyncio.sleep(4)

                # Step 4: Check if 2FA code is requested
                code_selector = 'input[data-testid="ocfEnterTextTextInput"], input[name="text"]'
                if await page.is_visible(code_selector) and "flow" in page.url:
                    if code:
                        await page.fill(code_selector, code.strip())
                        await page.keyboard.press("Enter")
                        await asyncio.sleep(4)
                    else:
                        await browser.close()
                        return {
                            "status": "code_required",
                            "message": "Twitter потребовал 2FA код подтверждения из почты или приложения.",
                        }

                await asyncio.sleep(3)
                cookies = await context.cookies()
                cookie_dict = {c["name"]: c["value"] for c in cookies}

                if "auth_token" in cookie_dict and "ct0" in cookie_dict:
                    # Write to target cookies path
                    with open(target_cookies_path, "w", encoding="utf-8") as f:
                        json.dump(cookie_dict, f, indent=2)

                    # Also write to root cookies.json
                    root_p = Path("cookies.json")
                    with open(root_p, "w", encoding="utf-8") as f:
                        json.dump(cookie_dict, f, indent=2)

                    await browser.close()
                    return {
                        "status": "ok",
                        "message": f"Авторизация в X.com через Playwright успешно завершена! Cookies сохранены в {target_cookies_path}",
                        "cookies_saved": True,
                    }
                else:
                    screenshot_path = debug_dir / "login_failed.png"
                    await page.screenshot(path=str(screenshot_path), full_page=True)
                    error_msg = f"Куки auth_token/ct0 не найдены. URL: {page.url}"
                    toast_elem = await page.query_selector('div[data-testid="toast"]')
                    if toast_elem:
                        txt = await toast_elem.inner_text()
                        if txt:
                            error_msg = f"Сообщение от X: {txt}"
                    await browser.close()
                    return {"status": "error", "error": error_msg}

            except Exception as inner_err:
                try:
                    screenshot_path = debug_dir / "login_failed.png"
                    await page.screenshot(path=str(screenshot_path), full_page=True)
                except Exception:
                    pass
                await browser.close()
                return {"status": "error", "error": f"Ошибка во время сессии браузера: {str(inner_err)}"}

    except Exception as err:
        return {"status": "error", "error": f"Ошибка запуска браузера Playwright: {str(err)}"}


def main() -> None:
    parser = argparse.ArgumentParser(description="Browser login to X.com using Playwright")
    parser.add_argument("--username", "-u", required=True, help="Twitter username or email")
    parser.add_argument("--password", "-p", required=True, help="Twitter password")
    parser.add_argument("--email", "-e", help="Twitter verification email/phone if requested")
    parser.add_argument("--code", "-c", help="Twitter 2FA confirmation code")
    parser.add_argument("--proxy", help="HTTP/SOCKS5 proxy URL")
    parser.add_argument("--headful", action="store_true", help="Run browser in visible (non-headless) mode")

    args = parser.parse_args()

    result = asyncio.run(
        playwright_twitter_login(
            auth_info_1=args.username,
            password=args.password,
            auth_info_2=args.email,
            code=args.code,
            proxy_url=args.proxy,
            headless=not args.headful,
        )
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result.get("status") != "ok":
        sys.exit(1)


if __name__ == "__main__":
    main()
