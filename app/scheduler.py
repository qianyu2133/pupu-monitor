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
    """购物车轮询间隔。出清池走独立的低频任务，不受这里影响。"""
    p = config.load()["poll"]
    h = time.localtime().tm_hour
    ps, pe = int(p.get("peak_start", 21)), int(p.get("peak_end", 23))
    peak = (ps <= h < pe) if ps <= pe else (h >= ps or h < pe)
    if peak:
        base = float(p.get("cart_peak_interval", 8))
    else:
        base = float(p.get("cart_idle_interval", 60))
    j = float(p.get("jitter", 0.3))
    return max(5.0, base * random.uniform(1 - j, 1 + j))


class Monitor:
    def __init__(self):
        self._thread = None
        self._stop = threading.Event()
        self.state = {}  # cred_id -> {last_run, last_error, next_at, platform, account}
        self.last_tick = 0
        self.requested_at = 0
        self.scan_next_at = 0
        self.scan_busy = False
        self.last_scan = 0

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
            self._maybe_scan(now)
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

    def _maybe_scan(self, now):
        """出清池全量抓取：低频、独立线程，不占用购物车的轮询节奏。"""
        interval = float(config.load()["poll"].get("scan_interval", 1800) or 0)
        if interval <= 0 or now < self.scan_next_at or self.scan_busy:
            return
        self.scan_busy = True
        self.scan_next_at = now + interval
        threading.Thread(target=self._scan_tick, name="scan", daemon=True).start()

    def _scan_tick(self):
        try:
            cred = next(
                (c for c in db.list_credentials()
                 if c["platform"] == "pupu" and c["status"] == "ok"),
                None,
            )
            if not cred:
                return
            rows = PupuAdapter(cred).fetch_crazy_discount()
            written, removed = db.replace_scan("pupu", rows, "疯狂折扣")
            self.last_scan = int(time.time())
            print(f"[scan] 自动抓取：{len(rows)} 件有效，写入 {written}，清理过期 {removed}")
        except AuthExpired as e:
            print(f"[scan] 凭证失效，跳过本轮：{e}")
        except Exception as e:
            print(f"[scan] 自动抓取失败：{type(e).__name__}: {e}")
        finally:
            self.scan_busy = False

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
