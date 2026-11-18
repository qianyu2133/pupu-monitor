"""桌面版入口：原生窗口（WebView2），不经过外部浏览器。

开发时：python desktop.py
打包后：双击 exe
"""
import os
import sys
import threading
import time
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import uvicorn

# 打包成无控制台窗口时，把输出重定向到 data/app.log，方便排查
if getattr(sys, "frozen", False):
    try:
        _logdir = os.path.join(os.path.dirname(os.path.abspath(sys.executable)), "data")
        os.makedirs(_logdir, exist_ok=True)
        _f = open(os.path.join(_logdir, "app.log"), "a", encoding="utf-8", buffering=1)
        sys.stdout = _f
        sys.stderr = _f
    except Exception:
        pass

from app import db, sysproxy
from app.proxy_host import proxy
from app.web import app

PORT = 8787
URL = f"http://127.0.0.1:{PORT}"


def _serve():
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning", access_log=False)


def _wait_ready(timeout=20):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(URL + "/api/setup", timeout=1) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.25)
    return False


def main():
    db.init()
    sysproxy.heal_on_start()   # 上次被强杀可能把系统代理留在本程序上 → 先还原
    proxy.start()

    threading.Thread(target=_serve, name="web", daemon=True).start()
    ready = _wait_ready()

    win = None
    try:
        import webview

        win = webview.create_window(
            "出清监控",
            URL if ready else "about:blank",
            width=1180,
            height=860,
            min_size=(980, 700),
            background_color="#FFFFFF",
        )
    except Exception as e:
        print(f"[warn] 原生窗口不可用，改用浏览器：{e}")
        win = None

    try:
        if win is not None:
            webview.start()          # 阻塞，直到窗口关闭
        else:
            import webbrowser

            webbrowser.open(URL)
            while True:
                time.sleep(0.8)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            sysproxy.turn_off()
        except Exception:
            pass
        proxy.stop()
        print("已退出，系统代理已还原")


if __name__ == "__main__":
    main()
