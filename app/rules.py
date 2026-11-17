import time

from . import db, config, notify


def _drop_hit(old, new, min_pct, min_abs):
    if old is None or new is None:
        return False
    drop = old - new
    if drop <= 0:
        return False
    if drop < min_abs:
        return False
    pct = drop / old * 100 if old else 0
    return pct >= min_pct


def _quiet_alert(platform, sku, new_price, cooldown=600):
    """同一 SKU 不要在冷却期内重复轰炸。"""
    last = db.last_alert(platform, sku)
    if not last:
        return False
    if last["new_price"] is not None and abs(last["new_price"] - new_price) < 0.01:
        return True
    if int(time.time()) - (last["created_at"] or 0) < cooldown:
        return True
    return False


def process(items):
    """写入价格历史并判定是否要推送，返回本次产生的告警数。

    星标商品（重点监控）只要降一分钱就推；非星标走常规阈值。
    """
    conf = config.load()["rule"]
    min_pct = float(conf.get("min_drop_pct", 5))
    min_abs = float(conf.get("min_drop_abs", 0.5))
    alert_tag = bool(conf.get("alert_on_clearance_tag", True))
    watched = {w["sku"] for w in db.list_watch()}

    hits = 0
    for it in items:
        old = db.last_price(it.platform, it.sku)
        db.log_price(it.platform, it.sku, it.name, it.price, it.origin_price, it.qty, it.tags, it.image)

        if _quiet_alert(it.platform, it.sku, it.price):
            continue

        star = it.sku in watched
        hit = (old - it.price >= 0.01) if star else _drop_hit(old, it.price, min_pct, min_abs)
        if hit and old is not None:
            aid = db.add_alert(it.platform, it.sku, it.name, old, it.price,
                               "watch_drop" if star else "price_drop")
            notify.price_drop(it, old, aid, watched=star)
            hits += 1
        elif old is None and alert_tag and it.is_clearance:
            aid = db.add_alert(it.platform, it.sku, it.name, None, it.price, "clearance")
            notify.clearance_new(it, aid)
            hits += 1
    return hits
