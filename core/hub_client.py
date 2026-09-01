"""
Client for the Alfredo Workflow Hub registry.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

import requests


class HubClientError(Exception):
    pass


class HubClient:
    def __init__(
        self,
        base_url: Optional[str] = None,
        username: Optional[str] = None,
        token: Optional[str] = None,
    ):
        self.base_url = (base_url or os.getenv("HUB_API_URL") or "http://localhost:8010").rstrip("/")
        self.username = username or os.getenv("HUB_USERNAME") or ""
        self.token = token or os.getenv("HUB_TOKEN") or ""

    @property
    def enabled(self) -> bool:
        mode = (os.getenv("HUB_MODE") or "off").strip().lower()
        return mode in ("local", "remote") and bool(self.base_url)

    def with_credentials(self, username: str = "", token: str = "", base_url: str = None) -> "HubClient":
        """Return a client copy with overridden credentials (used by UI settings)."""
        return HubClient(
            base_url=base_url or self.base_url,
            username=username or self.username,
            token=token or self.token,
        )

    def _headers(self, auth: bool = True) -> Dict[str, str]:
        h = {"Accept": "application/json", "Content-Type": "application/json"}
        if auth and self.username and self.token:
            h["X-Hub-Username"] = self.username
            h["X-Hub-Token"] = self.token
        return h

    def _request(self, method: str, path: str, auth: bool = True, **kwargs) -> Any:
        url = f"{self.base_url}{path}"
        try:
            res = requests.request(method, url, headers=self._headers(auth=auth), timeout=60, **kwargs)
        except requests.RequestException as e:
            raise HubClientError(f"Hub unreachable: {e}") from e
        if res.status_code >= 400:
            try:
                detail = res.json().get("detail") or res.text
            except Exception:
                detail = res.text
            raise HubClientError(f"HTTP {res.status_code}: {detail}")
        if res.status_code == 204 or not res.content:
            return None
        return res.json()

    def health(self) -> Dict[str, Any]:
        return self._request("GET", "/hub/health", auth=False)

    def register(
        self,
        username: str,
        display_name: str = "",
        org_slug: str = "",
        invite_token: str = "",
    ) -> Dict[str, Any]:
        return self._request(
            "POST",
            "/hub/register",
            auth=False,
            json={
                "username": username,
                "display_name": display_name,
                "org_slug": org_slug,
                "invite_token": invite_token,
            },
        )

    def publish(
        self,
        package: Dict[str, Any],
        *,
        slug: str,
        title: str,
        description: str = "",
        tags: Optional[List[str]] = None,
        visibility: str = "private",
        package_version: str = "1.0.0",
        share_with: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        return self._request(
            "POST",
            "/hub/packages",
            json={
                "slug": slug,
                "title": title,
                "description": description,
                "tags": tags or [],
                "visibility": visibility,
                "package_version": package_version,
                "package": package,
                "share_with": share_with or [],
            },
        )

    def search(self, q: str = "", tag: str = "", limit: int = 50) -> List[Dict[str, Any]]:
        params = {"q": q, "tag": tag, "limit": limit}
        return self._request("GET", "/hub/packages", params=params) or []

    def download(self, package_id: int) -> Dict[str, Any]:
        return self._request("GET", f"/hub/packages/{package_id}")

    def share(self, package_id: int, username: str) -> Dict[str, Any]:
        return self._request(
            "POST",
            f"/hub/packages/{package_id}/share",
            json={"username": username},
        )
