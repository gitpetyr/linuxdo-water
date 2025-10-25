# start
import subprocess
import time
import threading
import signal
import sys
import os

# --- 配置 ---
CRAWLER_SCRIPT = "main.py"  # 爬取器主脚本的文件名
VISITOR_SCRIPT = "tpread.py"  # 水帖器脚本的文件名 (修改这里)
WAIT_BEFORE_VISITOR = 10  # 启动爬取器后，等待多少秒再启动水帖器

DEFAULT_VENV_PY = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".venv", "bin", "python")
PYTHON_EXECUTABLE = os.getenv("PROJECT_PYTHON", DEFAULT_VENV_PY if os.path.exists(DEFAULT_VENV_PY) else sys.executable)

# --- 用户名和密码配置 ---
USERNAME = "petyr"  # 在这里配置用户名
PASSWORD = "your_actual_password_here" # 在这里配置密码，请确保安全性

# 全局变量用于管理子进程和退出信号
processes = {'crawler': None, 'visitor': None}
stop_event = threading.Event()

def signal_handler(signum, frame):
    print(f"\nReceived signal {signum}, stopping all processes...")
    stop_event.set()

def run_script(script_name, process_name, stop_event, wait_time=0, env_vars=None):
    """运行一个Python脚本作为子进程，并监控其状态"""
    global processes
    try:
        # 等待指定时间（如果需要）
        if wait_time > 0:
            print(f"Waiting {wait_time} seconds before starting {process_name}...")
            time.sleep(wait_time)

        print(f"--- Starting {process_name}: {script_name} ---")
        # 准备环境变量
        env = os.environ.copy() # 复制当前环境
        if env_vars:
            env.update(env_vars) # 添加或覆盖特定环境变量

        # 使用 Popen 启动进程，并捕获输出（可选）
        proc = subprocess.Popen([PYTHON_EXECUTABLE, script_name],
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                                text=True,
                                env=env) # 传递修改后的环境
        processes[process_name] = proc
        print(f"Started {process_name} process (PID: {proc.pid})")

        # 等待进程结束或被信号中断
        while proc.poll() is None and not stop_event.is_set(): # poll() 检查进程是否结束
            time.sleep(1)

        # 检查进程退出码
        if proc.poll() is not None:
            print(f"--- {process_name} ({script_name}) finished with return code {proc.returncode} ---")
            if proc.returncode != 0:
                stderr_output = proc.stderr.read() if proc.stderr else "No stderr captured"
                print(f"--- {process_name} stderr: {stderr_output} ---")

    except FileNotFoundError:
        print(f"--- Script file not found: {script_name} ---")
    except Exception as e:
        print(f"--- Unexpected error running {process_name} ({script_name}): {e} ---")
    finally:
        # 确保进程引用被清理
        processes[process_name] = None

def main():
    print("Starting Crawler and Visitor launcher with credentials...")

    # 注册信号处理器
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # 准备要传递给子进程的环境变量
    env_vars = {
        'LINUX_DO_USERNAME': USERNAME,
        'LINUX_DO_PASSWORD': PASSWORD
    }

    # 创建并启动线程来运行爬取器，传递环境变量
    crawler_thread = threading.Thread(target=run_script, args=(CRAWLER_SCRIPT, 'crawler', stop_event, 0, env_vars), name="CrawlerLauncher")
    # 创建并启动线程来运行水帖器，传递环境变量，设置等待时间
    visitor_thread = threading.Thread(target=run_script, args=(VISITOR_SCRIPT, 'visitor', stop_event, WAIT_BEFORE_VISITOR, env_vars), name="VisitorLauncher")

    crawler_thread.start()
    visitor_thread.start()

    try:
        # 等待两个线程（它们会等待子进程结束或被停止）完成
        crawler_thread.join()
        visitor_thread.join()
        print("Both Crawler and Visitor launcher threads have stopped.")
    except KeyboardInterrupt:
        print("\nMain script interrupted.")
        stop_event.set() # 设置停止事件
        # 等待线程响应
        crawler_thread.join()
        visitor_thread.join()

    # 确保所有子进程都被终止
    for name, proc in processes.items():
        if proc and proc.poll() is None: # 检查进程是否仍在运行
            print(f"Ensuring {name} process (PID: {proc.pid}) is terminated...")
            proc.terminate() # 尝试优雅终止
            try:
                proc.wait(timeout=10) # 等待最多10秒
            except subprocess.TimeoutExpired:
                print(f"Force killing {name} process (PID: {proc.pid})...")
                proc.kill() # 强制杀死

    print("Launcher script finished.")


if __name__ == "__main__":
    main()
