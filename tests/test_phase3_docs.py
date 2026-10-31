"""M16: the Phase 3 decision record and the §1.3 document revisions exist.

REQUIREMENTS-PHASE3 §8 item 13 makes this part of "done": D13–D17 must be
recorded with evidence, and the two conflicts with Phase 2 must be revised in
the documents they belong to -- not silently bypassed.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_decisions_has_d13_to_d17_with_the_required_fields():
    text = (ROOT / "DECISIONS.md").read_text(encoding="utf-8")
    for decision in ("D13", "D14", "D15", "D16", "D17"):
        assert f"### {decision} ·" in text, f"{decision} is missing from DECISIONS.md"
    for field in ("**候选方案**", "**选择**", "**依据与证据**", "**可逆性**", "**日期**"):
        assert field in text, f"decision format field {field} is missing"


def test_phase3_section_1_3_revisions_are_applied():
    phase2 = (ROOT / "REQUIREMENTS-PHASE2.md").read_text(encoding="utf-8")
    # Revision one: NF-13 points at the Phase 3 NF-15 bound.
    assert "REQUIREMENTS-PHASE3.md" in phase2
    assert "NF-15" in phase2
    # Revision two: the F10 form points at Phase 3.
    f10 = phase2.split("### F10", 1)[1]
    assert "Phase 3" in f10.split("### F11", 1)[0]


def test_the_frame_pages_versus_player_division_is_recorded():
    text = (ROOT / "DECISIONS.md").read_text(encoding="utf-8")
    assert "逐帧页" in text and "播放器页" in text
    section = text.split("Phase 3 · 与 Phase 2 的两处冲突修订", 1)
    assert len(section) == 2, "the Phase 3 §1.3 revision record is missing"
    body = section[1]
    assert "全部保留" in body
    assert "新增" in body
