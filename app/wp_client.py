import requests
from urllib.parse import urljoin

class WPClient:
    def __init__(self, base_url: str, timeout: int = 20):
        self.base_url = base_url.rstrip("/") + "/"
        self.timeout = timeout

    def _get(self, path: str, params: dict):
        url = urljoin(self.base_url, path.lstrip("/"))
        r = requests.get(url, params=params, timeout=self.timeout)
        r.raise_for_status()
        return r.json(), r.headers

    def list_posts(self, page: int = 1, per_page: int = 20):
        return self._get(
            "/wp-json/wp/v2/posts",
            {
                "page": page,
                "per_page": per_page,
                "_embed": 1,
                "orderby": "date",
                "order": "desc",
                "status": "publish",
            },
        )

    def list_categories(self, page: int = 1, per_page: int = 100):
        return self._get(
            "/wp-json/wp/v2/categories",
            {"page": page, "per_page": per_page, "hide_empty": True},
        )
