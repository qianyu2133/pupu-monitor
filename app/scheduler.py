import random
import threading
import time
import traceback

from . import db, config, notify, rules
from .adapters.base import AdapterError, AuthExpired, NotConfigured
from .adapters.mock import MockAdapter
from .adapters.pupu import PupuAdapter

BUILDERS = {"pupu": PupuAdapter, "mock": MockAdapter}


def current_interval():
    p = config.load()["poll"]
    h = time.localtime().tm_hour
    ps, pe = int(p.get("peak_start", 21)), int(p.get("peak_end", 23))
    peak = (ps <= h < pe) if ps <= pe else (h >= ps or h < pe)
    base = float(p.get("peak_interval", 12) if peak else p.get("idle_interval", 180))
    j = float(p.get("jitter", 0.3))
    return max(5.0, base * random.uniform(1 - j, 1 + j))


class Monitor:
    def __init__(self):
        self._thread = None
        self._stop = threading.Event()
        self.state = {}  # cred_id -> {last_run, last_error, next_at, platform, account}
        self.last_tick = 0
        self.requested_at = 0

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="monitor", daemon=True)
        self._thread.start()
        print("[monitor] 已启动")

    def stop(self):
        self._stop.set()
        print("[monitor] 停止中")

    def request_refresh(self):
        """手动刷新：把所有账号的 next_at 清零，下一轮（1 秒内）立刻拉。"""
        self.requested_at = int(time.time())
        for st in self.state.values():
            st["next_at"] = 0
        # 还没跑过首次的账号，补一条空状态让它立刻被抓到
        for cred in db.list_credentials():
            self.state.setdefault(
                cred["id"],
                {"platform": cred["platform"], "account": cred["account"],
                 "last_run": None, "last_error": None, "next_at": 0, "hits": 0},
            )
        return self.requested_at

    def _loop(self):
        while not self._stop.is_set():
            self.last_tick = int(time.time())
            if not db.monitor_enabled():
                time.sleep(2)
                continue
            try:
                creds = db.list_credentials()
            except Exception as e:
                print(f"[monitor] 读凭证失败: {e}")
                time.sleep(5)
                continue

            now = time.time()
            for cred in creds:
                st = self.state.setdefault(
                    cred["id"],
                    {"platform": cred["platform"], "account": cred["account"],
                     "last_run": None, "last_error": None, "next_at": 0, "hits": 0},
                )
                if cred["status"] != "ok":
                    st["next_at"] = now + 300
                    continue
                if now < st.get("next_at", 0):
                    continue
                self._tick(cred, st)
                st["next_at"] = time.time() + current_interval()
            time.sleep(1)

    def _tick(self, cred, st):
        builder = BUILDERS.get(cred["platform"])
        if not builder:
            st["last_error"] = f"未知平台 {cred['platform']}"
            return
        try:
            adapter = builder(cred)
            items = adapter.fetch_cart()
            hits = rules.process(items)
            st.update(last_run=int(time.time()), last_error=None, items=len(items))
            st["hits"] = st.get("hits", 0) + hits
        except AuthExpired as e:
            db.set_status(cred["id"], "expired")
            st["last_error"] = f"凭证失效：{e}"
            notify.auth_expired(cred["platform"], cred["account"], str(e))
            print(f"[monitor] {cred['platform']}/{cred['account']} 凭证失效: {e}")
        except NotConfigured as e:
            st["last_error"] = f"待配置：{e}"
        except AdapterError as e:
            st["last_error"] = f"适配器错误：{e}"
        except Exception as e:
            st["last_error"] = f"{type(e).__name__}: {e}"
            print(f"[monitor] 异常: {e}")
            traceback.print_exc(limit=2)


monitor = Monitor()
