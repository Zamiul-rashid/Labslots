"""Minimal Headscale REST client (v0.29 /api/v1)."""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx


def _get(d: dict, snake: str, camel: str, default=None):
    if snake in d:
        return d[snake]
    return d.get(camel, default)


@dataclass
class Node:
    id: str
    name: str
    owner: str  # user email, else user name; "" for tagged nodes
    tags: list[str] = field(default_factory=list)
    online: bool = False
    ips: list[str] = field(default_factory=list)
    created: str = ""
    last_seen: str = ""

    @property
    def tagged(self) -> bool:
        return bool(self.tags)

    @property
    def ipv4(self) -> str:
        return next((ip for ip in self.ips if "." in ip), "")


def parse_node(d: dict) -> Node:
    user = d.get("user") or {}
    tags = _get(d, "tags", "tags") or []
    owner = "" if tags else (user.get("email") or user.get("name") or "")
    return Node(
        id=str(d.get("id", "")),
        name=_get(d, "given_name", "givenName") or d.get("name", ""),
        owner=owner.lower(),
        tags=list(tags),
        online=bool(d.get("online", False)),
        ips=list(_get(d, "ip_addresses", "ipAddresses") or []),
        created=str(_get(d, "created_at", "createdAt") or ""),
        last_seen=str(_get(d, "last_seen", "lastSeen") or ""),
    )


class HeadscaleError(RuntimeError):
    pass


class Headscale:
    def __init__(self, url: str, api_key: str, timeout: float = 15.0):
        self._c = httpx.Client(
            base_url=url.rstrip("/") + "/api/v1",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
        )

    def _req(self, method: str, path: str, **kw) -> dict:
        try:
            r = self._c.request(method, path, **kw)
        except httpx.HTTPError as e:
            raise HeadscaleError(f"{method} {path}: {e}") from e
        if r.status_code >= 300:
            raise HeadscaleError(f"{method} {path}: HTTP {r.status_code} {r.text[:500]}")
        return r.json() if r.content else {}

    def nodes(self) -> list[Node]:
        return [parse_node(n) for n in self._req("GET", "/node").get("nodes", [])]

    def users(self) -> list[dict]:
        return self._req("GET", "/user").get("users", [])

    def get_policy(self) -> str:
        return self._req("GET", "/policy").get("policy", "")

    def set_policy(self, text: str) -> None:
        self._req("PUT", "/policy", json={"policy": text})

    def delete_node(self, node_id: str) -> None:
        self._req("DELETE", f"/node/{node_id}")
