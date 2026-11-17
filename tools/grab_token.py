"""mitmproxy 插件：抓包即入库。

用法（一般不用手敲，双击 start_capture.bat 即可）：
    python tools/capture.py

作用：
  1. 所有经过代理的域名记一条计数（只记域名，不记内容）——判断"流量到底进没进来"
  2. 朴朴接口记到 capture 表，帮我们找到购物车 / 出清频道路径
  3. 自动抠出 Authorization / pp-userid，加密写进凭证表
  4. 从响应体里找 refresh_token
  5. 任何返回里带商品的响应，都收进出清池（你浏览过的页面，商品都会进池子）
  6. 录制「删除购物车商品」「提交订单」两条请求，之后可参数替换重放

账号名用环境变量 CHUJIAN_ACCOUNT 指定，默认「默认」。
"""
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mitmproxy import http

from app import crypto, db
from app.adapters.parser import harvest_products, parse_pupu

ACCOUNT = os.environ.get("CHUJIAN_ACCOUNT", "默认")
PUPU_SUFFIX = ("pupuapi.com", "pupumall.com")
MEITUAN_SUFFIX = ("meituan.com", "meituan.net", "dianping.com")
SKIP_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".css", ".js", ".woff", ".woff2", ".svg", ".ico")
JUNK = ("红包", "优惠券", "满减", "打包费", "配送费", "服务费", "押金")

_last = {"token": None}
_stat = {"req": 0, "saved": 0, "scan": 0}


def running():
    db.init()
    print("\n" + "=" * 56)
    print(" 出清监控 · 抓包插件已就绪")
    print(" 端口 8080   账号标记: " + ACCOUNT)
    print(" 打开 App / 小程序，点购物车、逛清仓页，都会自动收进出清池")
    print(" 控制台 http://127.0.0.1:8787")
    print("=" * 56 + "\n")


def _host(flow):
    return flow.request.pretty_host


def _match(host, suffixes):
    return any(host == s or host.endswith("." + s) for s in suffixes)


def _path(flow):
    return flow.request.path.split("?")[0].split("#")[0]


def _looks_useful(items):
    out = []
    for it in items:
        if not it.name or len(it.name) < 2:
            continue
        if any(j in it.name for j in JUNK):
            continue
        out.append(it)
    return out


def _record_replay(flow, key, label):
    try:
        db.save_replay(
            "pupu", key, flow.request.pretty_url, flow.request.method,
            dict(flow.request.headers), flow.request.get_text(strict=False) or "",
        )
        print(f"[录制{label}] {flow.request.method} {_path(flow)}")
    except Exception as e:
        print(f"[warn] 录制{label}失败: {e}")


