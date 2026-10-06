"""F-03.R2 / F-03.R3: the two read prompts (contracts/prompt-rules.md) and `load_prompt`."""

import json
import re
from pathlib import Path

import pytest

from app.domain.enums import ReadVariant
from app.pipeline.extract import load_prompt

PROMPTS_DIR = Path(__file__).resolve().parents[1] / "app" / "pipeline" / "prompts"
PROMPT_A = PROMPTS_DIR / "read_a_v1.md"
PROMPT_B = PROMPTS_DIR / "read_b_v1.md"
READ_SCHEMA = PROMPTS_DIR / "read_schema_v1.json"

SHARED_RULES = [f"PR-{n:02d}" for n in range(1, 15)]

# contracts/prompt-rules.md "Forbidden content": no ERP/sheet values, supplier codes, canonical
# units, expected pack answers, and no instruction to fix, normalise or interpret values.
FORBIDDEN_PATTERNS = [
    r"S-1\d\d",
    r"\bPCS\b",
    r"(?<![A-Za-z])M(?![A-Za-z])",
    r"\b250\b",
    r"\b4428\b",
    r"\b4419\b",
    r"\b4425\b",
    r"\b1097\b",
    r"L2609",
    r"Taraz",
    r"\bbdl\b",
    r"(?i)normali[sz]",
    r"(?i)\bcorrect the\b",
    r"(?i)\bfix",
    r"(?i)\binterpret",
]


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _flat(text: str) -> str:
    """Collapse line wraps and indentation so phrases match across wrapped lines."""
    return re.sub(r"\s+", " ", text)


def _cell_keys() -> list[str]:
    schema = json.loads(_text(READ_SCHEMA))
    return schema["properties"]["rows"]["items"]["properties"]["cells"]["required"]


# Each rule's text (from its tag up to the next tag) must keep these phrases, so a rule cannot
# silently lose its key instruction while the tag survives.
RULE_PHRASES: dict[str, list[str]] = {
    "PR-05": ["null", "CLEAR"],
    "PR-06": ["CORRECTED", "not certain", "no replacement", "PR-07", "PR-08", "struck through"],
    "PR-07": ["UNCLEAR", "at least one candidate", "ILLEGIBLE", "never guess"],
    "PR-08": ["ILLEGIBLE", "NO candidates", "note"],
    "PR-10": ["month_written", "page_no_written", "null", "certainty", "never guess"],
    "PR-11": ["blank_row_slots", "must equal 16"],
    "PR-12": ["table_top", "table_bottom", "null", "not visible"],
    "PR-13": _cell_keys(),
}


def _rule_text(text: str, rule: str) -> str:
    """Text following `[rule]` up to the next `[PR-..]` tag (or the end)."""
    start = text.index(f"[{rule}]") + len(rule) + 2
    nxt = re.search(r"\[PR-[0-9A-Z]+\]", text[start:])
    return text[start : start + nxt.start()] if nxt else text[start:]


def _missing_phrases(text: str) -> list[str]:
    return [
        f"{rule}: {phrase!r}"
        for rule, phrases in RULE_PHRASES.items()
        for phrase in phrases
        if phrase not in _flat(_rule_text(text, rule))
    ]


def _without_reading_order(text: str) -> str:
    """Drop the title line and the `## Reading order` section (the only A/B difference)."""
    body = text.split("\n", 1)[1]
    return re.sub(r"## Reading order\n.*?(?=\n## )", "", body, flags=re.S)


def test_F03_R3_prompts_contain_all_rules() -> None:
    a, b = _text(PROMPT_A), _text(PROMPT_B)
    for rule in SHARED_RULES:
        assert f"[{rule}]" in a, f"{rule} missing from read_a_v1.md"
        assert f"[{rule}]" in b, f"{rule} missing from read_b_v1.md"
    assert "[PR-A1]" in a
    assert "[PR-A1]" not in b
    assert "[PR-B1]" in b
    assert "[PR-B1]" not in a


@pytest.mark.parametrize("path", [PROMPT_A, PROMPT_B], ids=["A", "B"])
def test_F03_R3_prompts_forbidden_content(path: Path) -> None:
    text = _text(path)
    for pattern in FORBIDDEN_PATTERNS:
        match = re.search(pattern, text)
        assert match is None, f"{path.name} contains forbidden {match.group(0)!r}"


@pytest.mark.parametrize("path", [PROMPT_A, PROMPT_B], ids=["A", "B"])
def test_F03_R3_rule_key_phrases(path: Path) -> None:
    assert _missing_phrases(_text(path)) == []


@pytest.mark.parametrize("path", [PROMPT_A, PROMPT_B], ids=["A", "B"])
def test_F03_R3_clear_bullet_points_to_blank_rule(path: Path) -> None:
    assert "(or null for a blank cell, PR-05)" in _flat(_text(path))


def test_F03_R3_rule_phrase_check_catches_regression() -> None:
    text = _text(PROMPT_A)
    mutated = text.replace("never guess", "pick the likeliest")
    assert any("never guess" in m for m in _missing_phrases(mutated))


def test_F03_R3_prompts_identical_apart_from_reading_order() -> None:
    a, b = _text(PROMPT_A), _text(PROMPT_B)
    assert "## Reading order" in a and "## Reading order" in b
    assert _without_reading_order(a) == _without_reading_order(b)


def test_F03_R2_load_prompt_by_version() -> None:
    assert load_prompt(ReadVariant.A, "v1") == _text(PROMPT_A)
    assert load_prompt(ReadVariant.B, "v1") == _text(PROMPT_B)
    assert load_prompt(ReadVariant.A, "v1") != load_prompt(ReadVariant.B, "v1")


def test_F03_R2_missing_prompt_version_errors() -> None:
    with pytest.raises(FileNotFoundError):
        load_prompt(ReadVariant.A, "v999")
    with pytest.raises(FileNotFoundError):
        load_prompt(ReadVariant.B, "v999")
