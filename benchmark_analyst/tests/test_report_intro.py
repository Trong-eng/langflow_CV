import unittest
from html.parser import HTMLParser
from pathlib import Path

REPORT = Path(__file__).parents[1] / "langflow_cache_production_report.html"


class ReportParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.section_ids = []
        self._section_depth = 0
        self._current_id = None
        self._chunks = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "section":
            self._section_depth += 1
            section_id = attrs.get("id")
            if section_id:
                self.section_ids.append(section_id)
                self._current_id = section_id
                self._chunks.setdefault(section_id, [])

    def handle_endtag(self, tag):
        if tag == "section":
            self._section_depth -= 1
            if self._section_depth == 0:
                self._current_id = None

    def handle_data(self, data):
        if self._current_id:
            self._chunks[self._current_id].append(data)

    def text(self, section_id):
        return " ".join(" ".join(self._chunks[section_id]).split())


class ReportIntroductionTest(unittest.TestCase):
    def test_baseline_comparison_precedes_executive_conclusion_and_explains_scope(self):
        parser = ReportParser()
        parser.feed(REPORT.read_text())

        self.assertEqual(parser.section_ids[1], "baseline-vs-version-a")
        intro = parser.text("baseline-vs-version-a")
        for required in (
            "AST — cây cú pháp trừu tượng",
            "compiled code object",
            "không đồng nghĩa với dùng lại kết quả chạy component",
            "Source không đổi và cache còn hợp lệ",
            "worker khởi động lại",
            "quyền, chính sách hoặc môi trường thực thi",
            "cùng source component được sử dụng nhiều lần",
        ):
            self.assertIn(required, intro)


if __name__ == "__main__":
    unittest.main()
