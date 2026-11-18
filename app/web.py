import threading
import time
from typing import List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from . import config, db, notify, scheduler, sysproxy
from .proxy_host import proxy
from .adapters.base import AdapterError
from .adapters.pupu import PupuAdapter

app = FastAPI(title="出清监控", docs_url=None, redoc_url=None)


@app.on_event("startup")
def _startup():
    db.init()
    scheduler.monitor.start()


class CredentialIn(BaseModel):
    platform: str = "pupu"
    account: str
    access_token: Optional[str] = None
    refresh_token: Optional[str] = None
    user_id: Optional[str] = None


class MonitorIn(BaseModel):
    enabled: bool


class SkuIn(BaseModel):
    skus: List[str]


class DangerIn(BaseModel):
    allow_submit_order: bool


class ConfigIn(BaseModel):
    cart_path: Optional[str] = None
    cart_method: Optional[str] = None
    host: Optional[str] = None
    wecom_webhook: Optional[str] = None
    public_base: Optional[str] = None


def _refreshing():
    m = scheduler.monitor
    if not m.requested_at:
        return False
    return db.last_refresh_ts() < m.requested_at and (time.time() - m.requested_at) < 90


def _best_cred():
    creds = [c for c in db.list_credentials("pupu") if c.get("status") == "ok"]
    return creds[0] if creds else None


@app.get("/api/state")
def state():
    creds = db.list_credentials()
    for c in creds:
        c["access_token"] = "***" if c.get("access_token") else ""
        c["refresh_token"] = "***" if c.get("refresh_token") else ""
    cfg = config.load()
    st = scheduler.monitor.state
    rep = db.get_replay("pupu", "cart")
    return {
        "now": int(time.time()),
        "monitor": db.monitor_enabled(),
        "interval": round(scheduler.current_interval(), 1),
        "last_refresh": db.last_refresh_ts(),
        "refreshing": _refreshing(),
        "creds": creds,
        "runtime": {str(k): v for k, v in st.items()},
        "captures": db.list_captures(40),
        "hosts": db.top_hosts(20),
        "hits": db.total_hits(),
        "alerts": db.recent_alerts(20),
        "snapshot": db.latest_snapshot(120),
        "scan": db.list_scan(600),
        "scan_stats": db.scan_stats(),
        "last_scan": scheduler.monitor.last_scan,
        "watches": [w["sku"] for w in db.list_watch("pupu")],
        "allow_submit": bool(cfg.get("danger", {}).get("allow_submit_order")),
        "last_order": db.last_submit(),
        "replay": {
            "cart": bool(rep),
            "cart_ts": (rep or {}).get("ts", 0),
            "write": bool(
                db.get_replay("pupu", "w:client_shopping_cart_shopping_cart_item_purchasing_product_v3")
                or db.get_replay("pupu", "cart_write")
            ),
            "order": bool(db.get_replay("pupu", "order_submit")),
        },
        "config": {
            "cart_path": cfg["pupu"].get("cart_path", ""),
            "cart_method": cfg["pupu"].get("cart_method", "GET"),
            "host": cfg["pupu"].get("host", ""),
            "wecom_set": bool(cfg["notify"].get("wecom_webhook")),
        },
    }


@app.get("/api/cart")
def cart_only():
    """只返回购物车快照。加购 / 删除后用这个局部刷新，不要拉整个 state。"""
    return {
        "snapshot": db.latest_snapshot(120),
        "last_refresh": db.last_refresh_ts(),
        "interval": round(scheduler.current_interval(), 1),
        "refreshing": _refreshing(),
    }


@app.post("/api/refresh")
def refresh_now():
    ts = scheduler.monitor.request_refresh()
    return {"ok": True, "at": ts}


@app.get("/api/scan")
def scan(max_discount: float = 1.0, limit: int = 600):
    return {"items": db.list_scan(limit, max_discount), "stats": db.scan_stats()}


class SkuIn2(BaseModel):
    sku: str


@app.get("/api/addresses")
def addresses():
    cred = _best_cred()
    if not cred:
        return {"items": [], "active": None}
    try:
        items = PupuAdapter(cred).list_addresses()
    except AdapterError as e:
        raise HTTPException(400, str(e))
    slim = [{k: v for k, v in a.items() if k != "raw"} for a in items]
    act = db.get_active_address("pupu")
    return {"items": slim, "active": (act or {}).get("id")}


class AddrIn(BaseModel):
    id: str


@app.post("/api/address/select")
def address_select(body: AddrIn):
    cred = _best_cred()
    if not cred:
        raise HTTPException(400, "没有可用凭证")
    try:
        a = PupuAdapter(cred).set_address(body.id)
    except AdapterError as e:
        raise HTTPException(400, str(e))
    # 换店后购物车和出清池都是旧店的，必须刷新
    try:
        PupuAdapter(cred).fetch_cart()
    except Exception:
        pass
    db.clear_scan("pupu")
    threading.Thread(target=_refill_scan_bg, daemon=True).start()
    return {"ok": True, "name": a.get("title"), "in_service": a.get("in_service"),
            "rescanning": True}


def _refill_scan_bg():
    try:
        cred = _best_cred()
        if cred:
            rows = PupuAdapter(cred).fetch_crazy_discount()
            db.replace_scan("pupu", rows, "疯狂折扣")
    except Exception as e:
        print(f"[rescan] 换店后重抓失败: {e}")


@app.get("/api/setup")
def setup_state():
    creds = [c for c in db.list_credentials("pupu")]
    rep = db.get_replay("pupu", "cart")
    return {
        "proxy_running": bool(proxy.running),
        "proxy_on": sysproxy.is_on(),
        "cert": sysproxy.cert_installed(),
        "hits": db.total_hits(),
        "has_token": bool(creds),
        "cart_ts": (rep or {}).get("ts", 0),
        "cred_status": [c.get("status") for c in creds],
        "ca": sysproxy.ca_path(),
        "proxy_error": proxy.error,
    }


class CertIn(BaseModel):
    action: str = "install"


@app.post("/api/setup/cert")
def setup_cert(body: CertIn):
    if body.action == "remove":
        return {"ok": sysproxy.remove_cert(), "installed": sysproxy.cert_installed()}
    res = sysproxy.install_cert()
    return {"ok": res["ok"], "msg": res.get("msg", ""), "installed": sysproxy.cert_installed()}


@app.post("/api/setup/engine")
def setup_engine():
    ok = proxy.start()
    return {"ok": ok, "running": proxy.running, "error": proxy.error}


class ProxyIn(BaseModel):
    on: bool


@app.post("/api/setup/proxy")
def setup_proxy(body: ProxyIn):
    if body.on:
        if not proxy.running:
            proxy.start()
        sysproxy.turn_on()
    else:
        sysproxy.turn_off()
    return {"ok": True, "proxy_on": sysproxy.is_on()}


