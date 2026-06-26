import unittest

from hermes_bridge.templates import render_template_text

class TemplateTests(unittest.TestCase):
    def test_render(self):
        self.assertEqual(render_template_text("Hello {{ name }}", {"name": "Ops"}), "Hello Ops")

if __name__ == "__main__":
    unittest.main()