def request(flow: http.HTTPFlow):
    host = _host(flow)
    path = _path(flow)
    low = path.lower()
    _stat["req"] += 1

    try:
        db.bump_host(host, path)
    except Exception as e:
        print(f"[warn] 流量统计写入失败: {e}")

    is_pupu = _match(host, PUPU_SUFFIX)
    is_mt = _match(host, MEITUAN_SUFFIX)
    if not (is_pupu or is_mt):
        return
    if low.endswith(SKIP_EXT):
        return

    if is_pupu:
        # 写操作：录下来供之后参数替换重放
        if "cart" in low and "detail" in low and "recommend" not in low and "startup" not in low:
            _record_replay(flow, "cart", "购物车请求")
        if "unifiedorder" in low or (
            "order" in low and any(k in low for k in ("submit", "create", "confirm", "place", "checkout"))
        ):
            _record_replay(flow, "order_submit", "提交订单请求")
        if "cart" in low and any(k in low for k in ("delete", "remove")):
            _record_replay(flow, "cart_write", "购物车删除请求")

        # GET 的频道/秒杀列表：把带参数的完整 URL 记下来（找分页参数用）
        if flow.request.method == "GET" and (
            "flashsale" in low or "channel" in low or "product" in low
        ):
            import re as _re

            slug = _re.sub(r"[^a-z0-9]+", "_", low).strip("_")[:50]
            q = flow.request.query.string
            tag = hashlib.md5(q.encode()).hexdigest()[:6] if q else "noq"
            try:
                db.save_replay(
                    "pupu", f"g:{slug}_{tag}", flow.request.pretty_url, "GET",
                    dict(flow.request.headers), "",
                )
            except Exception:
                pass

        # 兜底：朴朴所有写请求都留一份，之后按路径找
        if flow.request.method != "GET":
            import re as _re

            slug = _re.sub(r"[^a-z0-9]+", "_", low).strip("_")[:70]
            try:
                db.save_replay(
                    "pupu", "w:" + slug, flow.request.pretty_url, flow.request.method,
                    dict(flow.request.headers), flow.request.get_text(strict=False) or "",
                )
            except Exception:
                pass

        auth = flow.request.headers.get("authorization") or flow.request.headers.get("Authorization") or ""
        uid = flow.request.headers.get("pp-userid") or flow.request.headers.get("PP-Userid") or ""
        if auth and len(auth) > 12:
            db.record_capture(host, flow.request.method, path, note="带凭证 " + crypto.mask(auth))
            if auth != _last["token"]:
                db.upsert_credential("pupu", ACCOUNT, access_token=auth, user_id=uid or None, extra=host)
                try:
                    db.save_headers("pupu", dict(flow.request.headers))
                except Exception as e:
                    print(f"[warn] 请求头存档失败: {e}")
                _last["token"] = auth
                _stat["saved"] += 1
                print(f"[抓到凭证] 朴朴 / {ACCOUNT}  token={crypto.mask(auth)}  uid={uid or '-'}")
            return
        db.record_capture(host, flow.request.method, path, note="pupu")
        return

    db.record_capture(host, flow.request.method, path, note="meituan(需签名)")


def response(flow: http.HTTPFlow):
    host = _host(flow)
    path = _path(flow)
    low = path.lower()

    is_pupu = _match(host, PUPU_SUFFIX)
    if not is_pupu and not _match(host, MEITUAN_SUFFIX):
        return

    db.record_capture(
        host, flow.request.method, path,
        status=flow.response.status_code,
        note="pupu" if is_pupu else "meituan(需签名)",
    )

    if not is_pupu:
        return
    if "json" not in flow.response.headers.get("content-type", ""):
        return

    text = flow.response.get_text(strict=False) or ""
    if "refresh_token" in text or "refreshToken" in text:
        try:
            data = json.loads(text)

            def dig(obj):
                if isinstance(obj, dict):
                    for k in ("refresh_token", "refreshToken"):
                        v = obj.get(k)
                        if isinstance(v, str) and len(v) > 10:
                            return v
                    for v in obj.values():
                        got = dig(v)
                        if got:
                            return got
                elif isinstance(obj, list):
                    for v in obj:
                        got = dig(v)
                        if got:
                            return got
                return None

            rt = dig(data)
            if rt:
                db.upsert_credential("pupu", ACCOUNT, refresh_token=rt)
                print(f"[抓到 refresh_token] 朴朴 / {ACCOUNT}  {crypto.mask(rt)}")
        except Exception:
            pass

    # 批量收货：带商品返回的响应都进池子
    if len(text) < 200 or len(text) > 4_000_000:
        return
    try:
        data = json.loads(text)
    except Exception:
        return

    items = []
    source = path
    if "shopping_cart" in low:
        items = parse_pupu(data)
        source = "购物车"
    if not items:
        items = harvest_products(data)
        if "flashsale" in low or "discount" in low:
            source = "疯狂折扣"
    if items:
        n = db.upsert_scan("pupu", items, source)
        _stat["scan"] += n
        print(f"[入池] {n} 件  ← {source[:48]}")


def done():
    print(f"\n[抓包汇总] 请求 {_stat['req']} 条，新凭证 {_stat['saved']} 个，入池 {_stat['scan']} 件")
    print("控制台 http://127.0.0.1:8787 可看明细\n")


class Grabber:
    """把模块级的钩子包成 addon 对象，供进程内嵌时使用。"""

    def running(self):
        running()

    def request(self, flow: http.HTTPFlow):
        request(flow)

    def response(self, flow: http.HTTPFlow):
        response(flow)

    def done(self):
        done()