@app.post("/api/setup/finish")
def setup_finish():
    info = sysproxy.turn_off()
    return {"ok": True, **info}


@app.post("/api/scan/refresh")
def scan_refresh():
    cred = _best_cred()
    if not cred:
        raise HTTPException(400, "没有可用凭证")
    try:
        rows = PupuAdapter(cred).fetch_crazy_discount()
    except AdapterError as e:
        raise HTTPException(400, str(e))
    written, removed = db.replace_scan("pupu", rows, "疯狂折扣")
    return {"ok": True, "fetched": len(rows), "written": written, "stale_removed": removed}


@app.post("/api/scan/add")
def scan_add(body: SkuIn2):
    row = db.get_scan("pupu", body.sku)
    if not row:
        raise HTTPException(404, "出清池里没有这条")
    cred = _best_cred()
    if not cred:
        raise HTTPException(400, "没有可用凭证")
    try:
        PupuAdapter(cred).add_to_cart(row)
    except AdapterError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "name": row["name"]}


class WatchIn(BaseModel):
    sku: str
    on: bool


@app.post("/api/watch")
def set_watch(body: WatchIn):
    if body.on:
        db.add_watch("pupu", body.sku)
    else:
        db.del_watch("pupu", body.sku)
    return {"ok": True, "on": body.on}


@app.post("/api/scan/clear")
def scan_clear():
    db.clear_scan()
    return {"ok": True}


def _map_skus(skus):
    cred = _best_cred()
    if not cred:
        raise HTTPException(400, "没有可用凭证")
    adapter = PupuAdapter(cred)
    items = adapter.fetch_cart()
    m = {it.sku: ((it.raw or {}).get("id") or it.sku) for it in items}
    ids = [m.get(s) for s in skus]
    missing = [s for s, i in zip(skus, ids) if not i]
    if missing:
        raise HTTPException(400, f"购物车里找不到这些商品（可能已失效）：{missing[:3]}")
    return adapter, [i for i in ids if i]


@app.post("/api/cart/remove")
def cart_remove(body: SkuIn):
    adapter, ids = _map_skus(body.skus)
    try:
        res = adapter.remove_items(ids)
    except AdapterError as e:
        raise HTTPException(400, str(e))
    if not res.get("removed"):
        raise HTTPException(400, "一件都没删掉，可能是商品已不在购物车")
    return {"ok": True, "removed": res["removed"]}


@app.post("/api/order/submit")
def order_submit(body: SkuIn):
    if not config.get("danger", "allow_submit_order"):
        raise HTTPException(
            403,
            "锁单功能默认关闭：它会真的提交订单锁库存。确认风险后把 config.json 的 "
            "danger.allow_submit_order 改成 true 并重启控制台。",
        )
    adapter, ids = _map_skus(body.skus)
    try:
        data = adapter.submit_order(ids)
    except AdapterError as e:
        raise HTTPException(400, str(e))
    oid = (data.get("data") or {}).get("id")
    if oid:
        db.log_submit(oid, len(ids))
    return {"ok": True, "submitted": len(ids), "order_id": oid}


@app.post("/api/danger")
def set_danger(body: DangerIn):
    cfg = config.load()
    cfg.setdefault("danger", {})["allow_submit_order"] = bool(body.allow_submit_order)
    config.save(cfg)
    return {"ok": True, "allow_submit_order": body.allow_submit_order}


class OrderIn(BaseModel):
    order_id: str


@app.post("/api/order/cancel")
def order_cancel(body: OrderIn):
    adapter = PupuAdapter(_best_cred())
    if not adapter:
        raise HTTPException(400, "没有可用凭证")
    try:
        adapter.cancel_order(body.order_id)
    except AdapterError as e:
        raise HTTPException(400, str(e))
    db.mark_submit(body.order_id, "cancelled")
    return {"ok": True}


@app.post("/api/credential")
def add_credential(body: CredentialIn):
    if not body.access_token and body.platform != "mock":
        raise HTTPException(400, "access_token 不能为空")
    cid = db.upsert_credential(
        platform=body.platform,
        account=body.account,
        access_token=body.access_token,
        refresh_token=body.refresh_token,
        user_id=body.user_id,
    )
    return {"ok": True, "id": cid}


@app.delete("/api/credential/{cred_id}")
def rm_credential(cred_id: int):
    db.delete_credential(cred_id)
    return {"ok": True}


@app.post("/api/credential/{cred_id}/ok")
def mark_ok(cred_id: int):
    db.set_status(cred_id, "ok")
    return {"ok": True}


@app.post("/api/monitor")
def set_monitor(body: MonitorIn):
    db.set_monitor(body.enabled)
    return {"ok": True, "enabled": body.enabled}


@app.post("/api/config")
def set_config(body: ConfigIn):
    cfg = config.load()
    if body.cart_path is not None:
        cfg["pupu"]["cart_path"] = body.cart_path.strip()
    if body.cart_method is not None:
        cfg["pupu"]["cart_method"] = body.cart_method.strip().upper()
    if body.host is not None and body.host.strip():
        cfg["pupu"]["host"] = body.host.strip()
    if body.wecom_webhook is not None and body.wecom_webhook.strip():
        cfg["notify"]["wecom_webhook"] = body.wecom_webhook.strip()
    if body.public_base is not None and body.public_base.strip():
        cfg["notify"]["public_base"] = body.public_base.strip()
    config.save(cfg)
    return {"ok": True}


@app.post("/api/test_notify")
def test_notify():
    ok = notify._wecom("### 测试消息\n出清监控已连通，降价提醒会发到这里。")
    return {"ok": ok}


@app.get("/ack")
def ack(aid: int, s: str = "go"):
    status = "acked" if s == "go" else "skipped"
    db.ack_alert(aid, status)
    label = "已记录：去结算" if s == "go" else "已记录：忽略"
    return HTMLResponse(
        "<html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{label}</title></head>"
        "<body style='font-family:-apple-system,sans-serif;padding:48px 24px;text-align:center;color:#222'>"
        f"<div style='font-size:20px'>{label}</div>"
        "<div style='color:#888;margin-top:12px;font-size:14px'>可以关掉这个页面了</div>"
        "</body></html>"
    )


@app.get("/history")
def history(platform: str, sku: str):
    return JSONResponse(db.price_series(platform, sku))


@app.get("/", response_class=HTMLResponse)
def index():
    return HTML


HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>出清监控</title>
<style>
:root{--bg:#fff;--panel:#fbfaf8;--line:#eae7e1;--txt:#1f1e1c;--mut:#8d8880;--dim:#b3aea6;--red:#c0392b;--green:#2f7d5c;--amber:#a9701b}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--txt);font:14px/1.55 -apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;-webkit-font-smoothing:antialiased}
.wrap{max-width:1120px;margin:0 auto;padding:22px 20px 90px}
.top{display:flex;align-items:center;gap:14px;padding-bottom:16px;border-bottom:1px solid var(--line);margin-bottom:18px;flex-wrap:wrap}
.brand{font-size:16px;font-weight:500}
.stats{display:flex;gap:16px;color:var(--mut);font-size:12.5px;align-items:center;flex-wrap:wrap}
.acts{margin-left:auto;display:flex;gap:8px}
button{font:inherit;font-size:13px;padding:6px 13px;border-radius:8px;border:1px solid var(--line);background:#fff;color:var(--txt);cursor:pointer;transition:.15s}
button:hover{border-color:#cfc9bf}
button.primary{background:#1f1e1c;border-color:#1f1e1c;color:#fff}
button.primary:hover{background:#333}
button.danger{color:#a5342a;border-color:#e8cdc8}
button:disabled{opacity:.45;cursor:default}
.dot{width:6px;height:6px;border-radius:50%;background:var(--dim);display:inline-block;vertical-align:middle;margin-right:5px}
.dot.on{background:var(--green)}
h2{font-size:12px;font-weight:500;color:var(--mut);letter-spacing:.04em;margin:24px 0 10px;display:flex;align-items:center;gap:8px}
h2 .n{color:var(--dim);font-weight:400}
.chips{margin-left:auto;display:flex;gap:6px}
.chip{font-size:11.5px;padding:2px 9px;border-radius:20px;border:1px solid var(--line);color:var(--mut);cursor:pointer;background:#fff}
.chip.on{background:#1f1e1c;border-color:#1f1e1c;color:#fff}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(276px,1fr));gap:10px}
.card{display:flex;gap:11px;padding:10px;border:1px solid var(--line);border-radius:10px;background:var(--panel);position:relative}
.card.sel{border-color:#1f1e1c;background:#fff}
.chk{position:absolute;top:8px;right:8px;width:17px;height:17px;border-radius:50%;border:1px solid #d6d1c8;background:#fff;cursor:pointer;display:flex;align-items:center;justify-content:center}
.card.sel .chk{background:#1f1e1c;border-color:#1f1e1c}
.card.sel .chk::after{content:"";width:7px;height:4px;border-left:1.5px solid #fff;border-bottom:1.5px solid #fff;transform:rotate(-45deg);margin-top:-2px}
.thumb{width:58px;height:58px;border-radius:7px;object-fit:cover;background:#f0ede8;flex:none}
.thumb.ph{display:flex;align-items:center;justify-content:center;color:var(--dim);font-size:11px}
.meta{min-width:0;flex:1;padding-right:14px}
.nm{font-size:13px;line-height:1.35;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;margin-bottom:4px}
.pr{display:flex;align-items:baseline;gap:6px;flex-wrap:wrap}
.now{color:var(--red);font-weight:500;font-size:15px}
.was{color:var(--dim);font-size:12px;text-decoration:line-through}
.off{font-size:11px;color:var(--amber);border:1px solid #f0e2cb;background:#fdf6ea;border-radius:4px;padding:0 5px;line-height:16px}
.sub{color:var(--mut);font-size:11.5px;margin-top:3px;display:flex;align-items:center;gap:6px}
.del{margin-left:auto;font-size:11px;color:var(--dim);cursor:pointer;border:none;background:none;padding:0 2px}
.del:hover{color:#a5342a}
.row{display:flex;align-items:center;gap:9px;padding:9px 0;border-bottom:1px solid var(--line);font-size:13px}
.row:last-child{border-bottom:none}
.tag{font-size:11px;padding:1px 6px;border-radius:4px;background:#f1eee9;color:#6b655c}
.tag.go{background:#e4f0e9;color:var(--green)}
.tag.hot{background:#fbeceb;color:#a5342a}
.mono{font-family:ui-monospace,Consolas,monospace;font-size:12px;color:#555;word-break:break-all}
.empty{color:var(--dim);font-size:13px;padding:14px 0}
details{border:1px solid var(--line);border-radius:10px;margin-top:10px;background:#fff}
summary{padding:11px 14px;cursor:pointer;font-size:13px;color:var(--mut);outline:none}
details[open] summary{border-bottom:1px solid var(--line);color:var(--txt)}
.dbody{padding:12px 14px}
input,select{font:inherit;font-size:13px;padding:6px 9px;border:1px solid var(--line);border-radius:7px;background:#fff;color:var(--txt);outline:none}
input:focus,select:focus{border-color:#cfc9bf}
.muted{color:var(--mut);font-size:12px}
.grow{flex:1;min-width:150px}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line)}
th{color:var(--mut);font-weight:400;font-size:11.5px}
tr:last-child td{border-bottom:none}
.mini{width:38px;height:38px;border-radius:6px;object-fit:cover;background:#f0ede8}
.bar{position:sticky;bottom:0;margin-top:16px;display:flex;align-items:center;gap:12px;padding:12px 16px;border:1px solid var(--line);border-radius:12px;background:#fff;box-shadow:0 -2px 12px rgba(0,0,0,.05)}
.bar.hide{display:none}
.mask{position:fixed;inset:0;background:rgba(0,0,0,.28);display:flex;align-items:center;justify-content:center;z-index:99}
.mask.hide{display:none}
.dlg{background:#fff;border-radius:12px;padding:18px;width:min(420px,88vw);box-shadow:0 10px 34px rgba(0,0,0,.16)}
.db{font-size:13px;color:#444;line-height:1.65;white-space:pre-wrap;margin-bottom:16px}
.df{display:flex;justify-content:flex-end;gap:8px}
.tabs{display:flex;gap:6px;margin:0 0 14px;border-bottom:1px solid var(--line);padding-bottom:0}
.tab{border:none;background:none;padding:8px 2px;margin-right:14px;border-bottom:2px solid transparent;border-radius:0;color:var(--mut);font-size:14px;cursor:pointer}
.tab.on{color:var(--txt);border-bottom-color:var(--txt);font-weight:500}
.tab b{font-weight:400;color:var(--dim);font-size:12px;margin-left:2px}
.star{cursor:pointer;font-size:13px;line-height:1;color:#d6d1c8;margin-right:4px}
.star.on{color:#e0a324}
.mask{position:fixed;inset:0;background:rgba(20,18,16,.42);display:flex;align-items:center;justify-content:center;z-index:120;padding:20px}
.mask.hide{display:none}
.wiz{background:#fff;border-radius:16px;width:min(520px,100%);padding:26px 26px 20px;box-shadow:0 18px 50px rgba(0,0,0,.2);max-height:88vh;overflow:auto}
.wiz h3{font-size:17px;font-weight:500;margin:0 0 4px}
.wiz .lead{color:var(--mut);font-size:13px;margin-bottom:18px;line-height:1.6}
.step{display:flex;gap:12px;padding:13px 14px;border:1px solid var(--line);border-radius:10px;margin-bottom:9px;background:var(--panel)}
.step.done{background:#f2f8f4;border-color:#d5e7dc}
.step.cur{border-color:#cfc9bf;background:#fff}
.num{width:22px;height:22px;border-radius:50%;background:#ebe7e0;color:var(--mut);font-size:12px;display:flex;align-items:center;justify-content:center;flex:none;margin-top:1px}
.step.done .num{background:var(--green);color:#fff}
.step.cur .num{background:#1f1e1c;color:#fff}
.stitle{font-size:13.5px;margin-bottom:2px}
.sdesc{font-size:12px;color:var(--mut);line-height:1.55}
.wiz .acts{display:flex;gap:8px;align-items:center;margin-top:16px}
.wiz .acts .right{margin-left:auto}
.live{font-family:ui-monospace,Consolas,monospace;font-size:12px;color:var(--green)}
@keyframes spin{to{transform:rotate(360deg)}}
.spin{display:inline-block;width:9px;height:9px;border:1.5px solid #bbb;border-top-color:transparent;border-radius:50%;animation:spin .7s linear infinite;margin-right:5px;vertical-align:-1px}
</style></head>
<body><div class="wrap">

<div class="top">
  <div class="brand"><span id="dot" class="dot"></span>出清监控</div>
  <div class="stats">
    <span id="stMon">监控中</span>
    <span>轮询 <b id="stIv">-</b>s</span>
    <span>上次刷新 <b id="stLast">-</b></span>
  </div>
  <div class="acts">
    <button onclick="wizOpen()">接入账号</button>
    <button class="primary" id="btnRe" onclick="doRefresh()">立即刷新</button>
    <button id="btnTg" onclick="toggle()">暂停</button>
  </div>
</div>

<div class="tabs">
  <button class="tab on" data-t="cart" onclick="setView('cart')">购物车 <b id="tabCart">0</b></button>
  <button class="tab" data-t="scan" onclick="setView('scan')">出清池 <b id="tabScan">0</b></button>
</div>

<div id="viewScan" style="display:none">
<h2>出清池 <span class="n" id="cntScan"></span>
  <div class="chips">
    <span class="chip on" data-d="1" onclick="setDisc(this,1)">全部</span>
    <span class="chip" data-d="0.7" onclick="setDisc(this,0.7)">≤7折</span>
    <span class="chip" data-d="0.5" onclick="setDisc(this,0.5)">≤5折</span>
    <span class="chip" onclick="refreshScan(this)">抓取疯狂折扣</span>
    <span class="chip" onclick="clearScan()">清空</span>
  </div>
</h2>
<div style="display:flex;gap:6px;margin:-2px 0 10px;flex-wrap:wrap">
  <span class="chip on" data-c="全部" onclick="setCat(this,'全部')">全部</span>
  <span class="chip" data-c="肉类" onclick="setCat(this,'肉类')">肉类</span>
  <span class="chip" data-c="海鲜" onclick="setCat(this,'海鲜')">海鲜</span>
  <span class="chip" data-c="其它" onclick="setCat(this,'其它')">其它</span>
</div>
<div id="scan"><div class="empty">还没有收到商品。点「抓取疯狂折扣」拉全量。</div></div>
</div>

<div id="viewCart">

<h2>购物车 <span class="n" id="cntItem"></span></h2>
<div class="row" style="border:none;padding:0 0 10px">
  <span class="muted">收货地址</span>
  <select id="addrSel" onchange="changeAddr(this)" style="max-width:420px"></select>
  <button id="addrBtn" onclick="refreshAddr(this)" style="padding:2px 9px;font-size:12px">刷新</button>
  <span class="muted" id="addrHint"></span>
</div>
<div id="lastOrder" class="muted" style="display:none;margin:-2px 0 10px"></div>
<div class="grid" id="items"><div class="empty">还没有数据</div></div>

<div class="bar hide" id="bar">
  <span>已选 <b id="selN">0</b> 件 · 合计 <b class="now" id="selSum">¥0.00</b></span>
  <span class="muted" id="barHint"></span>
  <div style="margin-left:auto;display:flex;gap:8px">
    <button class="danger" onclick="doRemove()">删除选中</button>
    <button class="primary" id="btnLock" onclick="doLock()">一键锁单</button>
  </div>
</div>

</div>

<h2>降价提醒 <span class="n" id="cntAlert"></span></h2>
<div id="alerts"><div class="empty">暂无</div></div>

<h2>推送</h2>
<div class="row" style="gap:8px">
  <input class="grow" id="hook" placeholder="企业微信群机器人 webhook">
  <button onclick="saveHook()">保存</button>
  <button onclick="testNotify()">测试</button>
</div>

<details>
  <summary>账号与接口</summary>
  <div class="dbody">
    <div class="muted" style="margin-bottom:6px" id="cfgstate"></div>
    <table><thead><tr><th>平台</th><th>账号</th><th>状态</th><th>更新时间</th><th></th></tr></thead>
    <tbody id="creds"></tbody></table>
    <div class="row" style="margin-top:10px">
      <select id="c_platform"><option value="pupu">朴朴</option><option value="mock">演示数据</option></select>
      <input id="c_account" placeholder="备注" style="width:110px">
      <input id="c_token" class="grow" placeholder="access_token">
      <input id="c_uid" placeholder="pp-userid" style="width:150px">
      <button class="primary" onclick="addCred()">添加</button>
    </div>
    <div class="row" style="margin-top:10px;padding-top:10px;border-top:1px solid var(--line)">
      <label style="display:flex;align-items:center;gap:7px;font-size:13px;cursor:pointer">
        <input type="checkbox" id="allowSub" onchange="toggleSubmit(this)" style="width:15px;height:15px">
        允许「一键锁单」（会真的提交订单占库存）
      </label>
    </div>
    <div class="row" style="margin-top:4px">
      <select id="c_method"><option>GET</option><option>POST</option></select>
      <input id="c_path" class="grow mono" placeholder="购物车接口路径">
      <button onclick="savePath()">保存</button>
    </div>
  </div>
</details>

<details>
  <summary>抓包诊断</summary>
  <div class="dbody">
    <div class="muted" style="margin-bottom:8px">经过代理的域名（只记域名不记内容）。空的就是流量没进代理。</div>
    <div id="hosts"></div>
    <div class="muted" style="margin:12px 0 6px">朴朴接口记录</div>
    <div id="caps"></div>
  </div>
</details>

</div>

<div class="mask hide" id="wizard">
  <div class="wiz">
    <h3>接入朴朴账号</h3>
    <div class="lead">全程在电脑上完成，不用手机。需要 Windows 版微信已登录。</div>

    <div class="step" id="s1">
      <div class="num">1</div>
      <div style="flex:1">
        <div class="stitle">信任抓包证书</div>
        <div class="sdesc" id="s1desc">让本机可以解密小程序的 HTTPS 流量。只影响这台电脑，随时可卸载。</div>
      </div>
      <button id="s1btn" onclick="wizCert()">安装</button>
    </div>

    <div class="step" id="s2">
      <div class="num">2</div>
      <div style="flex:1">
        <div class="stitle">把系统代理指向本程序</div>
        <div class="sdesc" id="s2desc">开启后这台电脑的上网会经过本程序。结束后自动还原。</div>
      </div>
      <button id="s2btn" onclick="wizProxy()">开启</button>
    </div>

    <div class="step" id="s3">
      <div class="num">3</div>
      <div style="flex:1">
        <div class="stitle">打开微信，进朴朴小程序点一下购物车</div>
        <div class="sdesc">看到下面数字在涨就说明通了，通常 5 秒内完成。</div>
        <div class="sdesc" style="margin-top:4px"><span class="live" id="wizLive">等待流量…</span></div>
      </div>
    </div>

    <div class="acts">
      <span class="muted" id="wizHint"></span>
      <span class="right"></span>
      <button onclick="wizClose()">稍后</button>
      <button class="primary" id="wizDone" onclick="wizFinish()" disabled>完成</button>
    </div>
  </div>
</div>

<div class="mask hide" id="mask">
  <div class="dlg">
    <div class="db" id="dlgB"></div>
    <div class="df"><button id="dlgNo" onclick="dlgNo()">取消</button><button class="primary" id="dlgYes" onclick="dlgYes()">确定</button></div>
  </div>
</div>

<script>
let S={},sel=new Set(),maxDisc=1.0,curCat='全部';
function el(id){return document.getElementById(id)}
let _dlg=null;
function uiConfirm(text, okText, cancelText){
  return new Promise(res=>{
    _dlg=res;
    el('dlgB').textContent=text;
    el('dlgYes').textContent=okText||'确定';
    const no=el('dlgNo');
    if(cancelText===null||cancelText===''){ no.style.display='none'; }
    else { no.style.display=''; no.textContent=cancelText||'取消'; }
    el('mask').classList.remove('hide');
  });
}
function uiAlert(text){ return uiConfirm(text,'知道了',''); }
function dlgYes(){ el('mask').classList.add('hide'); if(_dlg){const r=_dlg;_dlg=null;r(true)} }
function dlgNo(){ el('mask').classList.add('hide'); if(_dlg){const r=_dlg;_dlg=null;r(false)} }
function esc(s){return (s==null?'':String(s)).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
function hhmmss(t){if(!t)return '—';return new Date(t*1000).toLocaleTimeString('zh-CN',{hour12:false})}
function shortTs(t){
  if(!t) return '—';
  const d=new Date(t*1000), n=new Date();
  const hm=d.toLocaleTimeString('zh-CN',{hour12:false});
  return d.toDateString()===n.toDateString()?hm:((d.getMonth()+1)+'/'+d.getDate()+' '+hm);
}
function ago(t){if(!t)return '';const s=Math.max(0,Math.round(Date.now()/1000-t));if(s<60)return s+' 秒前';if(s<3600)return Math.round(s/60)+' 分钟前';return Math.round(s/3600)+' 小时前'}
function yuan(v){return '¥'+Number(v||0).toFixed(2)}
async function load(){const r=await fetch('/api/state');S=await r.json();render()}
async function post(u,b){const r=await fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b||{})});const j=await r.json().catch(()=>({}));if(!r.ok)throw new Error(j.detail||('HTTP '+r.status));return j}

function imgTag(src,cls){return src?('<img class="'+cls+'" src="'+esc(src)+'" loading="lazy" onerror="this.remove()">'):'<div class="'+cls+' ph">无图</div>'}

let _cartSig='';
function renderCart(items,force){
  items=items||S.snapshot||[];
  el('cntItem').textContent=items.length?items.length+' 件':'';
  el('tabCart').textContent=items.length;
  // 内容没变就不重建 DOM（否则每 4 秒整列表重建一次，图片会闪、滚动位置会丢）
  const sig=items.map(i=>i.sku+'|'+i.price+'|'+i.qty+'|'+i.origin_price+'|'+(i.tags||'')).join('~')
    +'#'+(S.watches||[]).join(',');
  if(force||sig!==_cartSig){
    _cartSig=sig;
    el('items').innerHTML=items.length?items.map(i=>{
      const on=sel.has(i.sku), star=(S.watches||[]).includes(i.sku);
      const disc=(i.origin_price&&i.price<i.origin_price)?(i.price/i.origin_price*10).toFixed(1)+'折':'';
      const tg=(i.tags||'').split(',').filter(Boolean).slice(0,2).map(t=>'<span class="tag">'+esc(t)+'</span>').join(' ');
      return '<div class="card'+(on?' sel':'')+'">'+imgTag(i.image,'thumb')+
        '<div class="chk" data-sel="'+esc(i.sku)+'"></div>'+
        '<div class="meta"><div class="nm">'+esc(i.name)+'</div>'+
        '<div class="pr"><span class="now">'+yuan(i.price)+'</span>'+
        (i.origin_price&&i.origin_price>i.price?'<span class="was">'+yuan(i.origin_price)+'</span>':'')+
        (disc?'<span class="off">'+disc+'</span>':'')+'</div>'+
        '<div class="sub">'+
          '<span class="star'+(star?' on':'')+'" data-star="'+esc(i.sku)+'" title="重点监控">'+(star?'\u2605':'\u2606')+'</span>'+
          (tg||'')+(i.qty>1?'<span>x'+i.qty+'</span>':'')+
          '<button class="del" data-rm="'+esc(i.sku)+'">移除</button>'+
        '</div></div></div>';
    }).join(''):'<div class="empty">购物车是空的</div>';
    el('items').querySelectorAll('[data-sel]').forEach(function(b){ b.onclick=function(){ toggleSel(this.dataset.sel) } });
    el('items').querySelectorAll('[data-star]').forEach(function(b){ b.onclick=function(){ doWatch(this.dataset.star) } });
    el('items').querySelectorAll('[data-rm]').forEach(function(b){ b.onclick=function(){ doRemoveOne(this.dataset.rm) } });
  }
  // 选中栏
  const chosen=items.filter(i=>sel.has(i.sku));
  const sum=chosen.reduce((a,b)=>a+Number(b.price),0);
  el('bar').className='bar'+(chosen.length?'':' hide');
  el('selN').textContent=chosen.length;
  el('selSum').textContent=yuan(sum);
  el('btnLock').disabled=!S.allow_submit;
  el('barHint').textContent=S.allow_submit?'':'锁单已关闭（config.json → danger.allow_submit_order）';
}

async function refreshCartOnly(){
  try{
    const r=await fetch('/api/cart'); const d=await r.json();
    S.snapshot=d.snapshot; S.last_refresh=d.last_refresh;
    el('stLast').textContent=hhmmss(S.last_refresh)+(S.last_refresh?'（'+ago(S.last_refresh)+'）':'');
    renderCart(d.snapshot);
  }catch(e){}
}

function render(){
  const m=S.monitor;
  el('dot').className='dot'+(m?' on':'');
  el('stMon').textContent=m?'监控中':'已暂停';
  el('stIv').textContent=S.interval;
  el('stLast').textContent=hhmmss(S.last_refresh)+(S.last_refresh?'（'+ago(S.last_refresh)+'）':'');
  el('btnTg').textContent=m?'暂停':'继续';
  el('btnRe').disabled=S.refreshing;
  el('btnRe').innerHTML=S.refreshing?'<span class="spin"></span>刷新中':'立即刷新';

  const sc=(S.scan||[]).filter(x=>x.discount<=maxDisc && !x.in_cart && (curCat==='全部'||x.category===curCat));
  el('cntScan').textContent='可加购 '+sc.length+' 件';
  el('scan').innerHTML=sc.length?('<table><thead><tr><th></th><th>商品</th><th>现价</th><th>折扣</th><th>来源</th><th>最近</th><th></th></tr></thead><tbody>'+
    sc.map(x=>{
      const off=(x.discount*10).toFixed(1)+'折';
      const hot=x.discount<=0.5?' tag hot':' tag';
      return '<tr>'+'<td style="width:44px">'+imgTag(x.image,'mini')+'</td>'+
        '<td style="max-width:420px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'+
        (x.category&&x.category!=='其它'?'<span class="tag" style="margin-right:5px">'+esc(x.category)+'</span>':'')+
        esc(x.name)+'</td>'+
        '<td><span class="now" style="font-size:13px">'+yuan(x.price)+'</span>'+
        (x.origin_price&&x.origin_price>x.price?' <span class="was">'+yuan(x.origin_price)+'</span>':'')+'</td>'+
        '<td><span class="'+hot+'">'+off+'</span></td>'+
        '<td class="muted" style="max-width:150px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'+esc(x.source||'')+'</td>'+
        '<td class="muted">'+ago(x.last_seen)+'</td>'+
        '<td style="text-align:right">'+(x.product_id
          ? '<button class="addBtn" data-sku="'+esc(x.sku)+'" style="padding:2px 9px;font-size:12px">加购</button>'
          : '<span class="muted" title="这条是从旧购物车收的，没有商品 id，点「抓取疯狂折扣」重新收">—</span>')
        +'</td></tr>';
    }).join('')+'</tbody></table>'):'<div class="empty">没有符合条件的商品</div>';
  el('scan').querySelectorAll('.addBtn').forEach(function(b){
    b.onclick=function(){ doAdd(this.dataset.sku, this) };
  });

  el('tabScan').textContent=sc.length;
  renderCart();

  const as=S.alerts||[];
  el('cntAlert').textContent=as.length?as.length+' 条':'';
  el('alerts').innerHTML=as.length?as.map(a=>{
    const badge=a.status==='pending'?'<span class="tag">待确认</span>':(a.status==='acked'?'<span class="tag go">去结算</span>':'<span class="tag">忽略</span>');
    const d=a.old_price!=null?(yuan(a.old_price)+' → '+yuan(a.new_price)):yuan(a.new_price);
    return '<div class="row">'+badge+'<span class="grow">'+esc(a.name)+'</span><span class="now" style="font-size:13px">'+d+'</span>'+
      '<span class="muted">'+hhmmss(a.created_at)+'</span></div>';
  }).join(''):'<div class="empty">暂无</div>';

  const rt=S.runtime||{}, hs=S.hosts||[];
  const rp=S.replay||{};
  const cartAge=rp.cart_ts?Math.round((Date.now()/1000-rp.cart_ts)/3600):null;
  el('cfgstate').textContent=(rp.cart?'购物车已录制 '+shortTs(rp.cart_ts):'购物车未录制')+
    (cartAge!=null&&cartAge>=12?'（'+cartAge+' 小时前，签名可能已过期 → 点「接入账号」重录）':'')+
    ' · 删除'+(rp.write?'已就绪':'未就绪')+' · 下单'+(rp.order?'已就绪':'未就绪');
  el('allowSub').checked=!!S.allow_submit;
  el('c_path').value=S.config.cart_path||'';
  el('c_method').value=S.config.cart_method||'GET';
  el('hook').placeholder=S.config.wecom_set?'已配置 webhook（重填可覆盖）':'企业微信群机器人 webhook';

  el('creds').innerHTML=(S.creds||[]).map(c=>{
    const st=rt[String(c.id)]||{}, ok=c.status==='ok';
    return '<tr><td>'+esc(c.platform)+'</td><td>'+esc(c.account)+'</td>'+
      '<td>'+(ok?'<span style="color:#2f7d5c">正常</span>':'<span style="color:#a9701b">需重抓</span>')+
      (st.last_error?'<div class="muted">'+esc(st.last_error)+'</div>':'')+'</td>'+
      '<td class="muted">'+hhmmss(c.updated_at)+'</td>'+
      '<td style="text-align:right"><button style="padding:2px 8px;font-size:12px" onclick="credOk('+c.id+')">标记有效</button> '+
      '<button style="padding:2px 8px;font-size:12px" onclick="credDel('+c.id+')">删除</button></td></tr>';
  }).join('')||'<tr><td colspan="5" class="empty">还没有凭证</td></tr>';

  el('hosts').innerHTML=hs.length?hs.map(h=>'<div class="row"><span class="mono">'+esc(h.host)+'</span>'+
    '<span class="tag" style="margin-left:auto">'+h.hits+'</span>'+
    '<span class="muted" style="max-width:280px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'+esc(h.last_path||'')+'</span></div>').join('')
    :'<div class="empty">还没有任何流量</div>';

  const caps=(S.captures||[]).filter(c=>!/[.](png|jpg|jpeg|css|js|woff2?)$/i.test(c.path));
  el('caps').innerHTML=caps.length?caps.map(c=>'<div class="row"><span class="tag">'+esc(c.method)+'</span>'+
    '<span class="mono grow">'+esc(c.host)+esc(c.path)+'</span>'+
    '<button style="padding:2px 8px;font-size:12px" data-p="'+esc(c.path)+'" data-m="'+esc(c.method)+'" onclick="pick(this)">选它</button></div>').join('')
    :'<div class="empty">还没有抓到包</div>';

  // 最近一次锁单
  const lo=S.last_order;
  if(lo && lo.status==='open'){
    el('lastOrder').style.display='';
    el('lastOrder').innerHTML='最近锁单 · 订单 '+esc(lo.order_id)+' · '+hhmmss(lo.ts)+' <button class="undoBtn" data-oid="'+esc(lo.order_id)+'">撤销锁单</button>';
    const ub=el('lastOrder').querySelector('.undoBtn');
    if(ub) ub.onclick=function(){ doCancelOrder(this.dataset.oid) };
  } else { el('lastOrder').style.display='none' }


  setView(view);
  if(!wizAutoOpened && !(S.creds||[]).length){ wizAutoOpened=true; wizOpen(); }
  if(!addrLoaded && (S.creds||[]).length){ loadAddresses(); }
}

let view='cart';
function setView(v){
  view=v;
  document.querySelectorAll('.tab').forEach(function(t){ t.classList.toggle('on', t.dataset.t===v) });
  el('viewCart').style.display = v==='cart' ? '' : 'none';
  el('viewScan').style.display = v==='scan' ? '' : 'none';
}
async function doWatch(sku){
  const on=!(S.watches||[]).includes(sku);
  try{ await post('/api/watch',{sku:sku,on:on}); await load(); }catch(e){ await uiAlert('操作失败：'+e.message) }
}
function toggleSel(sku){
  if(sel.has(sku)) sel.delete(sku); else sel.add(sku);
  const chk=[].slice.call(el('items').querySelectorAll('[data-sel]')).filter(function(n){return n.dataset.sel===sku})[0];
  if(chk) chk.parentElement.classList.toggle('sel', sel.has(sku));
  const items=S.snapshot||[];
  const chosen=items.filter(function(i){return sel.has(i.sku)});
  const sum=chosen.reduce(function(a,b){return a+Number(b.price)},0);
  el('bar').className='bar'+(chosen.length?'':' hide');
  el('selN').textContent=chosen.length;
  el('selSum').textContent=yuan(sum);
}
function setCat(node,c){
  curCat=c;
  document.querySelectorAll('[data-c]').forEach(function(n){ n.classList.toggle('on', n.dataset.c===c) });
  render();
}
function setDisc(node,d){ maxDisc=d; document.querySelectorAll('.chip').forEach(c=>c.classList.remove('on')); node.classList.add('on'); render(); }

async function doRefresh(){ try{ await post('/api/refresh'); }catch(e){ await uiAlert(e.message) } setTimeout(load,600); }
async function toggle(){ await post('/api/monitor',{enabled:!S.monitor}); load(); }
async function testNotify(){ try{ const r=await post('/api/test_notify'); await uiAlert(r.ok?'已发送，去微信看看':'发送失败，检查 webhook'); }catch(e){ await uiAlert(e.message) } }
async function saveHook(){ const v=el('hook').value.trim(); if(!v)return; await post('/api/config',{wecom_webhook:v}); el('hook').value=''; load(); }
async function savePath(){ await post('/api/config',{cart_path:el('c_path').value,cart_method:el('c_method').value}); load(); }
function pick(btn){ el('c_path').value=btn.dataset.p; el('c_method').value=btn.dataset.m; }
async function refreshScan(btn){
  const old=btn.textContent; btn.textContent='抓取中…'; btn.style.pointerEvents='none';
  try{
    const r=await post('/api/scan/refresh');
    await uiAlert('抓到 '+r.fetched+' 件，入库 '+r.written+' 条');
    await load();
  }catch(e){ await uiAlert('抓取失败：'+e.message) }
  finally{ btn.textContent=old; btn.style.pointerEvents='' }
}
async function clearScan(){ if(!await uiConfirm('清空出清池？已收集的商品记录会删除。'))return; await post('/api/scan/clear'); load(); }

async function doRemoveOne(sku){
  if(!await uiConfirm('从购物车里移除这件商品？')) return;
  try{ await post('/api/cart/remove',{skus:[sku]}); sel.delete(sku); await uiAlert('已移除'); await refreshCartOnly(); }
  catch(e){ await uiAlert('移除失败：'+e.message+'\\n\\n（若提示「接口还没录到」：开着抓包，在 App 里把数量 −1 或删除一件，录一次以后就永久有效）'); }
}
async function doRemove(){
  const skus=[...sel]; if(!skus.length) return;
  if(!await uiConfirm('从购物车移除这 '+skus.length+' 件商品？')) return;
  try{ await post('/api/cart/remove',{skus}); sel.clear(); await refreshCartOnly(); await uiAlert('已移除 '+skus.length+' 件'); }
  catch(e){ await uiAlert('移除失败：'+e.message); }
}
async function doLock(){
  const skus=[...sel]; if(!skus.length) return;
  const chosen=(S.snapshot||[]).filter(i=>sel.has(i.sku));
  const sum=chosen.reduce((a,b)=>a+Number(b.price),0);
  const ok=await uiConfirm('将提交 '+skus.length+' 件商品，合计 '+yuan(sum)+'\\n\\n这会真的占用库存，生成待支付订单（15 分钟内需付款，否则自动取消）。\\n不会自动付款。\\n\\n确定锁单？');
  if(!ok) return;
  el('btnLock').disabled=true; el('btnLock').textContent='提交中';
  try{ const r=await post('/api/order/submit',{skus}); await uiAlert('已提交 '+r.submitted+' 件，去 App 里完成支付'); sel.clear(); await load(); }
  catch(e){ await uiAlert('锁单失败：'+e.message); }
  finally{ el('btnLock').disabled=false; el('btnLock').textContent='一键锁单'; }
}

async function toggleSubmit(cb){
  if(cb.checked){
    const ok=await uiConfirm('开启后，「一键锁单」会真的提交订单并占用库存，只在手动点击时执行，不会自动付款。确定开启？');
    if(!ok){ cb.checked=false; return }
  }
  await post('/api/danger',{allow_submit_order:cb.checked});
  await load();
}
async function loadAddresses(force){
  if(addrLoaded && !force) return;
  el('addrHint').textContent='加载中…';
  try{
    const res=await fetch('/api/addresses');
    const r=await res.json().catch(function(){return {}});
    if(!res.ok) throw new Error(r.detail||('HTTP '+res.status));
    const items=r.items||[];
    el('addrSel').innerHTML=items.length?items.map(function(a){
      return '<option value="'+esc(a.id)+'"'+(a.id===r.active?' selected':'')+'>'+
        esc((a.title||'')+(a.in_service?'':'（不在配送范围）'))+'</option>';
    }).join(''):'<option value="">暂无保存的地址</option>';
    addrLoaded=true;
    el('addrHint').textContent=items.length?('共 '+items.length+' 个地址'):'账号里没有保存的地址';
  }catch(e){
    // 不自动重试（render 每 4 秒一次，会把接口刷爆），交给「刷新」按钮
    addrLoaded=true;
    el('addrSel').innerHTML='<option value="">（未加载）</option>';
    el('addrHint').textContent='地址加载失败，点「刷新」重试';
  }
}
async function refreshAddr(btn){
  const old=btn.textContent;
  btn.disabled=true; btn.textContent='刷新中';
  await loadAddresses(true);
  btn.disabled=false; btn.textContent=old;
}
async function changeAddr(sel){
  if(!sel.value) return;
  el('addrHint').textContent='切换中…';
  try{
    const r=await post('/api/address/select',{id:sel.value});
    el('addrHint').textContent = r.in_service ? '已切换：'+r.name : '已切换，但该地址不在配送范围';
    await load();
  }catch(e){
    el('addrHint').textContent='切换失败';
    await uiAlert('切换地址失败：'+e.message);
  }
}

async function doAdd(sku, btn){
  const old=btn?btn.textContent:'';
  if(btn){ btn.disabled=true; btn.textContent='加购中' }
  try{
    await post('/api/scan/add',{sku:sku});
    // 局部更新：删掉这一行，只刷购物车，不拉整个 state
    const tr=btn?btn.closest('tr'):null;
    if(tr){
      tr.remove();
      el('cntScan').textContent='可加购 '+el('scan').querySelectorAll('tbody tr').length+' 件';
      el('tabScan').textContent=Math.max(0,Number(el('tabScan').textContent||1)-1);
    }
    await refreshCartOnly();
  }catch(e){
    await uiAlert('加购失败：'+e.message+'\\n\\n（若提示「接口还没录到」：开着抓包，在 App 里点一次加购或把数量 +1，录一次以后就永久有效）');
    if(btn){ btn.disabled=false; btn.textContent=old }
  }
}
async function doCancelOrder(oid){
  if(!await uiConfirm('撤销这笔锁单？商品会退回购物车。')) return;
  try{ await post('/api/order/cancel',{order_id:oid}); await uiAlert('已撤销，商品已退回购物车'); await load(); }
  catch(e){ await uiAlert('撤销失败：'+e.message); }
}
async function credDel(id){ await fetch('/api/credential/'+id,{method:'DELETE'}); load(); }
async function credOk(id){ await post('/api/credential/'+id+'/ok',{}); load(); }
async function addCred(){
  const b={platform:el('c_platform').value,account:el('c_account').value||'默认',
    access_token:el('c_token').value||null,user_id:el('c_uid').value||null};
  if(b.platform!=='mock'&&!b.access_token){await uiAlert('先填 access_token');return}
  await post('/api/credential',b); el('c_token').value=''; el('c_uid').value=''; load();
}


let wizTimer=null, wizBase=0, wizCart=0, wizAutoOpened=false, addrLoaded=false;
async function wizOpen(){
  el('wizard').classList.remove('hide');
  const s=await wizStateGet();
  wizCart=s?(s.cart_ts||0):0;   // 记下打开时的模板时间，之后比它新才算重录成功
  await wizRefresh();
  if(wizTimer) clearInterval(wizTimer);
  wizTimer=setInterval(wizRefresh, 1200);
}
function wizClose(){
  el('wizard').classList.add('hide');
  if(wizTimer){ clearInterval(wizTimer); wizTimer=null }
}
async function wizStateGet(){
  try{ return await (await fetch('/api/setup')).json() }catch(e){ return null }
}
async function wizRefresh(){
  const s=await wizStateGet();
  if(!s) return;
  const s1=el('s1'), s1b=el('s1btn'), s2=el('s2'), s2b=el('s2btn'), s3=el('s3');
  if(s.cert){
    s1.className='step done'; s1b.textContent='已信任'; s1b.disabled=true;
    el('s1desc').textContent='证书已在本机受信任根中，随时可在设置里卸载。';
  }else{
    s1.className='step cur'; s1b.textContent='安装'; s1b.disabled=false;
  }
  if(s.proxy_on){ s2.className='step done'; s2b.textContent='已开启'; s2b.disabled=false }
  else { s2.className=s.cert?'step cur':'step'; s2b.textContent='开启'; s2b.disabled=!s.cert }
  const d=Math.max(0, (s.hits||0) - wizBase);
  const fresh=(s.cart_ts||0)>wizCart;
  if(s.has_token&&fresh){
    s3.className='step done';
    el('wizLive').textContent='凭证已拿到 · 购物车模板已重录 ✓';
  }else if(s.has_token){
    s3.className=s.proxy_on?'step cur':'step';
    el('wizLive').textContent=s.proxy_on
      ? ('凭证已拿到，还差购物车页 —— 进小程序点一下「购物车」（已捕获 '+d+' 条）')
      : '凭证已拿到 · 购物车模板未重录';
  }else{
    s3.className=(s.cert&&s.proxy_on)?'step cur':'step';
    el('wizLive').textContent = s.proxy_on ? ('已捕获 '+d+' 条请求') : '等待开启代理';
  }
  el('wizDone').disabled = !s.has_token;
  el('wizHint').textContent = s.proxy_error ? s.proxy_error : (s.has_token?'':'');
}
async function wizCert(){
  const b=el('s1btn'); b.disabled=true; b.textContent='安装中';
  try{
    const r=await post('/api/setup/cert',{action:'install'});
    if(!r.ok) await uiAlert(r.msg || '安装失败，可能被安全软件拦了');
  }catch(e){ await uiAlert('安装失败：'+e.message) }
  b.textContent='安装'; b.disabled=false;
  await wizRefresh();
}
async function wizProxy(){
  const b=el('s2btn'); b.disabled=true; b.textContent='开启中';
  try{
    const st=await wizStateGet();
    wizBase=st?st.hits:0;
    await post('/api/setup/proxy',{on:true});
  }catch(e){ await uiAlert('开启失败：'+e.message) }
  b.disabled=false; b.textContent='开启';
  await wizRefresh();
}
async function wizFinish(){
  try{ await post('/api/setup/finish'); }catch(e){}
  wizClose();
  addrLoaded=false;   // 凭证变了，地址需要重新拉一次
  await load();
}

let lastFull=0;
(async function tick(){
  try{
    // 购物车视图走轻量接口（几百字节），全量状态 20 秒才拉一次
    if(S.refreshing || Date.now()-lastFull>20000){ lastFull=Date.now(); await load(); }
    else if(view==='cart'){ await refreshCartOnly(); }
  }catch(e){}
  setTimeout(tick, S.refreshing?1200:4000);
})();
</script></body></html>"""
