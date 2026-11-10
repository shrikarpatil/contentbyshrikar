"""Temporary probe: compare Instagram Graph API request variants from this network."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from urllib.parse import urlencode

TOKEN = os.environ["INSTAGRAM_ACCESS_TOKEN"]
USER_ID = os.environ["INSTAGRAM_USER_ID"]
VERSION = os.environ.get("INSTAGRAM_GRAPH_API_VERSION", "v27.0")
BASE = f"https://graph.facebook.com/{VERSION}"
BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
SAFE_HEADERS = ("x-app-usage", "x-business-use-case-usage", "x-fb-trace-id", "x-fb-request-id", "www-authenticate", "proxy-status")


def call(label: str, method: str, path: str, *, fields=None, bearer=True, query_token=False, body_token=False, ua=None) -> None:
    fields = dict(fields or {})
    headers = {}
    if bearer:
        headers["Authorization"] = f"Bearer {TOKEN}"
    if query_token:
        fields["access_token"] = TOKEN
    if body_token:
        fields["access_token"] = TOKEN
    if ua:
        headers["User-Agent"] = ua
    data = None
    url = f"{BASE}/{path}"
    if method == "GET":
        if fields:
            url += "?" + urlencode(fields)
    else:
        data = urlencode(fields).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            status, text, hdrs = response.status, response.read().decode(), response.headers
    except urllib.error.HTTPError as exc:
        status, text, hdrs = exc.code, exc.read().decode(), exc.headers
    text = text.replace(TOKEN, "[redacted]")[:400]
    shown = {name: hdrs.get(name) for name in SAFE_HEADERS if hdrs.get(name)}
    print(f"[{label}] status={status} body={text} headers={shown}", flush=True)


def main() -> None:
    ip = urllib.request.urlopen("https://api.ipify.org", timeout=20).read().decode()
    print(f"egress_ip={ip}", flush=True)
    call("read-bearer", "GET", USER_ID, fields={"fields": "id,username"})
    call("read-query-token", "GET", USER_ID, fields={"fields": "id,username"}, bearer=False, query_token=True)
    call("read-bearer-browser-ua", "GET", USER_ID, fields={"fields": "id,username"}, ua=BROWSER_UA)
    limits = {"fields": "content_publishing_limit,quota_usage,config"}
    call("publish-limit", "GET", f"{USER_ID}/content_publishing_limit", fields={"fields": "quota_usage,config"})
    container = {"media_type": "REELS", "upload_type": "resumable", "caption": "probe", "is_ai_generated": "true"}
    call("create-bearer", "POST", f"{USER_ID}/media", fields=container)
    call("create-body-token", "POST", f"{USER_ID}/media", fields=container, bearer=False, body_token=True)
    call("create-bearer-browser-ua", "POST", f"{USER_ID}/media", fields=container, ua=BROWSER_UA)


if __name__ == "__main__":
    main()
