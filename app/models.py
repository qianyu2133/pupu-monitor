from dataclasses import dataclass, field
from typing import Optional, List


@dataclass
class Item:
    platform: str
    sku: str
    name: str
    price: float
    origin_price: Optional[float] = None
    qty: float = 1
    image: Optional[str] = None
    tags: List[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    @property
    def discount(self):
        if self.origin_price and self.origin_price > 0:
            return round(self.price / self.origin_price, 3)
        return None

    @property
    def is_clearance(self):
        keys = ("出清", "清仓", "临期", "特价", "折扣", "买一送一", "捡漏")
        blob = " ".join(self.tags) + self.name
        return any(k in blob for k in keys)
