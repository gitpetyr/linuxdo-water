# tpread.py (修改版，从环境变量读取用户名和密码)

import sqlite3
import time
import os # 导入 os 模块以读取环境变量
import getpass
from DrissionPage import ChromiumPage, ChromiumOptions
from DrissionPage.common import Settings
from DrissionPage.errors import ElementNotFoundError, WaitTimeoutError, ElementLostError, BaseError

# --- 从环境变量读取用户名和密码 ---
USERNAME = os.getenv('LINUX_DO_USERNAME', 'default_user')
PASSWORD = os.getenv('LINUX_DO_PASSWORD', 'default_pass')
if USERNAME == 'default_user' or PASSWORD == 'default_pass':
    print("Warning: Username or Password not found in environment variables. Using defaults. Please check start_crawler_and_visitor.py configuration.")
    # 可以选择在此处退出
    # import sys
    # sys.exit(1)

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

# --- 初始化已访问数据库 ---
def init_visited_db():
    """初始化用于存储已访问帖子信息的数据库"""
    conn = sqlite3.connect('visited_posts.db', check_same_thread=False)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS visited_topics (
            topic_id INTEGER PRIMARY KEY,
            last_visited_posts_count INTEGER,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

# --- 主函数 ---
def main():
    # 初始化已访问数据库
    init_visited_db()

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
        print("Main login completed.")
    except Exception as e:
        print(f"Error during main login: {e}")
        browser.quit()
        return

    # 连接到 topics.db 读取数据
    try:
        topics_conn = sqlite3.connect('topics.db', check_same_thread=False)
        topics_cursor = topics_conn.cursor()
        topics_cursor.execute('SELECT id, posts_count FROM topic_ids ORDER BY id') # 按ID排序便于查看
        all_topics = topics_cursor.fetchall()
        topics_conn.close()
        print(f"Read {len(all_topics)} topics from topics.db")
    except sqlite3.Error as e:
        print(f"Error reading from topics.db: {e}")
        browser.quit()
        return

    # 遍历 topics.db 中的数据
    visit_page = browser.new_tab() # 创建一个标签页用于访问主题页
    visited_conn = sqlite3.connect('visited_posts.db', check_same_thread=False)
    visited_cursor = visited_conn.cursor()

    for topic_id, posts_count in all_topics:
        try:
            # 查询 visited_posts.db 中该 topic_id 的上次访问记录
            visited_cursor.execute('SELECT last_visited_posts_count FROM visited_topics WHERE topic_id = ?', (topic_id,))
            result = visited_cursor.fetchone()
            
            last_visited_count = result[0] if result else 1 # 如果没有记录，则视为1

            if posts_count > last_visited_count:
                print(f"Topic {topic_id}: posts_count ({posts_count}) > last_visited_count ({last_visited_count}). Visiting...")
                
                # 访问主题页面
                url = f"https://linux.do/t/topic/{topic_id}/{last_visited_count}"
                print(f"Visiting: {url}")
                visit_page.get(url)
                visit_page.wait.doc_loaded()
                try :
                    getTurnstileToken(visit_page)
                except Exception:
                    print("  -> No Captcha found or error occurred while solving Captcha.")
                cnt = last_visited_count
                while cnt < posts_count:
                    visit_page.get(f"https://linux.do/t/topic/{topic_id}/{cnt + 1}")
                    visit_page.wait.doc_loaded()
                    try :
                        getTurnstileToken(visit_page)
                    except Exception:
                        print("  -> No Captcha found or error occurred while solving Captcha.")
                    for ele in visit_page.eles("@class:read-state"):
                        try:
                            print(ele.states.is_displayed)
                            if ele.states.is_displayed:
                                visit_page.wait.ele_hidden(ele, timeout=10)
                            cnt = max(cnt, int(ele.parent().parent().parent().parent().parent().attr('id').split("_")[1]))
                            print(ele.parent().parent().parent().parent().parent().attr('id').split("_")[1])
                            # visit_page.scroll.down(300)
                            visit_page.scroll.down(70)
                            visited_cursor.execute('''
                                INSERT OR REPLACE INTO visited_topics (topic_id, last_visited_posts_count)
                                VALUES (?, ?)
                            ''', (topic_id, cnt))
                            visited_conn.commit()
                            # ele.scroll.to_center()
                            visit_page.wait(0.4)
                        except (ElementNotFoundError, ElementLostError, WaitTimeoutError):
                            break
                # --- 在这里添加你需要的访问逻辑 ---
                # 例如：等待页面加载、滚动、查找元素等
                # visit_page.wait.doc_loaded() # 等待页面加载
                # 或者其他操作...
                time.sleep(2) # 模拟访问时间
                
                # 访问成功后，更新 visited_posts.db
                visited_cursor.execute('''
                    INSERT OR REPLACE INTO visited_topics (topic_id, last_visited_posts_count)
                    VALUES (?, ?)
                ''', (topic_id, posts_count))
                visited_conn.commit()
                print(f"  -> Updated visited_posts.db for topic {topic_id} with posts_count {posts_count}.")
            else:
                print(f"Topic {topic_id}: posts_count ({posts_count}) <= last_visited_count ({last_visited_count}). Skipping.")

        except Exception as e:
            print(f"Error processing topic {topic_id}: {e}")
            # 可以选择在此处添加重试逻辑或跳过该主题

    visited_conn.close()
    visit_page.close() # 关闭访问用的标签页

    # 关闭浏览器
    browser.quit()
    print("Browser closed and script finished.")

if __name__ == "__main__":
    main()
