"""进程内嵌 mitmproxy：不弹黑窗口，随应用一起启停。"""
import asyncio
import os
import threading
import time

from mitmproxy import options
from mitmproxy.tools import dump

from .paths import root as _root

ROOT = _root()
CONFDIR = os.path.join(ROOT, "data", "mitm")
PORT = 8080


class ProxyHost:
    def __init__(self):
        self._thread = None
        self._master = None
        self._loop = None
        self.running = False
        self.error = None

    def start(self):
        if self.running:
            return True

        def run():
            async def main():
                opts = options.Options(
                    listen_host="127.0.0.1",
                    listen_port=PORT,
                    confdir=CONFDIR,
                )
                master = dump.DumpMaster(opts)
                try:
                    from tools.grab_token import Grabber

                    master.addons.add(Grabber())
                except Exception as e:
                    self.error = f"插件加载失败: {e}"
                    print(self.error)
                self._master = master
                self.running = True
                self._loop = asyncio.get_running_loop()
                await master.run()

            try:
                asyncio.run(main())
            except Exception as e:
                self.error = f"代理异常: {e}"
                print(self.error)
            finally:
                self.running = False

        self._thread = threading.Thread(target=run, name="mitmproxy", daemon=True)
        self._thread.start()

        for _ in range(40):
            time.sleep(0.15)
            if self.running:
                return True
        return self.running

    def stop(self):
        m = self._master
        if m:
            try:
                m.shutdown()
            except Exception:
                pass
        self.running = False
        self._master = None
        return True

    @property
    def ca_file(self):
        return os.path.join(CONFDIR, "mitmproxy-ca-cert.cer")


proxy = ProxyHost()
