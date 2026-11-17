import requests

from . import config

DROUGHT_KEY = ""


def _wecom(text_md):
    hook = config.get("notify", "wecom_webhook", "")
    if not hook:
        print("[notify] 未配置企业微信 webhook，仅打印：\n" + text_md)
        return False
    try:
        r = requests.post(
            hook,
            json={"msgtype": "markdown", "markdown": {"content": text_md}},
            timeout=10,
        )
        ok = r.status_code == 200 and r.json().get("errcode") == 0
        if not ok:
            print(f"[notify] 企微返回异常: {r.text[:200]}")
        return ok
    except Exception as e:
        print(f"[notify] 发送失败: {e}")
        return False


def price_drop(item, old_price, alert_id, watched=False):
    base = config.get("notify", "public_base", "").rstrip("/")
    drop = old_price - item.price
    pct = drop / old_price * 100 if old_price else 0
    tag = " · ".join(item.tags[:3]) if item.tags else "购物车"
    title = ("⭐ 重点商品降价" if watched else "降价") + f" ¥{drop:.2f}（{pct:.0f}%）"
    lines = [
        f"### {title}",
        f"**{item.name}**",
        f"<font color=\"warning\">¥{old_price:.2f} → ¥{item.price:.2f}</font>"
        + (f"　原价 ¥{item.origin_price:.2f}" if item.origin_price else ""),
        f"> {tag}　数量 {int(item.qty) if item.qty else 1}",
    ]
    if base:
        lines.append(f"[去结算]({base}/ack?aid={alert_id}&s=go)　[忽略]({base}/ack?aid={alert_id}&s=skip)")
    return _wecom("\n".join(lines))


def auth_expired(platform, account, detail=""):
    base = config.get("notify", "public_base", "").rstrip("/")
    lines = [
        f"### {platform} 凭证失效，监控已暂停",
        f"账号 `{account}`　{detail}",
        "需要重新抓一次包（手机打开 App → 点购物车即可）",
    ]
    if base:
        lines.append(f"[打开控制台]({base}/)")
    return _wecom("\n".join(lines))


def clearance_new(item, alert_id):
    base = config.get("notify", "public_base", "").rstrip("/")
    lines = [
        "### 发现出清商品",
        f"**{item.name}**　<font color=\"warning\">¥{item.price:.2f}</font>",
        f"> {item.platform}　" + (" · ".join(item.tags[:3]) if item.tags else "出清区"),
    ]
    if base:
        lines.append(f"[已加购]({base}/ack?aid={alert_id}&s=go)　[忽略]({base}/ack?aid={alert_id}&s=skip)")
    return _wecom("\n".join(lines))
