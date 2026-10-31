"""F14: the overview page must sum up the artefacts, not re-assert them."""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "replay"
DASHBOARD = SITE / "dashboard.html"


class Nodes(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.elements: list[tuple[str, dict[str, str]]] = []
        self.captions: list[str] = []
        self._in_caption = False
        self._caption: list[str] = []

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, {key: (value or "") for key, value in attrs}))
        if tag == "figcaption":
            self._in_caption, self._caption = True, []

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_data(self, data):
        if self._in_caption:
            self._caption.append(data)

    def handle_endtag(self, tag):
        if tag == "figcaption":
            self._in_caption = False
            self.captions.append("".join(self._caption).strip())

    def by_class(self, name: str) -> list[dict[str, str]]:
        return [attrs for _, attrs in self.elements if name in attrs.get("class", "").split()]

    def tags(self, name: str) -> list[dict[str, str]]:
        return [attrs for tag, attrs in self.elements if tag == name]


def _parse() -> Nodes:
    page = Nodes()
    page.feed(DASHBOARD.read_text(encoding="utf-8"))
    page.close()
    return page


def _key_numbers() -> list[dict]:
    return json.loads((ROOT / "outputs" / "key_numbers.json").read_text(encoding="utf-8"))


pytestmark = pytest.mark.skipif(not DASHBOARD.exists(), reason="dashboard not built; run `bike-agent repro` first")


# -- F14-1 ------------------------------------------------------------------
def test_every_card_is_a_key_number_and_there_are_enough():
    page = _parse()
    cards = [attrs for attrs in page.tags("article") if "num-card" in attrs.get("class", "").split()]
    assert len(cards) >= 12

    entries = _key_numbers()
    by_metric = {str(entry["metric"]): entry for entry in entries}
    numeric = [attrs for attrs in page.by_class("metric") if attrs.get("data-value")]
    assert numeric
    for attrs in numeric:
        metric = attrs["data-metric"]
        assert metric in by_metric, f"{metric} is not a key number"
        assert float(attrs["data-value"]) == float(by_metric[metric]["value"])
        assert attrs["data-source"] == by_metric[metric]["source_file"]


# -- F14-2 ------------------------------------------------------------------
def test_every_figure_is_referenced_with_a_caption_and_a_source():
    page = _parse()
    figures = sorted((ROOT / "figures").glob("*.png"))
    assert figures
    images = page.tags("img")
    referenced = {Path(attrs["src"]).name for attrs in images}
    for figure in figures:
        assert figure.name in referenced, f"{figure.name} is not shown on the dashboard"
        assert (ROOT / "figures" / figure.name).exists()

    for attrs in images:
        assert (DASHBOARD.parent / attrs["src"]).resolve().exists(), attrs["src"]

    figure_nodes = page.tags("figure")
    assert len(figure_nodes) == len(figures)
    for attrs in figure_nodes:
        source = attrs.get("data-source", "")
        assert source, "figure without data-source"
        assert (ROOT / source).exists(), source
    assert len(page.captions) == len(figures)
    assert all(caption for caption in page.captions)


# -- F14-3 ------------------------------------------------------------------
def test_cross_source_table_matches_the_artifacts():
    page = _parse()
    entries = _key_numbers()
    by_metric = {str(entry["metric"]): entry for entry in entries}
    cells = page.by_class("metric")
    table_cells = [attrs for attrs in cells if attrs.get("data-metric", "").startswith(("XM · ", "SH · "))]
    assert table_cells
    for attrs in table_cells:
        entry = by_metric[attrs["data-metric"]]
        assert float(attrs["data-value"]) == float(entry["value"])
        assert attrs["data-source"] == entry["source_file"]
        assert (ROOT / entry["source_file"]).exists()


# -- F14-4 ------------------------------------------------------------------
def test_report_embeds_figures_and_links_the_dashboard():
    text = (ROOT / "REPORT.md").read_text(encoding="utf-8")
    references = re.findall(r"!\[[^\]]*\]\((figures/[^)]+)\)", text)
    assert len(references) >= 6
    for reference in references:
        assert (ROOT / reference).exists(), reference
    assert "replay/dashboard.html" in text


# -- F14-5 ------------------------------------------------------------------
_EXTERNAL = re.compile(
    r"""(?:src|href)\s*=\s*["'](?:(?:https?:)?//)[^"']+["']|@import\s+(?:url\()?["']?(?:https?:)?//|url\(\s*["']?(?:https?:)?//""",
    re.IGNORECASE,
)


def test_dashboard_is_offline():
    text = DASHBOARD.read_text(encoding="utf-8")
    assert not _EXTERNAL.search(text)


# -- F14-6 ------------------------------------------------------------------
def test_dashboard_is_honest_about_simulation():
    text = DASHBOARD.read_text(encoding="utf-8")
    assert "模拟值" in text
    for forbidden in ("真实运营", "实际节省"):
        assert forbidden not in text
