"""browser_check — headless Chromium page runner with console capture + screenshot.

Launches a headless Chromium via Playwright, navigates to a URL, collects all
console messages and uncaught errors, waits for the page to settle, takes a
screenshot of the canvas (or full page), and returns everything the agent needs
to diagnose and fix browser-side rendering issues.

WebGL is enabled.  Screenshots are saved to .agent/browser_screenshots/ and
their paths are returned so the agent can view them with the file read tool.
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path


def fn(
    url: str,
    wait_seconds: float = 4.0,
    screenshot: bool = True,
    canvas_selector: str = "canvas",
    inject_js: str = "",
) -> str:
    """Open a URL in a headless browser, capture console output and a screenshot.

    Args:
        url: The URL to open (must be reachable from this machine).
        wait_seconds: Seconds to wait after page load before capturing. Increase
            for pages that take time to initialise (WebGL scenes, animations).
        screenshot: If true, save a screenshot and return its path.
        canvas_selector: CSS selector for the element to screenshot. Defaults to
            "canvas". Pass "" or "body" to screenshot the full page.
        inject_js: Optional JavaScript to evaluate after the page loads, before
            the screenshot. Useful for triggering actions or extracting state.
            The return value (if any) is appended to the output.
    """
    try:
        from playwright.sync_api import sync_playwright, Error as PlaywrightError
    except ImportError:
        return (
            "Error: playwright is not installed. "
            "Run: pip3 install playwright && python3 -m playwright install chromium"
        )

    home_cwd = os.getcwd()
    screenshots_dir = Path(home_cwd) / ".agent" / "browser_screenshots"
    screenshots_dir.mkdir(parents=True, exist_ok=True)

    console_lines: list[str] = []
    page_errors: list[str] = []

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                args=[
                    "--enable-webgl",
                    "--enable-webgl2",
                    "--use-gl=swiftshader",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-setuid-sandbox",
                ],
            )

            context = browser.new_context(viewport={"width": 1280, "height": 720})
            page = context.new_page()

            # Capture all console messages
            def _on_console(msg):
                level = msg.type.upper()
                text = msg.text
                # Include the source location for errors/warnings
                if msg.type in ("error", "warning") and msg.location:
                    loc = msg.location
                    src = f"{loc.get('url','?').split('/')[-1]}:{loc.get('lineNumber','?')}"
                    console_lines.append(f"[{level}] {text}  ({src})")
                else:
                    console_lines.append(f"[{level}] {text}")

            def _on_page_error(exc):
                page_errors.append(f"[UNCAUGHT] {exc}")

            page.on("console", _on_console)
            page.on("pageerror", _on_page_error)

            # Navigate
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=15_000)
            except PlaywrightError as e:
                browser.close()
                return f"Error: could not load {url!r}: {e}"

            # Wait for the scene to settle
            page.wait_for_timeout(int(wait_seconds * 1000))

            # Optional JS injection (e.g. trigger a key-press, read a variable)
            inject_result = ""
            if inject_js.strip():
                try:
                    val = page.evaluate(inject_js)
                    if val is not None:
                        inject_result = f"\ninjected JS result: {val}"
                except PlaywrightError as e:
                    inject_result = f"\ninjected JS error: {e}"

            # Screenshot
            screenshot_path = ""
            if screenshot:
                stamp = str(int(time.time()))
                fname = re.sub(r'[^a-z0-9_]', '_', url.lower())[:40]
                screenshot_path = str(screenshots_dir / f"{fname}_{stamp}.png")
                try:
                    selector = canvas_selector.strip() if canvas_selector.strip() else None
                    if selector:
                        el = page.query_selector(selector)
                        if el:
                            el.screenshot(path=screenshot_path)
                        else:
                            # Fall back to full page
                            page.screenshot(path=screenshot_path, full_page=False)
                            screenshot_path += "  (canvas not found, full-page fallback)"
                    else:
                        page.screenshot(path=screenshot_path, full_page=False)
                except PlaywrightError as e:
                    screenshot_path = f"(screenshot failed: {e})"

            browser.close()

    except Exception as e:
        return f"Error: browser_check failed: {e}"

    # Build output
    parts: list[str] = []

    if console_lines:
        parts.append("=== Console output ===")
        parts.extend(console_lines)
    else:
        parts.append("=== Console output: (none) ===")

    if page_errors:
        parts.append("\n=== Uncaught page errors ===")
        parts.extend(page_errors)

    if inject_result:
        parts.append(inject_result)

    if screenshot and screenshot_path:
        parts.append(f"\n=== Screenshot saved ===\n{screenshot_path}")
        parts.append(
            "Read the screenshot with: file(action='read', path='<path above>') "
            "to view it as an image."
        )

    return "\n".join(parts)


definition = {
    "type": "function",
    "function": {
        "name": "browser_check",
        "description": (
            "Open a URL in a headless Chromium browser with WebGL enabled. "
            "Captures all browser console output (console.log, console.error, "
            "THREE.js shader errors, uncaught exceptions) and takes a screenshot "
            "of the canvas element (or full page). "
            "Use this to diagnose and verify browser-side rendering: WebGL shader "
            "compile errors appear in the console output, and the screenshot lets "
            "you visually confirm the result. "
            "After calling this, read the screenshot path with the file tool to "
            "view the image. "
            "The agent can iterate: check → read error → fix file → check again."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The URL to open, e.g. 'http://localhost:8081'.",
                },
                "wait_seconds": {
                    "type": "number",
                    "description": (
                        "Seconds to wait after page load before capturing. "
                        "Default 4. Use 6-10 for heavy WebGL scenes."
                    ),
                    "default": 4.0,
                },
                "screenshot": {
                    "type": "boolean",
                    "description": "If true (default), save a screenshot and return its path.",
                    "default": True,
                },
                "canvas_selector": {
                    "type": "string",
                    "description": (
                        "CSS selector for the element to screenshot. "
                        "Default 'canvas'. Pass '' for full-page screenshot."
                    ),
                    "default": "canvas",
                },
                "inject_js": {
                    "type": "string",
                    "description": (
                        "Optional JavaScript to evaluate after the page loads. "
                        "Return value is included in output. "
                        "Example: 'document.title' or 'window.gameState?.fps'."
                    ),
                    "default": "",
                },
            },
            "required": ["url"],
        },
    },
}
