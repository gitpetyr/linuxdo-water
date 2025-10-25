# main.py (修改版，从环境变量读取用户名和密码)

import threading
import sqlite3
import time
import os # 导入 os 模块以读取环境变量
import json
from DrissionPage import ChromiumPage, ChromiumOptions
from DrissionPage.common import Settings
from DrissionPage.errors import ElementNotFoundError, WaitTimeoutError

# --- 从环境变量读取用户名和密码 ---
USERNAME = os.getenv('LINUX_DO_USERNAME', 'default_user') # 如果环境变量未设置，使用 'default_user'
PASSWORD = os.getenv('LINUX_DO_PASSWORD', 'default_pass') # 如果环境变量未设置，使用 'default_pass'
if USERNAME == 'default_user' or PASSWORD == 'default_pass':
    print("Warning: Username or Password not found in environment variables. Using defaults. Please check start_crawler_and_visitor.py configuration.")
    # 可以选择在此处退出
    # sys.exit(1)

# --- 全局变量和锁 ---
# 用于保护共享资源（数据库连接和ID集合）的锁
db_lock = threading.Lock()
id_set_lock = threading.Lock()

# 用于在线程间通信，标记是否应该停止
stop_event = threading.Event()

# 共享的ID集合，存储新获取或更新的ID数据 (id, posts_count)
id_data_set = set() # 使用元组 (id, posts_count) 作为元素

