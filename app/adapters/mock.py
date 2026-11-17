"""离线自测用的假适配器：模拟购物车里几件出清商品随时间降价。"""
import random
import time

from ..models import Item
from .base import Adapter

BASE = [
    ("排骨 500g", 39.9, 19.9),
    ("里脊肉 400g", 26.8, 12.9),
    ("三明治 火腿芝士", 12.5, 5.0),
    ("鲜奶 950ml", 18.0, 9.9),
]


class MockAdapter(Adapter):
    platform = "mock"

    def __init__(self, cred=None, **_):
        self.cred = cred
        self.t0 = time.time() - 240

    def fetch_cart(self):
        # 每 30 秒整体降一档，模拟出清阶梯降价
        step = int((time.time() - self.t0) / 30) * 0.03
        items = []
        for i, (name, origin, price) in enumerate(BASE):
            p = max(round(price * (1 - step), 2), 0.01)
            items.append(
                Item(
                    platform=self.platform,
                    sku=f"mock-{i}",
                    name=name,
                    price=p,
                    origin_price=origin,
                    qty=1 + random.randint(0, 1),
                    tags=["出清"] if i % 2 == 0 else [],
                )
            )
        return items
