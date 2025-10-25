"""Visitor script migrated from DrissionPage to Camoufox."""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from typing import List, Tuple

from camoufox import Camoufox, launch_options
from playwright.sync_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError

from camoufox_helpers import perform_login, solve_turnstile

USERNAME = os.getenv('LINUX_DO_USERNAME', 'default_user')
PASSWORD = os.getenv('LINUX_DO_PASSWORD', 'default_pass')
if USERNAME == 'default_user' or PASSWORD == 'default_pass':
    print("Warning: Username or Password missing in environment variables. Check start.py configuration.")

CAMOUFOX_HEADLESS = os.getenv('CAMOUFOX_HEADLESS', '0') == '1'
CAMOUFOX_DEBUG = os.getenv('CAMOUFOX_DEBUG', '0') == '1'
NAVIGATION_TIMEOUT = int(os.getenv('TPREAD_NAV_TIMEOUT_MS', '15000'))
SCROLL_DELAY = float(os.getenv('TPREAD_SCROLL_DELAY_SECONDS', '0.4'))
SCROLL_STEP = int(os.getenv('TPREAD_SCROLL_STEP', '400'))
MAX_RETRIES = int(os.getenv('TPREAD_VISIT_RETRIES', '3'))


def init_visited_db() -> None:
    """Initialize the visited posts database."""
    conn = sqlite3.connect('visited_posts.db', check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute(
        '''
        CREATE TABLE IF NOT EXISTS visited_topics (
            topic_id INTEGER PRIMARY KEY,
            last_visited_posts_count INTEGER,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        '''
    )
    conn.commit()
    conn.close()


def read_topics() -> List[Tuple[int, int]]:
    """Fetch topics from topics.db."""
    conn = sqlite3.connect('topics.db', check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute('SELECT id, posts_count FROM topic_ids ORDER BY id')
    rows = cursor.fetchall()
    conn.close()
    print(f"Read {len(rows)} topics from topics.db")
    return rows


def build_camoufox_options():
    """Produce Camoufox launch options for Firefox."""
    return launch_options(
        headless=CAMOUFOX_HEADLESS,
        disable_coop=True,
        humanize=True,
        block_images=False,
        block_webrtc=False,
    )


@contextmanager
def camoufox_context():
    """Yield a logged-in browser context."""
    launch_kwargs = dict(from_options=build_camoufox_options(), debug=CAMOUFOX_DEBUG)
    with Camoufox(**launch_kwargs) as browser:
        context = browser.new_context(ignore_https_errors=True)
        context.set_default_timeout(NAVIGATION_TIMEOUT)
        try:
            if not perform_login(context, USERNAME, PASSWORD):
                raise RuntimeError("Login failed. Verify credentials or challenge response.")
            yield context
        finally:
            context.close()


def lookup_last_visited(cursor, topic_id: int) -> int:
    cursor.execute('SELECT last_visited_posts_count FROM visited_topics WHERE topic_id = ?', (topic_id,))
    row = cursor.fetchone()
    return int(row[0]) if row else 1


def persist_last_visited(cursor, topic_id: int, count: int) -> None:
    cursor.execute(
        '''
        INSERT OR REPLACE INTO visited_topics (topic_id, last_visited_posts_count, timestamp)
        VALUES (?, ?, CURRENT_TIMESTAMP)
        ''',
        (topic_id, count),
    )


def highest_post_number(page) -> int:
    """Return the highest post number currently visible on the page."""
    try:
        value = page.evaluate(
            """
() => {
  const nodes = Array.from(document.querySelectorAll('[id^="post_"]'));
  const nums = nodes
    .map(el => Number((el.id || '').split('_')[1]))
    .filter(n => Number.isFinite(n));
  return nums.length ? Math.max(...nums) : 0;
}
"""
        )
        return int(value) if isinstance(value, (int, float)) else 0
    except PlaywrightError:
        return 0


def smooth_scroll(page) -> None:
    """Scroll the page to mimic human behaviour."""
    try:
        page.mouse.wheel(0, SCROLL_STEP)
    except PlaywrightError:
        pass
    page.wait_for_timeout(int(SCROLL_DELAY * 1_000))


def visit_topic(page, cursor, topic_id: int, posts_count: int, last_visited: int) -> None:
    """Visit unread posts for the given topic."""
    if posts_count <= last_visited:
        print(f"Topic {topic_id}: posts_count ({posts_count}) <= last_visited ({last_visited}). Skipping.")
        return

    print(f"Topic {topic_id}: Visiting posts {last_visited} -> {posts_count}")
    current = max(1, last_visited)

    while current < posts_count:
        next_url = f"https://linux.do/t/topic/{topic_id}/{current}"
        success = False
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                page.goto(next_url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT)
                solve_turnstile(page)
                page.wait_for_load_state("networkidle")
                success = True
                break
            except PlaywrightTimeoutError:
                print(f"  -> Timeout navigating to {next_url} (attempt {attempt}/{MAX_RETRIES}).")
            except PlaywrightError as exc:
                print(f"  -> Error navigating to {next_url}: {exc}")
                break
        if not success:
            print(f"  -> Unable to load {next_url}, aborting topic {topic_id}.")
            return

        max_seen = highest_post_number(page)
        smooth_scroll(page)
        max_seen = max(max_seen, current)
        current = min(posts_count, max(current + 1, max_seen))
        persist_last_visited(cursor, topic_id, current)
        cursor.connection.commit()
        print(f"  -> Progressed to post {current} for topic {topic_id}.")

    persist_last_visited(cursor, topic_id, posts_count)
    cursor.connection.commit()
    print(f"Topic {topic_id}: Updated visited_posts.db to {posts_count}.")


def main():
    init_visited_db()
    topics = read_topics()

    visited_conn = sqlite3.connect('visited_posts.db', check_same_thread=False)
    visited_cursor = visited_conn.cursor()

    with camoufox_context() as context:
        page = context.new_page()
        page.set_default_timeout(NAVIGATION_TIMEOUT)
        for topic_id, posts_count in topics:
            try:
                last_visited = lookup_last_visited(visited_cursor, topic_id)
                visit_topic(page, visited_cursor, topic_id, posts_count, last_visited)
            except Exception as exc:  # pylint: disable=broad-except
                print(f"Error processing topic {topic_id}: {exc}")
        page.close()

    visited_conn.close()
    print("Browser closed and script finished.")


if __name__ == "__main__":
    main()
