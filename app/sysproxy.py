"""Windows 系统代理开关 + 根证书安装（都不需要管理员权限）。"""
import ctypes
import os
import subprocess
import sys
import winreg

KEY = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
SERVER = "127.0.0.1:8080"

from .paths import root as _root

ROOT = _root()
BACKUP = os.path.join(ROOT, "data", "proxy_backup.txt")


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
    ctypes.windll.wininet.InternetSetOptionW(0, 39, 0, 0)
    ctypes.windll.wininet.InternetSetOptionW(0, 37, 0, 0)


def _write(enable, server=None):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, KEY, 0, winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, "ProxyEnable", 0, winreg.REG_DWORD, 1 if enable else 0)
        if server:
            winreg.SetValueEx(k, "ProxyServer", 0, winreg.REG_SZ, server)
        try:
            winreg.SetValueEx(k, "ProxyOverride", 0, winreg.REG_SZ, "<local>")
        except OSError:
            pass
    _notify()


def is_on():
    enable, server = _read()
    return bool(enable and SERVER in (server or ""))


def turn_on():
    was_on, old = _read()
    os.makedirs(os.path.dirname(BACKUP), exist_ok=True)
    # 只备份"真实的原值"。若旧值已经是自己的地址，说明上次没还原干净，
    # 此时不能覆盖备份，否则会把 8080 永久固化成"用户的原始设置"。
    if SERVER not in (old or ""):
        with open(BACKUP, "w", encoding="utf-8") as f:
            f.write(("1" if was_on else "0") + "\n" + (old or ""))
    _write(True, SERVER)
    return {"was_on": was_on, "old": old}


def turn_off():
    old_enable, old_server = "0", ""
    if os.path.exists(BACKUP):
        with open(BACKUP, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
        old_enable = lines[0] if lines else "0"
        old_server = lines[1] if len(lines) > 1 else ""
    if old_server and SERVER not in old_server:
        _write(old_enable == "1", old_server)
        restored = old_server
    else:
        # 没有可信备份：只关开关，绝不把自己的地址写进注册表
        _write(False)
        restored = "(直连)"
    return {"restored": restored, "enabled": old_enable == "1"}


def heal_on_start():
    """清掉上次异常退出留下的代理残留。

    进程被强杀（任务管理器结束、崩溃、关机）时 `turn_off()` 不会执行，
    注册表里就留着 `127.0.0.1:8080`；此时抓包引擎已经不在，整台机器表现为断网。
    启动时立刻还原——系统代理只在向导抓包时临时开，不需要常驻。
    """
    enable, server = _read()
    if not enable or SERVER not in (server or ""):
        return None
    info = turn_off()
    print(f"[sysproxy] 发现上次残留的系统代理，已还原 → {info['restored']}")
    return info


# ---------- 证书 ----------

def ca_path():
    return os.path.join(ROOT, "data", "mitm", "mitmproxy-ca-cert.cer")


def cert_thumbprint():
    """本机 CA 的 SHA1 指纹（大小写不敏感，去掉空格）。"""
    p = ca_path()
    if not os.path.exists(p):
        return None
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes

        with open(p, "rb") as f:
            raw = f.read()
        try:
            cert = x509.load_der_x509_certificate(raw)
        except Exception:
            cert = x509.load_pem_x509_certificate(raw)
        return cert.fingerprint(hashes.SHA1()).hex()
    except Exception:
        return None


def cert_installed():
    """必须是指纹匹配的那一张，别的 mitmproxy 证书不算。"""
    thumb = cert_thumbprint()
    if not thumb:
        return False
    try:
        r = subprocess.run(
            ["certutil", "-user", "-store", "Root", thumb],
            capture_output=True, timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return r.returncode == 0
    except Exception:
        return False


def remove_cert():
    """按指纹精确删除，不误删别人装的证书。"""
    thumb = cert_thumbprint()
    if not thumb:
        return {"ok": False, "msg": "本地还没有生成证书"}
    r = subprocess.run(
        ["certutil", "-delstore", "-user", "Root", thumb],
        capture_output=True, timeout=20,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return {"ok": r.returncode == 0}


def install_cert():
    p = ca_path()
    if not os.path.exists(p):
        return {"ok": False, "msg": "证书还没生成，先点「启动抓包引擎」"}
    r = subprocess.run(
        ["certutil", "-addstore", "-user", "Root", p],
        capture_output=True, timeout=20,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return {"ok": r.returncode == 0, "msg": "" if r.returncode == 0 else "安装失败，可能已被安全软件拦截"}


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "on":
        print("系统代理已开启 →", SERVER, turn_on())
    elif cmd == "off":
        print("系统代理已关闭", turn_off())
    else:
        print("ProxyEnable=%d ProxyServer=%s" % (1 if _read()[0] else 0, _read()[1] or "(空)"))
