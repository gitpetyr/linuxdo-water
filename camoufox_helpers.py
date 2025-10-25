import re
import time
from typing import Optional

from playwright.sync_api import (
    BrowserContext,
    Error as PlaywrightError,
    Page,
    TimeoutError as PlaywrightTimeoutError,
)


TURNSTILE_FRAME_SUBSTRING = "challenges.cloudflare.com"


def solve_turnstile(page: Page, attempts: int = 10, delay: float = 0.8) -> bool:
    """Best-effort Cloudflare Turnstile solver using Playwright primitives."""
    try:
        page.evaluate("() => { try { turnstile.reset(); } catch (e) {} }")
    except PlaywrightError:
        pass

    for _ in range(attempts):
        try:
            token = page.evaluate(
                "() => { try { return turnstile.getResponse(); } catch (e) { return null; } }"
            )
            if token:
                return True
        except PlaywrightError:
            pass

        frame = _locate_turnstile_frame(page)
        if frame:
            try:
                checkbox = frame.locator("input[type='checkbox'], input[type='radio']")
                checkbox.wait_for(state="visible", timeout=3_000)
                checkbox.click()
            except PlaywrightTimeoutError:
                pass
            except PlaywrightError:
                pass

        page.wait_for_timeout(int(delay * 1_000))

    try:
        page.reload(wait_until="domcontentloaded")
    except PlaywrightError:
        pass
    return False


def perform_login(
    context: BrowserContext,
    username: str,
    password: str,
    *,
    login_url: str = "https://linux.do/login",
    success_pattern: re.Pattern[str] = re.compile(r"^https://linux\.do/?"),
) -> bool:
    """Log into linux.do using the provided browser context."""
    page = context.new_page()
    page.set_default_timeout(15_000)
    try:
        page.goto(login_url, wait_until="domcontentloaded")
        _wait_for_login_inputs(page)
        solve_turnstile(page)
        page.fill("#login-account-name", username)
        page.fill("#login-account-password", password)
        page.click("#login-button")
        page.wait_for_url(success_pattern)
        page.wait_for_load_state("networkidle")
        return True
    except PlaywrightTimeoutError as exc:
        print(f"Login timed out: {exc}")
    except PlaywrightError as exc:
        print(f"Login failed: {exc}")
    finally:
        page.close()
    return False


def _locate_turnstile_frame(page: Page) -> Optional[Page]:
    """Find the Cloudflare Turnstile frame, if present."""
    for frame in page.frames:
        if TURNSTILE_FRAME_SUBSTRING in frame.url:
            return frame
    return None


def _wait_for_login_inputs(page: Page) -> None:
    """Ensure login inputs are visible before interacting."""
    page.wait_for_selector("#login-account-name", state="visible")
    page.wait_for_selector("#login-account-password", state="visible")
