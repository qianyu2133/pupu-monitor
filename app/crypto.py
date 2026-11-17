import os
import base64

from .paths import root as _root

ROOT = _root()
DATA_DIR = os.path.join(ROOT, "data")
KEY_FILE = os.path.join(DATA_DIR, "secret.key")

try:
    from cryptography.fernet import Fernet
    _HAS_CRYPTO = True
except ImportError:
    _HAS_CRYPTO = False


def _fernet():
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.exists(KEY_FILE):
        with open(KEY_FILE, "wb") as f:
            f.write(Fernet.generate_key())
        try:
            os.chmod(KEY_FILE, 0o600)
        except Exception:
            pass
    with open(KEY_FILE, "rb") as f:
        return Fernet(f.read())


def enc(value):
    """凭证落库前加密。没有 cryptography 时退化为 base64（仅防误读，不是安全措施）。"""
    if not value:
        return value
    if _HAS_CRYPTO:
        return "enc:" + _fernet().encrypt(value.encode("utf-8")).decode("utf-8")
    return "b64:" + base64.b64encode(value.encode("utf-8")).decode("utf-8")


def dec(value):
    if not value:
        return value
    try:
        if value.startswith("enc:"):
            return _fernet().decrypt(value[4:].encode("utf-8")).decode("utf-8")
        if value.startswith("b64:"):
            return base64.b64decode(value[4:]).decode("utf-8")
    except Exception:
        return ""
    return value


def mask(value, keep_head=6, keep_tail=4):
    if not value:
        return ""
    if len(value) <= keep_head + keep_tail:
        return "*" * len(value)
    return value[:keep_head] + "*" * 8 + value[-keep_tail:]
