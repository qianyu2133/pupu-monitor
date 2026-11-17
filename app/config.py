import json
import os
import copy

from .paths import root as _root

ROOT = _root()
CONFIG_PATH = os.path.join(ROOT, "config.json")

DEFAULTS = {
    "pupu": {
        "host": "https://j1.pupuapi.com",
        "cart_path": "/client/shopping_cart/shopping_cart/detail/v3",
        "cart_method": "POST",
        "refresh_path": "",
        "price_scale": 0.01,
    },
    "notify": {"wecom_webhook": "", "public_base": "http://127.0.0.1:8787"},
    "poll": {
        "peak_start": 21,
        "peak_end": 23,
        "peak_interval": 12,
        "idle_interval": 180,
        "jitter": 0.3,
    },
    "rule": {"min_drop_pct": 5.0, "min_drop_abs": 0.5, "alert_on_clearance_tag": True},
    "danger": {"allow_submit_order": False},
}

_cache = None


def _merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load(reload=False):
    global _cache
    if _cache is not None and not reload:
        return _cache
    raw = {}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception as e:
            print(f"[config] 读取失败，使用默认值: {e}")
    else:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(DEFAULTS, f, ensure_ascii=False, indent=2)
    _cache = _merge(DEFAULTS, raw)
    return _cache


def get(section, key, default=None):
    return load().get(section, {}).get(key, default)


def save(cfg=None):
    global _cache
    if cfg is not None:
        _cache = cfg
    data = _cache if _cache is not None else load()
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return data
