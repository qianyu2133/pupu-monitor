"""Windows 系统代理开关（只改当前用户，随时可关）。

    python tools/proxy.py on      开启，指向 127.0.0.1:8080
    python tools/proxy.py off     关闭，恢复正常直连
    python tools/proxy.py status  查看当前状态
"""
import ctypes
import os
import sys
import winreg

KEY = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
SERVER = "127.0.0.1:8080"
BACKUP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "proxy_backup.txt")


def _read():
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, KEY) as k:
        try:
            enable = winreg.QueryValueEx(k, "ProxyEnable")[0]
        except FileNotFoundError:
            enable = 0
        try:
            server = winreg.QueryValueEx(k, "ProxyServer")[0]
        except FileNotFoundError:
            server = ""
    return bool(enable), server


def _notify():
    # 让 WinINET 立即重读设置，不然要等一会儿 / 重开应用
    ctypes.windll.wininet.InternetSetOptionW(0, 39, 0, 0)
    ctypes.windll.wininet.InternetSetOptionW(0, 37, 0, 0)


def _write(enable, server):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, KEY, 0, winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, "ProxyEnable", 0, winreg.REG_DWORD, 1 if enable else 0)
        if server:
            winreg.SetValueEx(k, "ProxyServer", 0, winreg.REG_SZ, server)
        try:
            winreg.SetValueEx(k, "ProxyOverride", 0, winreg.REG_SZ, "<local>")
        except OSError:
            pass
    _notify()


def main():
    cmd = (sys.argv[1] if len(sys.argv) > 1 else "status").lower()
    if cmd == "on":
        was_on, old_server = _read()
        os.makedirs(os.path.dirname(BACKUP), exist_ok=True)
        with open(BACKUP, "w", encoding="utf-8") as f:
            f.write(("1" if was_on else "0") + "\n" + (old_server or ""))
        _write(True, SERVER)
        print(f"系统代理已开启 → {SERVER}")
        if old_server and old_server != SERVER:
            print(f"（你原来的代理 {old_server} 已记下，关掉时会还原）")
        print("关掉用: python tools/proxy.py off")
    elif cmd == "off":
        enable, server = _read()
        old_enable, old_server = "0", ""
        if os.path.exists(BACKUP):
            with open(BACKUP, "r", encoding="utf-8") as f:
                lines = f.read().splitlines()
            old_enable = lines[0] if lines else "0"
            old_server = lines[1] if len(lines) > 1 else ""
        if old_server and old_server != SERVER:
            _write(old_enable == "1", old_server)
            print(f"已还原为原来的代理设置: {old_server}（{'开' if old_enable == '1' else '关'}）")
        else:
            _write(False, SERVER)
            print("系统代理已关闭，恢复直连")
    else:
        enable, server = _read()
        print(f"ProxyEnable={1 if enable else 0}  ProxyServer={server or '(空)'}")


if __name__ == "__main__":
    main()
