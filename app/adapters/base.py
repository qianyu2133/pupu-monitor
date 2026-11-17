class AdapterError(Exception):
    pass


class AuthExpired(AdapterError):
    """凭证失效，需要重新抓包。"""


class NotConfigured(AdapterError):
    """接口路径还没配置（还没抓到对应的包）。"""


class Adapter:
    platform = "base"

    def fetch_cart(self):
        raise NotImplementedError
