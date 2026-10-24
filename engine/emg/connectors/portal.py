"""Headless-browser portal export, driven by a declarative recipe.

A recipe is a list of steps run in order in a fresh browser profile:

    {"goto": "https://portal.example.com/login"}
    {"fill": "#email", "value": "{username}"}          # {username} {password} {totp}
    {"fill": "#password", "value": "{password}"}       # {date_from} {date_to} (ISO) and
    {"click": "button[type=submit]"}                   # {date_from_us} {date_to_us} (MM/DD/YYYY)
    {"wait_for": "text=Dashboard"}
    {"select": "#range", "value": "last30"}
    {"download": "text=Export CSV"}                    # click that starts a download; the file is the result
    {"wait_ms": 1000}

Safety: the browser may only visit ``allowed_hosts``; credentials are typed only
into pages on those hosts; a failed run keeps a screenshot for admins.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, timedelta
from urllib.parse import urlparse

from .secrets import totp

STEP_KEYS = ("goto", "fill", "click", "wait_for", "select", "download", "wait_ms", "press")
CREDENTIAL_VARS = ("{username}", "{password}", "{totp}")


@dataclass
class Fetched:
    file_name: str
    data: bytes


class PortalError(Exception):
    def __init__(self, message: str, screenshot: bytes | None = None, step: int | None = None):
        super().__init__(message)
        self.screenshot, self.step = screenshot, step


def validate(config: dict) -> None:
    hosts = config.get("allowed_hosts") or []
    if not hosts:
        raise ValueError("List the portal's host names in allowed_hosts")
    steps = config.get("steps") or []
    if not any("download" in s for s in steps):
        raise ValueError("The recipe needs a download step")
    for i, s in enumerate(steps, 1):
        kind = [k for k in STEP_KEYS if k in s]
        if len(kind) != 1:
            raise ValueError(f"Step {i} must have exactly one of {STEP_KEYS}")
        if "goto" in s and not _allowed(s["goto"], hosts):
            raise ValueError(f"Step {i} goes to {urlparse(s['goto']).hostname}, which is not in allowed_hosts")
        if "goto" in s and urlparse(s["goto"]).scheme != "https" and urlparse(s["goto"]).hostname not in ("localhost", "127.0.0.1"):
            raise ValueError(f"Step {i}: portals must use https")


def _allowed(url: str, hosts: list[str]) -> bool:
    h = (urlparse(url).hostname or "").lower()
    return any(h == a.lower() or h.endswith("." + a.lower()) for a in hosts)


def variables(creds: dict, lookback_days: int, today: date | None = None) -> dict:
    today = today or date.today()
    start = today - timedelta(days=lookback_days)
    v = {"{username}": creds.get("username", ""), "{password}": creds.get("password", ""),
         "{date_from}": start.isoformat(), "{date_to}": today.isoformat(),
         "{date_from_us}": start.strftime("%m/%d/%Y"), "{date_to_us}": today.strftime("%m/%d/%Y")}
    if creds.get("totp_secret"):
        v["{totp}"] = totp(creds["totp_secret"])
    return v


def _sub(s: str, v: dict) -> str:
    for k, x in v.items():
        s = s.replace(k, x)
    return s


def run(config: dict, creds: dict, today: date | None = None, timeout_ms: int = 60000) -> Fetched:
    from playwright.sync_api import sync_playwright

    validate(config)
    hosts = config["allowed_hosts"]
    v = variables(creds, int(config.get("lookback_days", 45)), today)
    exe = os.environ.get("PLAYWRIGHT_CHROMIUM_PATH") or None
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=exe)
        ctx = browser.new_context(accept_downloads=True)
        # Block every request to a host outside the allow-list (no stray credential or data leaks).
        ctx.route("**/*", lambda route: route.continue_() if _allowed(route.request.url, hosts) or route.request.url.startswith("data:")
                  else route.abort())
        page = ctx.new_page()
        page.set_default_timeout(timeout_ms)
        i = 0
        try:
            for i, s in enumerate(config["steps"], 1):
                if "goto" in s:
                    page.goto(_sub(s["goto"], v))
                elif "fill" in s:
                    if any(cv in s.get("value", "") for cv in CREDENTIAL_VARS) and not _allowed(page.url, hosts):
                        raise PortalError(f"Refused to type credentials into {urlparse(page.url).hostname}", step=i)
                    page.fill(s["fill"], _sub(s.get("value", ""), v))
                elif "click" in s:
                    page.click(s["click"])
                elif "press" in s:
                    page.press(s["press"], s.get("key", "Enter"))
                elif "wait_for" in s:
                    page.wait_for_selector(s["wait_for"])
                elif "select" in s:
                    page.select_option(s["select"], _sub(s["value"], v))
                elif "wait_ms" in s:
                    page.wait_for_timeout(int(s["wait_ms"]))
                elif "download" in s:
                    with page.expect_download() as d:
                        page.click(s["download"])
                    dl = d.value
                    path = dl.path()
                    return Fetched(dl.suggested_filename or "export.csv", open(path, "rb").read())
            raise PortalError("The recipe finished without downloading a file")
        except PortalError as e:
            e.screenshot = e.screenshot or _shot(page)
            raise
        except Exception as e:  # selector timeouts, navigation errors, ...
            msg = str(e).splitlines()[0][:300]
            for x in (creds.get("password"), creds.get("username")):
                if x:
                    msg = msg.replace(x, "***")
            raise PortalError(f"Step {i} failed: {msg}", _shot(page), i) from None
        finally:
            browser.close()


def _shot(page) -> bytes | None:
    try:
        return page.screenshot(full_page=True)
    except Exception:
        return None
