"""Static checks on the frontend: no HTML-string APIs, CSP-compatible markup, known icons."""

from __future__ import annotations

import os
import re
import unittest

WEB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")
JS_DIR = os.path.join(WEB, "js")


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def _js_files() -> dict:
    return {name: _read(os.path.join(JS_DIR, name)) for name in sorted(os.listdir(JS_DIR)) if name.endswith(".js")}


class FrontendSafetyTest(unittest.TestCase):
    def test_no_html_string_apis(self) -> None:
        banned = re.compile(r"\b(innerHTML|outerHTML|insertAdjacentHTML|document\.write|eval\s*\(|new\s+Function\s*\()")
        for name, src in _js_files().items():
            for lineno, line in enumerate(src.splitlines(), 1):
                self.assertIsNone(banned.search(line), "%s:%d uses an HTML-string API: %s" % (name, lineno, line.strip()))

    def test_index_has_no_inline_script_or_style(self) -> None:
        html = _read(os.path.join(WEB, "index.html"))
        self.assertNotRegex(html, r"\sstyle=", "inline style attributes are blocked by the CSP")
        self.assertNotRegex(html, r"<style[\s>]", "inline <style> is blocked by the CSP")
        for m in re.finditer(r"<script\b([^>]*)>(.*?)</script>", html, re.S):
            self.assertIn("src=", m.group(1), "inline <script> is blocked by the CSP")
            self.assertEqual(m.group(2).strip(), "")
        self.assertNotRegex(html, r"\son[a-z]+=", "inline event handlers are blocked by the CSP")

    def test_no_style_attribute_strings_in_js(self) -> None:
        # setAttribute('style', ...) is blocked by the CSP; element.style.* is fine.
        for name, src in _js_files().items():
            self.assertNotRegex(src, r"setAttribute\(\s*['\"]style['\"]", name)

    def test_every_icon_used_exists_in_sprite(self) -> None:
        html = _read(os.path.join(WEB, "index.html"))
        defined = set(re.findall(r'<symbol id="i-([\w-]+)"', html))
        used = set()
        for src in _js_files().values():
            used.update(re.findall(r"\bicon\(\s*'([\w-]+)'", src))
        self.assertTrue(used, "expected icon() calls")
        missing = sorted(used - defined)
        self.assertEqual(missing, [], "icons used but not defined in index.html: %s" % missing)

    def test_api_calls_send_the_csrf_header(self) -> None:
        api = _read(os.path.join(JS_DIR, "api.js"))
        self.assertIn("'X-NoteToSelf': '1'", api)
        for name, src in _js_files().items():
            if name != "api.js":
                self.assertNotIn("fetch(", src, "%s should go through api.js" % name)


if __name__ == "__main__":
    unittest.main()
