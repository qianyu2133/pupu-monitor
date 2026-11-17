"""启动抓包代理（给 start_capture.bat 调用的，也可以直接命令行跑）。

    python tools/capture.py          正常启动
    python tools/capture.py --check  只检查环境，不启动

会自动找一个装了 mitmproxy 的 Python——双击时 cmd 里的 python
和开发用的可能不是同一个，所以不能直接用 sys.executable。
"""
import os
import socket
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _pyfind

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 8080

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def find_mitmdump():
    py = _pyfind.find(["mitmproxy"])
    if not py:
        return None, None
    scripts = os.path.join(os.path.dirname(py), "Scripts")
    for name in ("mitmdump.exe", "mitmdump"):
        p = os.path.join(scripts, name)
        if os.path.exists(p):
            return p, py
    import shutil

    w = shutil.which("mitmdump")
    return (w, py) if w else (None, py)


def port_busy(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.6)
        return s.connect_ex(("127.0.0.1", port)) == 0


def main():
    check_only = "--check" in sys.argv

    mitm, py = find_mitmdump()

    if not mitm:
        print("[x] 没找到装了 mitmproxy 的 Python。")
        print("\n扫到的解释器：")
        print(_pyfind.describe(["mitmproxy"]))
        print("\n解决：用上面任意一个打 OK 的 python 装一次")
        print(f'  "{py or sys.executable}" -m pip install mitmproxy')
        return 1

    addon = os.path.join(ROOT, "tools", "grab_token.py")
    confdir = os.path.join(ROOT, "data", "mitm")
    cert = os.path.join(confdir, "mitmproxy-ca-cert.cer")

    print(f"[ok] python:   {py}")
    print(f"[ok] mitmdump: {mitm}")
    print(f"[{'ok' if os.path.exists(cert) else '!'}] 证书:     {cert}")

    if not os.path.exists(cert):
        print("     证书还没生成，第一次启动会自动创建，之后跑一次：")
        print(f'     certutil -addstore -user Root "{cert}"')

    if check_only:
        print(f"[i] 端口 {PORT} {'被占用' if port_busy(PORT) else '空闲'}")
        return 0

    if port_busy(PORT):
        print(f"[x] 端口 {PORT} 已被占用，可能是上一个抓包窗口没关。")
        print("    关掉那个黑窗口，或在任务管理器里结束 mitmdump.exe。")
        return 1

    cmd = [
        mitm,
        "-s", addon,
        "--listen-host", "0.0.0.0",
        "--listen-port", str(PORT),
        "--set", f"confdir={confdir}",
    ]
    print("\n" + "=" * 52)
    print(" 抓包已启动 · 端口 8080")
    print(" 现在打开 微信 → 朴朴超市小程序 → 点购物车")
    print(" 抓到凭证会自动入库，这个窗口 Ctrl+C 结束")
    print("=" * 52 + "\n")

    try:
        return subprocess.call(cmd, cwd=ROOT)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