# --- 数据库初始化 ---
def init_db():
    """初始化SQLite数据库和表"""
    conn = sqlite3.connect('topics.db', check_same_thread=False) # 允许在不同线程中访问
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS topic_ids (
            id INTEGER PRIMARY KEY,
            posts_count INTEGER,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

# --- 获取Turnstile Token ---
def getTurnstileToken(page: ChromiumPage, times: int = 10):
    page.run_js("try { turnstile.reset() } catch(e) { }")
    turnstileResponse = None
    page.wait.ele_displayed("@name=cf-turnstile-response")

    for i in range(0, times):
        try:
            print(f"Main Thread: Finding Captcha")
            turnstileResponse = page.run_js("try { return turnstile.getResponse() } catch(e) { return null }")
            if turnstileResponse:
                print(f"Main Thread: Captcha Passed")
                return

            challengeSolution = page.ele("@name=cf-turnstile-response")
            challengeWrapper = challengeSolution.parent()
            challengeIframe = challengeWrapper.shadow_root.ele("tag:iframe")

            challengeIframe.run_js("""
window.dtp = 1
function getRandomInt(min, max) {
    return Math.floor(Math.random() * (max - min + 1)) + min;
}

let screenX = getRandomInt(800, 1200);
let screenY = getRandomInt(400, 600);

Object.defineProperty(MouseEvent.prototype, 'screenX', { value: screenX });
Object.defineProperty(MouseEvent.prototype, 'screenY', { value: screenY });
                        """)

            challengeIframeBody = challengeIframe.ele("tag:body").shadow_root
            challengeButton = challengeIframeBody.ele("tag:input")
            challengeButton.click()
            print(f"Main Thread: Try Captcha again")
        except:
            pass
        time.sleep(0.8)
    page.refresh()
    print(f"Main Thread: Tried Captcha, refreshing")
    return

# --- 向数据库添加或更新ID和posts_count ---
def add_or_update_ids_in_db(data_to_upsert):
    """将ID和posts_count列表安全地添加到数据库中 (使用 INSERT OR REPLACE)"""
    if not data_to_upsert:
        return

    with db_lock:
        try:
            conn = sqlite3.connect('topics.db', check_same_thread=False)
            cursor = conn.cursor()
            # 使用 INSERT OR REPLACE INTO 来更新或插入
            cursor.executemany('INSERT OR REPLACE INTO topic_ids (id, posts_count) VALUES (?, ?)', data_to_upsert)
            conn.commit()
            conn.close()
            print(f"Thread {threading.current_thread().name}: Upserted {len(data_to_upsert)} records (ID/posts_count) to database.")
        except sqlite3.Error as e:
            print(f"Database error in {threading.current_thread().name}: {e}")

# --- 线程1: 持续关注 page 0 和 1 ---
def thread1_worker(browser, stop_event):
    """线程1的工作函数，持续关注 page 0 和 1"""
    print(f"Thread {threading.current_thread().name} started, monitoring pages 0 and 1.")
    page = browser.new_tab() # 新标签页会继承会话

    # 等待页面加载完成（虽然没有特定的URL变化，但可以等待文档加载）
    page.wait.doc_loaded()

    while not stop_event.is_set():
        pages_to_check = [0, 1]
        for pg_num in pages_to_check:
            if stop_event.is_set():
                break
            try:
                url = f"https://linux.do/latest.json?no_definitions=true&page={pg_num}"
                print(f"Thread {threading.current_thread().name}: Fetching {url}")
                page.get(url)
                page.wait.doc_loaded()

                if page.json and 'topic_list' in page.json and 'topics' in page.json['topic_list']:
                    info = page.json['topic_list']['topics']
                    current_data = {(i['id'], i['posts_count']) for i in info} # 提取 (id, posts_count) 元组
                    print(f"Thread {threading.current_thread().name}: Got {len(current_data)} ID/posts_count pairs from page {pg_num}")

                    # 检查是否有新ID或posts_count有变化
                    with id_set_lock:
                        # 找出需要更新或插入的数据
                        data_to_upsert = current_data - id_data_set
                        if data_to_upsert:
                            id_data_set.update(data_to_upsert)
                            # 将新/更新的数据添加到数据库
                            add_or_update_ids_in_db(list(data_to_upsert))
                else:
                    # 检查是否是错误响应 {"error_type":"invalid_parameters"}
                    if page.json and page.json.get('error_type') == 'invalid_parameters':
                        print(f"Thread {threading.current_thread().name}: Received 'invalid_parameters' error on page {pg_num}. Stopping thread.")
                        stop_event.set() # 通知其他线程停止
                        break
                    print(f"Thread {threading.current_thread().name}: Unexpected response format on page {pg_num}")

            except (ElementNotFoundError, WaitTimeoutError):
                print(f"Thread {threading.current_thread().name}: Failed to get JSON from page {pg_num}, trying captcha...")
                getTurnstileToken(page)
                # 重试一次
                try:
                    page.get(f"https://linux.do/latest.json?no_definitions=true&page={pg_num}")
                    page.wait.doc_loaded()
                    if page.json and 'topic_list' in page.json and 'topics' in page.json['topic_list']:
                        info = page.json['topic_list']['topics']
                        current_data = {(i['id'], i['posts_count']) for i in info}
                        print(f"Thread {threading.current_thread().name}: Got {len(current_data)} ID/posts_count pairs from page {pg_num} after captcha.")
                        with id_set_lock:
                            data_to_upsert = current_data - id_data_set
                            if data_to_upsert:
                                id_data_set.update(data_to_upsert)
                                add_or_update_ids_in_db(list(data_to_upsert))
                except:
                    print(f"Thread {threading.current_thread().name}: Retry failed for page {pg_num}.")

            time.sleep(1) # 避免过于频繁的请求

    print(f"Thread {threading.current_thread().name} finished.")
    page.close() # 线程结束时关闭标签页

# --- 线程2: 向后枚举页面 (单次执行) ---
def thread2_worker_single_run(browser, start_page, stop_event):
    """线程2的工作函数，从指定页码开始向后枚举，直到遇到错误或被停止信号中断。此函数执行一次完整的枚举任务。"""
    print(f"Thread {threading.current_thread().name} (Single Run) started, enumerating pages starting from {start_page}.")
    page = browser.new_tab() # 新标签页会继承会话

    # 等待页面加载完成
    page.wait.doc_loaded()

    pg_num = start_page
    task_completed_successfully = False # 标记任务是否因遇到错误而完成

    while not stop_event.is_set():
        try:
            url = f"https://linux.do/latest.json?no_definitions=true&page={pg_num}"
            print(f"Thread {threading.current_thread().name}: Fetching {url}")
            page.get(url)
            page.wait.doc_loaded()

            if page.json and 'topic_list' in page.json and 'topics' in page.json['topic_list']:
                info = page.json['topic_list']['topics']
                current_data = {(i['id'], i['posts_count']) for i in info} # 提取 (id, posts_count) 元组
                print(f"Thread {threading.current_thread().name}: Got {len(current_data)} ID/posts_count pairs from page {pg_num}")

                with id_set_lock:
                    # 找出需要更新或插入的数据
                    data_to_upsert = current_data - id_data_set
                    if data_to_upsert:
                        id_data_set.update(data_to_upsert)
                        add_or_update_ids_in_db(list(data_to_upsert))

                pg_num += 1 # 移动到下一页
            else:
                # 检查是否是错误响应 {"error_type":"invalid_parameters"}
                if page.json and page.json.get('error_type') == 'invalid_parameters':
                    print(f"Thread {threading.current_thread().name}: Received 'invalid_parameters' error on page {pg_num}. Task completed.")
                    task_completed_successfully = True
                    break # 任务完成，退出循环
                else:
                    print(f"Thread {threading.current_thread().name}: Unexpected response format on page {pg_num}, stopping enumeration.")
                    break # 遇到未知错误，退出循环

        except (ElementNotFoundError, WaitTimeoutError):
            print(f"Thread {threading.current_thread().name}: Failed to get JSON from page {pg_num}, trying captcha...")
            getTurnstileToken(page)
            # 重试一次
            try:
                page.get(f"https://linux.do/latest.json?no_definitions=true&page={pg_num}")
                page.wait.doc_loaded()
                if page.json and 'topic_list' in page.json and 'topics' in page.json['topic_list']:
                    info = page.json['topic_list']['topics']
                    current_data = {(i['id'], i['posts_count']) for i in info}
                    print(f"Thread {threading.current_thread().name}: Got {len(current_data)} ID/posts_count pairs from page {pg_num} after captcha.")
                    with id_set_lock:
                        data_to_upsert = current_data - id_data_set
                        if data_to_upsert:
                            id_data_set.update(data_to_upsert)
                            add_or_update_ids_in_db(list(data_to_upsert))
                    pg_num += 1
                else:
                     if page.json and page.json.get('error_type') == 'invalid_parameters':
                         print(f"Thread {threading.current_thread().name}: Received 'invalid_parameters' error on page {pg_num} after retry. Task completed.")
                         task_completed_successfully = True
                         break # 任务完成，退出循环
                     else:
                         print(f"Thread {threading.current_thread().name}: Unexpected response after retry on page {pg_num}, stopping enumeration.")
                         break # 遇到未知错误，退出循环
            except:
                print(f"Thread {threading.current_thread().name}: Retry failed for page {pg_num}. Stopping enumeration.")
                break # 重试失败，退出循环

        time.sleep(1) # 避免过于频繁的请求

    print(f"Thread {threading.current_thread().name} (Single Run) finished.")
    page.close() # 线程结束时关闭标签页
    return task_completed_successfully # 返回任务是否因遇到 'invalid_parameters' 而自然结束

# --- 线程2管理器 ---
def thread2_manager(browser, start_page, stop_event, restart_delay):
    """管理线程2的生命周期：启动、等待完成、根据结果和时间间隔决定是否重启"""
    print("Thread2 Manager started.")
    last_run_start_time = time.time() # 记录本次运行的开始时间

    while not stop_event.is_set():
        # 检查是否需要重启（基于时间）
        elapsed_time = time.time() - last_run_start_time
        if elapsed_time >= restart_delay:
            print(f"Thread2 Manager: {restart_delay/60:.0f} minutes elapsed since last run start. Preparing to restart thread2.")

        # 创建一个新的停止事件，用于控制当前运行的 thread2
        current_thread2_stop_event = threading.Event()

        # 启动线程2
        thread2 = threading.Thread(target=thread2_worker_single_run, args=(browser, start_page, current_thread2_stop_event), name="EnumeratePages")
        thread2.start()
        print(f"Thread2 Manager: Started new thread2 instance (PID simulated via thread).")

        # 等待线程2完成其任务或被外部 stop_event 停止
        while thread2.is_alive():
            if stop_event.is_set(): # 如果外部停止信号被设置，则停止当前 thread2
                print("Thread2 Manager: Received global stop signal, stopping current thread2.")
                current_thread2_stop_event.set()
                break
            time.sleep(1) # 等待线程2结束或全局停止信号

        thread2.join() # 确保线程对象被清理
        print("Thread2 Manager: Current thread2 instance finished.")

        # 检查是否是外部停止信号导致的结束
        if stop_event.is_set():
            print("Thread2 Manager: Global stop signal received, exiting manager.")
            break

        # 检查线程2是否是因遇到 'invalid_parameters' 而自然结束
        # 注意：由于线程函数的返回值无法直接从 join() 获取，我们需要用一个共享变量或队列来传递结果
        # 这里简化处理，假设只要线程正常退出（没有被强制停止），就认为任务完成了
        # 更精确的方式是使用 Queue 或修改线程函数使其设置一个共享标志
        # 我们可以引入一个标志来近似判断
        task_completed_naturally = not current_thread2_stop_event.is_set() # 如果 current_thread2_stop_event 没有被设置，则认为是自然结束
        if task_completed_naturally:
            print(f"Thread2 Manager: Thread2 completed its task naturally. Waiting {restart_delay} seconds before restarting...")
            # 等待指定时间后重启
            # 在等待期间，持续检查全局停止信号
            wait_start = time.time()
            while time.time() - wait_start < restart_delay and not stop_event.is_set():
                time.sleep(1)
            
            if stop_event.is_set():
                 print("Thread2 Manager: Global stop signal received during wait, exiting manager.")
                 break
            
            print(f"Thread2 Manager: Wait finished. Restarting thread2 now.")
            last_run_start_time = time.time() # 重置计时器为下一次运行开始
        else:
            print("Thread2 Manager: Thread2 was stopped externally or crashed. Restarting immediately.")
            # 如果是外部停止或崩溃，则立即重启，并重置计时器
            last_run_start_time = time.time()


    print("Thread2 Manager finished.")

# --- 主函数 ---
def main():
    # 初始化数据库
    init_db()

    # 设置 Chromium 选项
    co = ChromiumOptions()
    co.auto_port()
    co.set_timeouts(base=1)
    EXTENSION_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "turnstilePatch"))
    co.add_extension(EXTENSION_PATH)

    # 创建浏览器实例
    browser = ChromiumPage(co)

    try:
        # 登录主页面 (在主线程中执行一次)
        main_page = browser.new_tab()
        main_page.get("https://linux.do/login")
        # 执行登录逻辑
        getTurnstileToken(main_page)
        main_page.wait.ele_displayed("@id=login-account-name")
        main_page.ele("@id=login-account-name").input(USERNAME) # 使用环境变量中的用户名
        main_page.ele("@id=login-account-password").input(PASSWORD) # 使用环境变量中的密码
        main_page.ele("@id=login-button").click()
        time.sleep(2)
        main_page.wait.url_change("https://linux.do", timeout=10)
        main_page.wait.doc_loaded()
        main_page.close() # 关闭登录用的标签页，线程会创建自己的
        print("Main login completed.")
    except Exception as e:
        print(f"Error during main login: {e}")
        browser.quit()
        return

    # 创建并启动线程1
    thread1 = threading.Thread(target=thread1_worker, args=(browser, stop_event), name="MonitorPages01")
    # 创建并启动线程2管理器 (它会负责启动和重启线程2)
    thread2_manager_thread = threading.Thread(target=thread2_manager, args=(browser, 2, stop_event, 90*60), name="Thread2Manager") # 从 page 2 开始，90分钟 = 90*60秒

    thread1.start()
    thread2_manager_thread.start()

    try:
        # 等待主线程被中断
        while True:
            time.sleep(10) # 主线程简单休眠，等待中断信号
            if stop_event.is_set():
                break
    except KeyboardInterrupt:
        print("\nReceived interrupt signal, stopping all threads...")
        stop_event.set()

    # 等待线程完成
    thread1.join()
    thread2_manager_thread.join()

    # 关闭浏览器
    browser.quit()
    print("Browser closed.")

if __name__ == "__main__":
    main()
