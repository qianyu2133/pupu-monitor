"""从任意结构 JSON 里抽取商品条目。

平台接口字段名会变，也不打算逐个版本硬编码，所以用容错抽取：
只要一个 dict 同时具备「名字字段」和「价格字段」，就当成一个商品。
"""
import json
import os
import time

from ..models import Item

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SAMPLE_DIR = os.path.join(ROOT, "data", "samples")

NAME_KEYS = ("productName", "skuName", "goodsName", "product_name", "name", "title")
PRICE_KEYS = ("salePrice", "currentPrice", "current_price", "sellPrice", "priceValue", "actPrice", "unitPrice", "sale_price", "price")
ORIGIN_KEYS = ("originalPrice", "originPrice", "marketPrice", "oldPrice", "linePrice", "origin_price")
SKU_KEYS = ("skuId", "sku_id", "productSkuId", "productId", "goodsId", "id")
QTY_KEYS = ("quantity", "cartNum", "qty", "count", "num")
TAG_KEYS = ("tags", "labels", "tagList", "promotionTags", "tagNames")


def to_num(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.strip().replace("¥", "").replace("￥", "").replace("元", "")
        try:
            return float(s)
        except ValueError:
            return None
    return None


def _first_str(node, keys):
    for k in keys:
        v = node.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _first_num(node, keys):
    for k in keys:
        if k in node:
            n = to_num(node[k])
            if n is not None:
                return n
    return None


def extract_items(payload, platform, price_scale=1.0):
    found = []

    def walk(node):
        if isinstance(node, dict):
            name = _first_str(node, NAME_KEYS)
            price = _first_num(node, PRICE_KEYS)
            if name and price is not None and price >= 0:
                tags = []
                for k in TAG_KEYS:
                    v = node.get(k)
                    if isinstance(v, list):
                        for x in v:
                            if isinstance(x, str):
                                tags.append(x)
                            elif isinstance(x, dict):
                                t = x.get("name") or x.get("tagName") or x.get("text")
                                if isinstance(t, str):
                                    tags.append(t)
                    elif isinstance(v, str) and v:
                        tags.append(v)
                found.append(
                    Item(
                        platform=platform,
                        sku=str(_first_str(node, SKU_KEYS) or name),
                        name=name,
                        price=round(price * price_scale, 2),
                        origin_price=(lambda o: round(o * price_scale, 2) if o else None)(
                            _first_num(node, ORIGIN_KEYS)
                        ),
                        qty=_first_num(node, QTY_KEYS) or 1,
                        tags=tags,
                        raw=node,
                    )
                )
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(payload)

    seen = {}
    for it in found:
        seen[it.sku] = it
    return list(seen.values())


def harvest_products(payload):
    """从任意朴朴商品列表响应里抓商品（要求同时有 name / 价格 / product_id，避免抓到垃圾）。"""
    out = {}

    def walk(node):
        if isinstance(node, dict):
            name = node.get("name") or node.get("product_name") or node.get("productName")
            pid = node.get("product_id") or node.get("productId")
            cur = to_num(node.get("current_price")) if "current_price" in node else None
            if cur is None:
                cur = to_num(node.get("price"))
            if isinstance(name, str) and name.strip() and cur and pid:
                origin = None
                for k in ("market_price", "original_price", "guide_price", "line_price"):
                    origin = to_num(node.get(k))
                    if origin:
                        break
                spid = node.get("store_product_id") or node.get("storeProductId")
                aid = node.get("activity_id")
                if not aid:
                    opt = node.get("optional_activity_ids")
                    if isinstance(opt, list) and len(opt) == 1 and isinstance(opt[0], str):
                        aid = opt[0]
                img = node.get("main_image") or node.get("image") or node.get("product_image")
                sku = spid or pid
                if sku not in out and not any(j in name for j in JUNK_NAMES):
                    out[sku] = {
                        "sku": str(sku),
                        "name": name.strip(),
                        "price": round(cur / 100.0, 2),
                        "origin_price": round(origin / 100.0, 2) if origin else None,
                        "image": img,
                        "product_id": pid,
                        "store_product_id": spid,
                        "activity_id": aid,
                    }
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(payload)
    return list(out.values())


def save_sample(kind, payload):
    """把原始响应存一份，方便核对字段。"""
    os.makedirs(SAMPLE_DIR, exist_ok=True)
    path = os.path.join(SAMPLE_DIR, f"{kind}-{time.strftime('%Y%m%d-%H%M%S')}.json")
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        # 只保留最近 20 个样本
        files = sorted(os.listdir(SAMPLE_DIR))
        for old in files[:-20]:
            os.remove(os.path.join(SAMPLE_DIR, old))
    except Exception as e:
        print(f"[sample] 保存失败: {e}")
    return path


# ---------------------------------------------------------------
# 朴朴专用解析：结构已知，比通用抽取准得多
#   data.business_module.product_group_module.product_group_list[].items[].base_model
#   价格单位是「分」
# ---------------------------------------------------------------

SKIP_NAMES = ("打包费", "配送费", "包装费")
JUNK_NAMES = ("红包", "优惠券", "满减券", "运费券")


def _tag_texts(node):
    out = []

    def walk(o):
        if isinstance(o, dict):
            for k in ("text", "tag_name", "name", "desc"):
                v = o.get(k)
                if isinstance(v, str) and 1 <= len(v) <= 12 and "<" not in v:
                    out.append(v)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(node)
    return out


def parse_pupu(payload):
    from ..models import Item

    data = (payload or {}).get("data") or {}
    module = ((data.get("business_module") or {}).get("product_group_module") or {})
    groups = module.get("product_group_list") or []

    items = []
    for group in groups:
        for raw in group.get("items") or []:
            bm = raw.get("base_model") or {}
            name = (bm.get("name") or "").strip()
            if not name or any(s in name for s in SKIP_NAMES):
                continue

            cur = to_num(bm.get("current_price"))
            # 注意：original_price 不可靠（促销时会出现比现价还低的值），
            # market_price / guide_price 才是吊牌价，且与 discount_rate 能对上
            orig = to_num(bm.get("market_price")) or to_num(bm.get("guide_price"))
            if cur is None:
                continue

            tags = []
            title_area = (raw.get("show_model") or {}).get("title_area") or {}
            for t in title_area.get("prefix_tag_list") or []:
                if isinstance(t, dict):
                    txt = t.get("tag_name") or t.get("text") or t.get("name")
                    if isinstance(txt, str) and txt:
                        tags.append(txt)
            # 「预估到手价」之类的营销标签，出现在 area_d
            area_d = (raw.get("show_model") or {}).get("area_d") or {}
            for txt in _tag_texts(area_d):
                if "到手价" in txt or "折" in txt or "省" in txt:
                    tags.append(txt)

            if orig and cur < orig:
                rate = bm.get("discount_rate")
                tags.append(f"{(cur / orig * 10):.1f}折")
                if isinstance(rate, (int, float)) and 0 < rate < 1:
                    pass
            if bm.get("status") not in (0, None):
                tags.append("可能售罄")

            items.append(
                Item(
                    platform="pupu",
                    sku=str(bm.get("store_product_id") or bm.get("product_id") or bm.get("id") or name),
                    name=name,
                    price=round(cur / 100.0, 2),
                    origin_price=round(orig / 100.0, 2) if orig else None,
                    qty=to_num(bm.get("selected_count")) or 1,
                    image=bm.get("main_image") or bm.get("product_original_image"),
                    tags=[t for t in dict.fromkeys(tags) if t],
                    raw=bm,
                )
            )
    return items
