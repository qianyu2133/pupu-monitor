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
        "cart_peak_interval": 8,
        "cart_idle_interval": 60,
        "scan_interval": 1800,
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


def _migrate(cfg, raw_poll=None):
    """把单一一档的 peak_interval/idle_interval 迁移成「购物车轮询 + 出清池任务」两套。

    判断依据必须是**原始文件里有没有旧键**——DEFAULTS 已经带了新键，
    拿 merge 后的结果判断会永远认为「已迁移」，旧键就永远清不掉。
    """
    p = cfg.setdefault("poll", {})
    legacy = [k for k in ("peak_interval", "idle_interval") if k in (raw_poll or {})]
    if not legacy:
        return False
    # 旧节奏（12/180）正是用户嫌慢的那套，直接用新的默认值，不沿用旧数
    p["cart_peak_interval"] = 8
    p["cart_idle_interval"] = 60
    p.setdefault("scan_interval", 1800)
    for k in legacy:
        p.pop(k, None)
    return True


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
    if _migrate(_cache, raw.get("poll") or {}):
        save(_cache)
        print("[config] 已迁移轮询配置：购物车 %ss/%ss，出清池 %ss" % (
            _cache["poll"]["cart_peak_interval"],
            _cache["poll"]["cart_idle_interval"],
            _cache["poll"]["scan_interval"],
        ))
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
