import json
import shutil
import subprocess
import unittest
from pathlib import Path


RENDERING_JS = Path(__file__).parents[1] / "geoai" / "server" / "static" / "rendering.js"
MARKED_JS = (
    Path(__file__).parents[1]
    / "geoai"
    / "server"
    / "static"
    / "vendor"
    / "marked.min.js"
)


@unittest.skipUnless(shutil.which("node"), "Node.js is required for frontend tests")
class FrontendRenderingTests(unittest.TestCase):
    def run_js(self, expression: str):
        script = (
            f"global.marked = require({json.dumps(str(MARKED_JS))});"
            f"const rendering = require({json.dumps(str(RENDERING_JS))});"
            f"console.log(JSON.stringify({expression}));"
        )
        result = subprocess.run(
            ["node", "-e", script],
            check=True,
            capture_output=True,
            text=True,
        )
        return json.loads(result.stdout)

    def test_markdown_renders_tables_and_inline_formatting(self):
        source = (
            "| Layer | Cloud |\n"
            "|---|---:|\n"
            "| **Before** | `0%` |\n"
            "| **After** | 24% |"
        )

        html = self.run_js(f"rendering.renderMarkdown({json.dumps(source)})")

        self.assertIn("<table>", html)
        self.assertIn("<strong>Before</strong>", html)
        self.assertIn("<code>0%</code>", html)
        self.assertIn('align="right"', html)

    def test_markdown_raw_html_is_escaped_before_inserting_into_dom(self):
        html = self.run_js(
            f"rendering.renderMarkdown({json.dumps('<img src=x onerror=alert(1)>')})"
        )

        self.assertNotIn("<img", html)
        self.assertIn("&lt;img", html)

    def test_completed_final_text_and_trace_usage_have_single_renderers(self):
        groups = [
            {"type": "tool", "call": {"name": "update_task_status"}},
            {"type": "text", "content": "Done."},
            {"type": "usage", "usage": {"total_tokens": 10}},
        ]

        visible = self.run_js(
            f"rendering.visibleTraceGroups({json.dumps(groups)}, 'done', true)"
        )

        self.assertEqual(visible, [groups[0]])

    def test_running_text_remains_visible_but_usage_does_not(self):
        groups = [
            {"type": "text", "content": "Working"},
            {"type": "usage", "usage": {"total_tokens": 10}},
        ]

        visible = self.run_js(
            f"rendering.visibleTraceGroups({json.dumps(groups)}, 'running', false)"
        )

        self.assertEqual(visible, [groups[0]])


if __name__ == "__main__":
    unittest.main()
