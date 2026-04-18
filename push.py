#!/usr/bin/env python3
"""Fetch Beszel server stats and push to a TRMNL Private Plugin webhook."""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


DEFAULT_PAYLOAD_LIMIT_BYTES = 2048


def env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None or value == "":
        print(f"missing env: {name}", file=sys.stderr)
        sys.exit(2)
    return value


def http(
    url: str,
    *,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    expect_json: bool = True,
    timeout: float = 15.0,
) -> dict[str, Any]:
    data = encode_json(body) if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", "beszel-trmnl/1.0 (+https://github.com/mrkaran)")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        content_type = resp.headers.get("Content-Type", "")
    if not expect_json:
        return {}
    if not raw:
        return {}
    if "json" not in content_type.lower():
        raise ValueError(f"expected JSON response from {url}, got {content_type or 'unknown content type'}")
    return json.loads(raw)


def encode_json(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode()


def beszel_token(base_url: str, email: str, password: str) -> str:
    resp = http(
        f"{base_url.rstrip('/')}/api/collections/users/auth-with-password",
        method="POST",
        body={"identity": email, "password": password},
    )
    return resp["token"]


def beszel_systems(base_url: str, token: str) -> list[dict[str, Any]]:
    resp = http(
        f"{base_url.rstrip('/')}/api/collections/systems/records?perPage=100&sort=name",
        headers={"Authorization": token},
    )
    return resp.get("items", [])


def format_uptime(seconds: Any) -> str:
    try:
        s = max(0, int(float(seconds or 0)))
    except (TypeError, ValueError):
        s = 0
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s / 3600:.1f}h"
    days = s / 86400
    if days < 10:
        return f"{days:.1f}d"
    return f"{int(days)}d"


def round1(x: Any) -> float:
    try:
        return round(float(x), 1)
    except (TypeError, ValueError):
        return 0.0


def build_payload(systems: list[dict[str, Any]], tz: str) -> dict[str, Any]:
    now_local = datetime.now(tz=ZoneInfo(tz))
    servers: list[dict[str, Any]] = []
    up = 0

    for s in systems:
        info = s.get("info") or {}
        status = s.get("status", "unknown")
        if status == "up":
            up += 1

        temp = info.get("dt")
        servers.append(
            {
                "n": s.get("name", "?"),
                "s": status,
                "cpu": round1(info.get("cpu")),
                "mem": round1(info.get("mp")),
                "dsk": round1(info.get("dp")),
                "up": format_uptime(info.get("u", 0)),
                "la": round1((info.get("la") or [0])[0]),
                "t": round1(temp) if temp is not None else None,
            }
        )

    def avg(key: str) -> float:
        vals = [s[key] for s in servers if s["s"] == "up"]
        return round1(sum(vals) / len(vals)) if vals else 0.0

    return {
        "merge_variables": {
            "ts": now_local.strftime("%H:%M"),
            "date": now_local.strftime("%a %b %-d"),
            "up": up,
            "total": len(servers),
            "cpu_avg": avg("cpu"),
            "mem_avg": avg("mem"),
            "dsk_avg": avg("dsk"),
            "servers": servers,
        }
    }


def push(webhook_url: str, payload: dict[str, Any]) -> None:
    # TRMNL webhook: POST to https://trmnl.com/api/custom_plugins/{UUID}
    http(webhook_url, method="POST", body=payload, expect_json=False, timeout=20.0)


def payload_limit() -> int:
    raw = os.environ.get("TRMNL_PAYLOAD_LIMIT_BYTES")
    if not raw:
        return DEFAULT_PAYLOAD_LIMIT_BYTES
    try:
        limit = int(raw)
    except ValueError:
        print(f"invalid TRMNL_PAYLOAD_LIMIT_BYTES={raw!r}", file=sys.stderr)
        sys.exit(2)
    if limit <= 0:
        print("TRMNL_PAYLOAD_LIMIT_BYTES must be greater than 0", file=sys.stderr)
        sys.exit(2)
    return limit


def main() -> int:
    base = env("BESZEL_URL").rstrip("/")
    email = env("BESZEL_EMAIL")
    password = env("BESZEL_PASSWORD")
    webhook = os.environ.get("TRMNL_WEBHOOK_URL", "")
    tz = os.environ.get("TZ", "UTC")
    dry = not webhook or "REPLACE" in webhook

    try:
        token = beszel_token(base, email, password)
        systems = beszel_systems(base, token)
    except urllib.error.HTTPError as e:
        print(f"beszel error {e.code}: {e.read().decode(errors='replace')}", file=sys.stderr)
        return 1
    except urllib.error.URLError as e:
        print(f"beszel network error: {e.reason}", file=sys.stderr)
        return 1
    except (KeyError, ValueError) as e:
        print(f"beszel response error: {e}", file=sys.stderr)
        return 1

    try:
        payload = build_payload(systems, tz)
    except ZoneInfoNotFoundError:
        print(f"invalid timezone in TZ: {tz}", file=sys.stderr)
        return 1

    limit = payload_limit()
    size = len(encode_json(payload))

    if dry:
        print(json.dumps(payload, indent=2))
        if size > limit:
            print(f"# dry-run: payload {size}B exceeds TRMNL limit {limit}B", file=sys.stderr)
        else:
            print(f"# dry-run (no TRMNL_WEBHOOK_URL): payload {size}B", file=sys.stderr)
        return 0

    if size > limit:
        print(f"payload {size}B exceeds TRMNL limit {limit}B; not pushing", file=sys.stderr)
        return 1

    try:
        push(webhook, payload)
    except urllib.error.HTTPError as e:
        print(f"trmnl error {e.code}: {e.read().decode(errors='replace')}", file=sys.stderr)
        return 1
    except urllib.error.URLError as e:
        print(f"trmnl network error: {e.reason}", file=sys.stderr)
        return 1
    except ValueError as e:
        print(f"trmnl response error: {e}", file=sys.stderr)
        return 1

    print(
        f"ok: {payload['merge_variables']['up']}/{payload['merge_variables']['total']} up, "
        f"payload {size}B @ {payload['merge_variables']['ts']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
