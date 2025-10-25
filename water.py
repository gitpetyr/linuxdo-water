"""Crawler entry rewritten to use Camoufox instead of DrissionPage."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Iterable, Optional, Sequence, Set, Tuple

from camoufox import Camoufox, launch_options
from playwright.sync_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError

from camoufox_helpers import perform_login, solve_turnstile

USERNAME = os.getenv('LINUX_DO_USERNAME', 'default_user')
PASSWORD = os.getenv('LINUX_DO_PASSWORD', 'default_pass')
if USERNAME == 'default_user' or PASSWORD == 'default_pass':
    print("Warning: Username or Password missing in environment variables. Check start.py configuration.")

CAMOUFOX_HEADLESS = os.getenv('CAMOUFOX_HEADLESS', '0') == '1'
CAMOUFOX_DEBUG = os.getenv('CAMOUFOX_DEBUG', '0') == '1'
ENUMERATOR_DELAY = int(os.getenv('WATER_ENUMERATOR_DELAY_SECONDS', str(90 * 60)))
NAVIGATION_TIMEOUT = int(os.getenv('WATER_NAV_TIMEOUT_MS', '15000'))
JSON_FETCH_RETRIES = int(os.getenv('WATER_JSON_RETRIES', '2'))
RESTART_DELAY_SECONDS = int(os.getenv('WATER_BROWSER_RESTART_DELAY', '5'))

db_lock = threading.Lock()
id_set_lock = threading.Lock()
stop_event = threading.Event()
id_data_set: Set[Tuple[int, int]] = set()


def init_db() -> None:
    """Initialize SQLite database and schema."""
    conn = sqlite3.connect('topics.db', check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute(
        '''
        CREATE TABLE IF NOT EXISTS topic_ids (
            id INTEGER PRIMARY KEY,
            posts_count INTEGER,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        '''
    )
    conn.commit()
    conn.close()


def add_or_update_ids_in_db(data_to_upsert: Sequence[Tuple[int, int]]) -> None:
    """Insert or update `(id, posts_count)` rows into the database."""
    if not data_to_upsert:
        return
    with db_lock:
        try:
            conn = sqlite3.connect('topics.db', check_same_thread=False)
            cursor = conn.cursor()
            cursor.executemany('INSERT OR REPLACE INTO topic_ids (id, posts_count) VALUES (?, ?)', data_to_upsert)
            conn.commit()
            conn.close()
            print(f"Thread {threading.current_thread().name}: Upserted {len(data_to_upsert)} records.")
        except sqlite3.Error as exc:
            print(f"Database error in {threading.current_thread().name}: {exc}")


def build_camoufox_options():
    """Produce Camoufox launch options."""
    return launch_options(
        headless=CAMOUFOX_HEADLESS,
        disable_coop=True,
        humanize=True,
        block_images=True,
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


def wait_with_stop(seconds: int) -> None:
    """Sleep while respecting the global stop flag."""
    end = time.time() + seconds
    while time.time() < end and not stop_event.is_set():
        time.sleep(1)


def fetch_json_payload(page, url: str) -> Optional[dict]:
    """Load JSON data and handle captcha retries."""
    for _ in range(JSON_FETCH_RETRIES):
        if stop_event.is_set():
            return None
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT)
            raw = page.evaluate("() => document.body ? document.body.innerText : ''")
            if not raw:
                raise ValueError("Empty response body.")
            return json.loads(raw)
        except (ValueError, json.JSONDecodeError) as exc:
            print(f"{threading.current_thread().name}: JSON decode failure ({exc}); attempting captcha solve.")
            solve_turnstile(page)
        except PlaywrightTimeoutError:
            print(f"{threading.current_thread().name}: Navigation timed out for {url}; retrying.")
            solve_turnstile(page)
        except PlaywrightError as exc:
            print(f"{threading.current_thread().name}: Playwright error for {url}: {exc}")
            break
    return None


def parse_topics(topics: Iterable[dict]) -> Set[Tuple[int, int]]:
    """Extract `(id, posts_count)` tuples from topic payload."""
    extracted: Set[Tuple[int, int]] = set()
    for topic in topics:
        try:
            topic_id = int(topic['id'])
            posts_count = int(topic.get('posts_count', 0))
            extracted.add((topic_id, posts_count))
        except (KeyError, TypeError, ValueError):
            continue
    return extracted


def handle_topics(topics_payload: Iterable[dict], thread_name: str) -> None:
    """Persist new or updated topic entries."""
    current_data = parse_topics(topics_payload)
    print(f"Thread {thread_name}: Got {len(current_data)} ID/posts_count pairs.")
    if not current_data:
        return
    with id_set_lock:
        delta = current_data - id_data_set
        if not delta:
            return
        id_data_set.update(delta)
    add_or_update_ids_in_db(list(delta))


def monitor_pages(page) -> None:
    """Watch the latest feed pages (0 & 1)."""
    print(f"Thread {threading.current_thread().name} started, monitoring pages 0 and 1.")
    while not stop_event.is_set():
        for pg_num in (0, 1):
            if stop_event.is_set():
                break
            url = f"https://linux.do/latest.json?no_definitions=true&page={pg_num}"
            payload = fetch_json_payload(page, url)
            if payload is None:
                continue
            if payload.get('error_type') == 'invalid_parameters':
                print(f"Thread {threading.current_thread().name}: Received 'invalid_parameters' for page {pg_num}.")
                stop_event.set()
                return
            topics = payload.get('topic_list', {}).get('topics') if isinstance(payload, dict) else None
            if not topics:
                print(f"Thread {threading.current_thread().name}: Unexpected response format on page {pg_num}")
                continue
            handle_topics(topics, threading.current_thread().name)
        wait_with_stop(1)
    print(f"Thread {threading.current_thread().name} finished.")


def enumerator_run(page, start_page: int) -> bool:
    """Enumerate older pages until an invalid_parameters response."""
    print(f"Thread {threading.current_thread().name} (Single Run) started from page {start_page}.")
    pg_num = start_page
    while not stop_event.is_set():
        url = f"https://linux.do/latest.json?no_definitions=true&page={pg_num}"
        payload = fetch_json_payload(page, url)
        if payload is None:
            print(f"Thread {threading.current_thread().name}: Failed to fetch data for page {pg_num}, stopping enumeration.")
            return False
        if payload.get('error_type') == 'invalid_parameters':
            print(f"Thread {threading.current_thread().name}: Received 'invalid_parameters' on page {pg_num}. Task completed.")
            return True
        topics = payload.get('topic_list', {}).get('topics') if isinstance(payload, dict) else None
        if not topics:
            print(f"Thread {threading.current_thread().name}: Unexpected response format on page {pg_num}, stopping enumeration.")
            return False
        handle_topics(topics, threading.current_thread().name)
        pg_num += 1
        wait_with_stop(1)
    print(f"Thread {threading.current_thread().name}: Stop signal received, ending enumeration.")
    return False


def monitor_thread_worker():
    """Thread entry for page monitoring."""
    while not stop_event.is_set():
        try:
            with camoufox_context() as context:
                page = context.new_page()
                page.set_default_timeout(NAVIGATION_TIMEOUT)
                monitor_pages(page)
                page.close()
                return
        except Exception as exc:  # pylint: disable=broad-except
            if stop_event.is_set():
                break
            print(f"Thread {threading.current_thread().name}: Monitor loop crashed: {exc}. Restarting in {RESTART_DELAY_SECONDS}s.")
            wait_with_stop(RESTART_DELAY_SECONDS)


def enumerator_manager(start_page: int):
    """Thread entry for enumerating older pages."""
    while not stop_event.is_set():
        try:
            with camoufox_context() as context:
                page = context.new_page()
                page.set_default_timeout(NAVIGATION_TIMEOUT)
                completed = enumerator_run(page, start_page)
                page.close()
        except Exception as exc:  # pylint: disable=broad-except
            if stop_event.is_set():
                break
            print(f"Thread {threading.current_thread().name}: Enumerator crashed: {exc}")
            completed = False

        if stop_event.is_set():
            break

        if completed:
            print(f"Thread {threading.current_thread().name}: Enumeration completed. Waiting {ENUMERATOR_DELAY} seconds.")
            wait_with_stop(ENUMERATOR_DELAY)
        else:
            print(f"Thread {threading.current_thread().name}: Enumeration failed. Restarting after {RESTART_DELAY_SECONDS} seconds.")
            wait_with_stop(RESTART_DELAY_SECONDS)


def main():
    init_db()
    monitor_thread = threading.Thread(target=monitor_thread_worker, name="MonitorPages01")
    enumerator_thread = threading.Thread(target=enumerator_manager, args=(2,), name="Thread2Manager")
    monitor_thread.start()
    enumerator_thread.start()

    try:
        while monitor_thread.is_alive() and enumerator_thread.is_alive():
            time.sleep(5)
    except KeyboardInterrupt:
        print("\nReceived interrupt signal, stopping all threads...")
        stop_event.set()

    stop_event.set()
    monitor_thread.join()
    enumerator_thread.join()
    print("All threads stopped.")


if __name__ == "__main__":
    main()
