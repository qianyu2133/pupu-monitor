"""朴朴超市适配器：纯 HTTP，靠抓包拿到的 Authorization + pp-userid。"""
import json
import time

import requests

from .. import config, db
from .base import Adapter, AdapterError, AuthExpired, NotConfigured
from .parser import extract_items, harvest_products, parse_pupu, save_sample

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 "
    "MicroMessenger/8.0.47.2560(0x28002F30) NetType/WIFI "
    "MiniProgramEnv/Windows WindowsWechat/WMPF WindowsWechat(0x63090a13) XWEB/9129"
)


class PupuAdapter(Adapter):
    platform = "pupu"

    def __init__(self, cred):
        self.cred = cred
        self.conf = config.load().get("pupu", {})
        self.s = requests.Session()
        self.s.verify = True

    def _base(self):
        return (self.conf.get("host") or "").rstrip("/")

    def headers(self):
        return {
            "Authorization": self.cred.get("access_token") or "",
            "pp-userid": str(self.cred.get("user_id") or ""),
            "pp-os": "0",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Origin": "https://ma.pupumall.com",
            "Referer": "https://ma.pupumall.com/",
            "User-Agent": UA,
        }

    def fetch_cart(self):
        # 优先用录制下来的真实请求原样重放——朴朴的必填请求头太多，猜不出来
        rep = db.get_replay("pupu", "cart")
        if rep and rep.get("url"):
            items = self._fetch_by_replay(rep)
        else:
            items = self._fetch_by_config()
        try:
            db.upsert_scan("pupu", items, "购物车")
            db.replace_cart("pupu", [it.sku for it in items])
            db.mark_in_cart("pupu", [(it.raw or {}).get("product_id") for it in items])
        except Exception:
            pass
        return items

    def _hdrs(self, rep=None):
        """优先用抓包时存下的最新请求头，再叠加当前 token。

        这样 token 过期重抓一次后，所有录制模板都会自动用上新头，
        不需要重录。
        """
        rep_headers = (rep or {}).get("headers") or {}
        base = db.get_headers("pupu") or rep_headers
        h = {
            k: v
            for k, v in base.items()
            if k.lower() not in ("content-length", "host", "connection", "accept-encoding")
        }
        # 模板里的 content-type 优先（GET/POST 格式可能不同）
        for k, v in rep_headers.items():
            if k.lower() == "content-type":
                h[k] = v
        token = self.cred.get("access_token")
        if token:
            hit = False
            for k in list(h):
                if k.lower() == "authorization":
                    h[k] = token
                    hit = True
            if not hit:
                h["Authorization"] = token
        uid = self.cred.get("user_id")
        if uid:
            for k in list(h):
                if k.lower() == "pp-userid":
                    h[k] = uid

        # 当前选中的收货地址 → 决定门店（这 4 个头发给服务端）
        act = db.get_active_address("pupu")
        if act:
            def _set(name, value):
                if value is None:
                    return
                for k in list(h):
                    if k.lower() == name.lower():
                        h[k] = str(value)
                        return
                h[name] = str(value)

            _set("pp_storeid", act.get("store_id"))
            _set("pp-placeid", act.get("place_id"))
            _set("pp-placezip", act.get("placezip"))
            _set("pp_store_city_zip", act.get("city_zip"))
        return h

    def list_addresses(self):
        """读取账号里已保存的收货地址。"""
        rep = db.get_replay("pupu", "cart")
        r = self.s.get(
            "https://j1.pupuapi.com/client/account/receivers/v3",
            headers=self._hdrs(rep), timeout=15,
        )
        if r.status_code in (401, 403):
            raise AuthExpired(f"HTTP {r.status_code}")
        data = r.json()
        if data.get("errcode") not in (0, None):
            raise AdapterError(f"取地址失败 {data.get('errcode')}: {data.get('errmsg')}")
        out = []
        for a in data.get("data") or []:
            pl = a.get("place") or {}
            out.append({
                "id": a.get("id"),
                "name": a.get("name"),
                "title": ((a.get("address") or "") + " " + (a.get("building_room_num") or "")).strip(),
                "full": ((a.get("extend") or {}).get("user_place_name") or "") + " " + (a.get("building_room_num") or ""),
                "store_id": pl.get("service_store_id") or a.get("service_store_id"),
                "place_id": pl.get("id"),
                "placezip": pl.get("zip"),
                "city_zip": pl.get("store_city_zip"),
                "in_service": a.get("is_in_service"),
                "is_default": a.get("is_default"),
                "raw": a,
            })
        return out

    def set_address(self, addr_id):
        for a in self.list_addresses():
            if a["id"] == addr_id:
                db.set_active_address("pupu", a)
                return a
        raise AdapterError("没找到这个地址")

    def _check_biz(self, data):
        if not isinstance(data, dict):
            return
        code = data.get("errcode", data.get("code"))
        if code in (None, 0, 200, "0", "200"):
            return
        msg = str(data.get("errmsg") or data.get("msg") or "")
        if str(code) in ("401", "403") or "token" in msg.lower() or "登录" in msg:
            raise AuthExpired(f"业务码 {code}: {msg}")
        raise NotConfigured(f"业务码 {code}: {msg}（请求可能又变了，重新点一次购物车录一份）")

    def _fetch_by_replay(self, rep):
        headers = self._hdrs(rep)

        r = self.s.request(rep.get("method") or "POST", rep["url"], headers=headers,
                           data=(rep.get("body") or None), timeout=15)
        if r.status_code in (401, 403):
            raise AuthExpired(f"HTTP {r.status_code}，凭证失效")
        r.raise_for_status()
        try:
            data = r.json()
        except ValueError:
            raise NotConfigured(f"返回不是 JSON（{r.headers.get('content-type')}）")

        self._check_biz(data)
        save_sample("pupu-cart", data)
        items = parse_pupu(data)
        if items:
            return items
        return extract_items(data, self.platform, 0.01)

    def _fetch_by_config(self):
        path = self.conf.get("cart_path") or ""
        if not path:
            raise NotConfigured("购物车接口路径未配置：先抓一次包，在控制台「抓到的接口」里选中它")
        url = self._base() + path
        method = (self.conf.get("cart_method") or "GET").upper()

        if method == "POST":
            r = self.s.post(url, headers=self.headers(), json={}, timeout=15)
        else:
            r = self.s.get(url, headers=self.headers(), params={"supplement_id": ""}, timeout=15)

        if r.status_code in (401, 403):
            raise AuthExpired(f"HTTP {r.status_code}，凭证失效")
        r.raise_for_status()
        try:
            data = r.json()
        except ValueError:
            raise NotConfigured(f"返回不是 JSON（{r.headers.get('content-type')}），路径可能不对")

        self._check_biz(data)
        save_sample("pupu-cart", data)
        items = parse_pupu(data)
        if items:
            return items
        return extract_items(data, self.platform, 0.01)

    # ---------- 写操作：参数替换重放 ----------

    @staticmethod
    def _patch_ids(body_text, ids):
        """把录制模板里的 id 数组换成新的 id 列表。返回 (新 body, 是否替换成功)。"""
        if not body_text:
            return "", False
        try:
            data = json.loads(body_text)
        except Exception:
            return body_text, False

        hit = {"n": 0}

        def walk(node):
            if isinstance(node, dict):
                for k, v in list(node.items()):
                    if isinstance(v, list) and v and all(
                        isinstance(x, str) and len(x) >= 16 and "-" in x for x in v
                    ):
                        node[k] = list(ids)
                        hit["n"] += 1
                    else:
                        walk(v)
            elif isinstance(node, list):
                for x in node:
                    walk(x)

        walk(data)
        return json.dumps(data, ensure_ascii=False, separators=(",", ":")), hit["n"] > 0

    def _send(self, rep, body, label=""):
        """用录到的模板发一次请求，body 由我们重建。"""
        if not rep or not rep.get("url"):
            raise NotConfigured(f"{label}的请求还没录到，开着抓包在 App 里操作一次")
        headers = self._hdrs(rep)
        payload = None if body is None else json.dumps(body, ensure_ascii=False, separators=(",", ":"))
        r = self.s.request(rep.get("method") or "POST", rep["url"], headers=headers, data=payload, timeout=20)
        if r.status_code in (401, 403):
            raise AuthExpired(f"HTTP {r.status_code}")
        try:
            data = r.json()
        except Exception:
            raise AdapterError(f"{label}返回不是 JSON: {r.text[:120]}")
        code = data.get("errcode", data.get("code"))
        msg = str(data.get("errmsg") or data.get("msg") or "")
        # 朴朴有些"成功"是用非 0 业务码表示的，比如下单成功返回 17115002
        ok = code in (None, 0, 200, "0", "200", 17115002) or "成功" in msg
        if not ok:
            raise AdapterError(f"{label}失败 {code}: {msg}")
        data["_ok_msg"] = msg
        return data

    def set_selection(self, cart_item_ids, selected=True):
        """勾选 / 取消勾选购物车里的商品。"""
        rep = db.get_replay("pupu", "w:client_shopping_cart_shopping_cart_item_selection_status_v3")
        if not rep:
            rep = db.find_replay("pupu", ("selection_status",))
        body = {
            "selection_status_ros": [
                {"is_selected": bool(selected), "item_id": i} for i in cart_item_ids
            ],
            "purchase_pattern": 10,
        }
        return self._send(rep, body, "勾选商品")

    @staticmethod
    def _item_ro(bm, selected_count=None):
        """用购物车返回的字段拼出加购/改数量接口要的 add_item_ro。"""
        ro = {
            "item_id": bm.get("id"),
            "product_id": bm.get("product_id"),
            "current_price": bm.get("current_price"),
            "product_tag": bm.get("product_tag") or 0,
            "selected_count": bm.get("selected_count") if selected_count is None else selected_count,
            "is_selected": bool(bm.get("is_selected")),
            "type": bm.get("type") or 0,
        }
        for k in ("activity_id", "batch_id"):
            v = bm.get(k)
            if v:
                ro[k] = v
        return ro

    def _post_cart_item(self, ro):
        rep = db.get_replay("pupu", "w:client_shopping_cart_shopping_cart_item_purchasing_product_v3")
        if not rep:
            rep = db.find_replay("pupu", ("purchasing_product",))
        if not rep:
            raise NotConfigured("这个接口还没录到：在 App 里把某件商品数量 +1 再 -1，就会自动录下")
        payload = {"add_item_ro": ro, "return_data_type": 0, "purchase_pattern": 10}
        return self._send(rep, payload, "修改购物车")

    def set_count(self, bm, count):
        """把某件商品的数量改成 count（0 就是删除）。bm 是购物车条目的 base_model。"""
        return self._post_cart_item(self._item_ro(bm, selected_count=int(count)))

    def remove_items(self, cart_item_ids):
        """删除 = 把数量改成 0（朴朴没有独立删除接口）。"""
        items = self.fetch_cart()
        by_id = {(it.raw or {}).get("id"): it.raw for it in items if (it.raw or {}).get("id")}
        done = 0
        for iid in cart_item_ids:
            bm = by_id.get(iid)
            if not bm:
                continue
            self.set_count(bm, 0)
            done += 1
        return {"removed": done}

    @staticmethod
    def _order_items(chosen):
        out = []
        for bm in chosen:
            it = {
                "product_id": bm.get("product_id"),
                "count": bm.get("selected_count") or 1,
                "price": bm.get("current_price"),
                "is_gift": False,
                "spread_tag": 0,
                "store_product_id": bm.get("store_product_id"),
                "type": bm.get("type") or 0,
                "cart_item_id": bm.get("id"),
                "cart_item_time_create": bm.get("time_create"),
            }
            if bm.get("batch_id"):
                it["batch_id"] = bm["batch_id"]
            if bm.get("activity_id"):
                it["activity_ids"] = [bm["activity_id"]]
            out.append(it)
        return out

    def fetch_crazy_discount(self, max_pages=20, delay=0.35):
        """抓「疯狂折扣」完整列表：分页拉到底（每页 20 条，实测约 242 件）。"""
        rep = db.get_replay("pupu", "cart")
        if not rep:
            raise NotConfigured("还没有购物车录制，先抓一次包")
        headers = {
            k: v for k, v in self._hdrs(rep).items() if k.lower() != "content-type"
        }

        url = "https://j1.pupuapi.com/client/marketing/channel/crazy_discount/hub/v2"
        out = {}
        pages = 0
        for page in range(1, max_pages + 1):
            r = self.s.get(url, params={"page": page}, headers=headers, timeout=25)
            if r.status_code in (401, 403):
                raise AuthExpired(f"HTTP {r.status_code}")
            body = r.json()
            if body.get("errcode") not in (0, None):
                raise AdapterError(f"业务码 {body.get('errcode')}: {body.get('errmsg')}")
            data = body.get("data") or {}
            plist = data.get("product_list") or []
            pages += 1
            for it in harvest_products({"data": data}):
                out[it["sku"]] = it
            if data.get("is_end_page") or not plist:
                break
            time.sleep(delay)
        print(f"[crazy_discount] 抓了 {pages} 页，{len(out)} 件")
        return list(out.values())

    def fetch_flashsale(self):
        """拉「疯狂折扣」页的全部商品（无分页，一次返回当前场次）。"""
        rep = db.get_replay("pupu", "cart")
        if not rep:
            raise NotConfigured("还没有购物车录制，先抓一次包")
        headers = {
            k: v for k, v in self._hdrs(rep).items() if k.lower() != "content-type"
        }

        url = "https://j1.pupuapi.com/client/marketing/channel/flashsale/products/index/v2"
        r = self.s.get(url, headers=headers, timeout=20)
        if r.status_code in (401, 403):
            raise AuthExpired(f"HTTP {r.status_code}")
        data = r.json()
        if data.get("errcode") not in (0, None):
            raise AdapterError(f"业务码 {data.get('errcode')}: {data.get('errmsg')}")

        out = []
        for group in data.get("data") or []:
            for p in group.get("products") or []:
                price = p.get("price")
                market = p.get("market_price") or p.get("original_price")
                if not p.get("name") or not price:
                    continue
                fs = p.get("flash_sale_info") or {}
                acts = p.get("activity_ids") or []
                out.append({
                    "sku": str(fs.get("store_product_id") or p.get("id") or p.get("product_id")),
                    "name": p["name"],
                    "price": round(price / 100.0, 2),
                    "origin_price": round(market / 100.0, 2) if market else None,
                    "image": p.get("main_image"),
                    "product_id": p.get("product_id"),
                    "store_product_id": fs.get("store_product_id") or p.get("id"),
                    "activity_id": acts[0] if isinstance(acts, list) and len(acts) == 1 else None,
                })
        return out

    def add_to_cart(self, row, count=1):
        """按出清池里的记录加一件进购物车。row 是 scan_item 行（价格是元）。"""
        pid = row.get("product_id")
        if not pid:
            raise AdapterError("这条记录没有 product_id，重新扫一次疯狂折扣页就能拿到")
        ro = {
            "product_id": pid,
            "current_price": int(round(float(row.get("price") or 0) * 100)),
            "product_tag": 0,
            "selected_count": int(count),
            "is_selected": True,
            "type": 0,
        }
        if row.get("activity_id"):
            ro["activity_id"] = row["activity_id"]
        res = self._post_cart_item(ro)
        try:
            self.fetch_cart()
        except Exception:
            pass
        return res

    def _settle(self, chosen_bms):
        """用当前勾选的商品重建结算请求，让服务端算好活动、券、运费。"""
        rep = db.get_replay("pupu", "w:client_order_settlement_detail_v3")
        if not rep or not rep.get("body"):
            return {}
        body = json.loads(rep["body"])
        ci = body.get("cart_info") or {}
        ci["cart_item_list"] = chosen_bms

        act_ids = {bm.get("activity_id") for bm in chosen_bms if bm.get("activity_id")}
        if isinstance(ci.get("activity_list"), list):
            ci["activity_list"] = [a for a in ci["activity_list"] if a.get("id") in act_ids]
        if isinstance(ci.get("cart_activity_list"), list):
            ci["cart_activity_list"] = [
                a for a in ci["cart_activity_list"] if a.get("activity_id") in act_ids
            ]
        body["cart_info"] = ci
        data = self._send(rep, body, "结算")
        return (data or {}).get("data") or {}

    def submit_order(self, cart_item_ids, lead_minutes=45):
        """锁单：只勾这几件 → 重建结算 → 提交订单（停在待支付，不付款）。"""
        import uuid

        items = self.fetch_cart()
        by_id = {(it.raw or {}).get("id"): it.raw for it in items if (it.raw or {}).get("id")}
        chosen = [by_id[i] for i in cart_item_ids if i in by_id]
        if not chosen:
            raise AdapterError("购物车里找不到要锁单的商品")

        others = [i for i in by_id if i not in set(cart_item_ids)]
        if others:
            self.set_selection(others, False)
        self.set_selection(cart_item_ids, True)

        # 结算能走通就优先用它（活动/券/运费由服务端算），走不通就回退手工拼单
        try:
            settle = self._settle(chosen)
        except Exception as e:
            print(f"[settle] 回退手工拼单: {e}")
            settle = {}
        pop = settle.get("place_order_param") or {}
        fee = settle.get("fee") or {}

        rep = db.get_replay("pupu", "order_submit") or db.find_replay("pupu", ("unifiedorder",))
        if not rep or not rep.get("body"):
            raise NotConfigured("下单请求还没录到：走一次结算到支付页（别付），就会自动录下")

        body = json.loads(rep["body"])
        # 用当前选中的地址覆盖门店 / 收货人，这样换地址不用重录下单模板
        act = db.get_active_address("pupu")
        if act:
            raw = act.get("raw") or {}
            body["store_id"] = act.get("store_id") or body.get("store_id")
            body["place_id"] = act.get("place_id") or body.get("place_id")
            if raw.get("id"):
                body["receiver_id"] = raw["id"]
            rc = body.get("receiver") or {}
            for src, dst in (("name", "name"), ("mobile", "mobile"),
                             ("address", "address"), ("building_room_num", "building_room_num")):
                if raw.get(src):
                    rc[dst] = raw[src]
            pb = (raw.get("place_building") or {}).get("id")
            if pb:
                rc["place_building_id"] = pb
            body["receiver"] = rc
        body["order_items"] = pop.get("order_items") or self._order_items(chosen)
        body["order_trace_id"] = pop.get("order_trace_id") or str(uuid.uuid4())
        if pop.get("add_to_cart_param") is not None:
            body["add_to_cart_param"] = pop["add_to_cart_param"]
        body["package_fee"] = pop.get("package_fee", fee.get("packing_fee", 45))
        body["special_service_fee"] = pop.get("special_service_fee", 0)
        body["logistics_fee"] = fee.get("logistics_fee", 0)
        if fee.get("total_amount") is not None:
            body["settlement_total_amount"] = fee["total_amount"]
            body["external_payment_amount"] = fee.get("external_payment_amount", fee["total_amount"])
        else:
            total = sum((x.get("price") or 0) * (x.get("count") or 1) for x in body["order_items"])
            logistics = 0 if total >= 3500 else 300
            body["logistics_fee"] = logistics
            body["settlement_total_amount"] = total + body["package_fee"] + logistics
            body["external_payment_amount"] = body["settlement_total_amount"]
        # 不能沿用录制时的券：券是绑定旧单金额的，会报"金额不满足条件金额"
        body["discount_entity_ids"] = []
        body.setdefault("coin_payment_amount", 0)

        now_ms = int(time.time() * 1000)
        target = now_ms + int(lead_minutes) * 60 * 1000
        for k in ("time_delivery_promise", "time_delivery_promise_start", "time_delivery_promise_end"):
            if k in body:
                body[k] = target
        return self._send(rep, body, "提交订单")

    def cancel_order(self, order_id):
        import re as _re

        rep = db.find_replay("pupu", ("cancel",))
        if not rep:
            raise NotConfigured("取消订单接口还没录到：在 App 里取消一次订单即可")
        uuid = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
        url = _re.sub(uuid, order_id, rep["url"])
        return self._send({**rep, "url": url}, {"id": order_id}, "取消订单")

    def try_refresh(self):
        """如果配置了刷新路径，尝试续期；返回新的 access_token 或 None。"""
        path = self.conf.get("refresh_path") or ""
        token = self.cred.get("refresh_token")
        if not path or not token:
            return None
        url = self._base() + path
        try:
            r = self.s.post(url, headers=self.headers(), json={"refresh_token": token}, timeout=15)
            if r.status_code != 200:
                return None
            data = r.json()
        except Exception:
            return None

        def dig(obj):
            if isinstance(obj, dict):
                for k in ("access_token", "accessToken", "token"):
                    if isinstance(obj.get(k), str) and len(obj[k]) > 10:
                        return obj[k]
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

        return dig(data)
