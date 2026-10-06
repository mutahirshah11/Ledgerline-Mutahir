"""F-03.R2 / F-03.R3: the two read prompts (contracts/prompt-rules.md) and `load_prompt`."""

import re
from pathlib import Path

import pytest

from app.domain.enums import ReadVariant
from app.pipeline.extract import load_prompt

PROMPTS_DIR = Path(__file__).resolve().parents[1] / "app" / "pipeline" / "prompts"
PROMPT_A = PROMPTS_DIR / "read_a_v1.md"
PROMPT_B = PROMPTS_DIR / "read_b_v1.md"

SHARED_RULES = [f"PR-{n:02d}" for n in range(1, 15)]

# contracts/prompt-rules.md "Forbidden content": no ERP/sheet values, supplier codes, canonical
# units, expected pack answers, and no instruction to fix, normalise or interpret values.
FORBIDDEN_PATTERNS = [
    r"S-10",
    r"\bPCS\b",
    r" M ",
    r"\b250\b",
    r"\b4428\b",
    r"\b4419\b",
    r"(?i)normali[sz]",
    r"(?i)\bcorrect the\b",
    r"(?i)\bfix",
    r"(?i)\binterpret",
]


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


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


def test_F03_R2_load_prompt_by_version() -> None:
    assert load_prompt(ReadVariant.A, "v1") == _text(PROMPT_A)
    assert load_prompt(ReadVariant.B, "v1") == _text(PROMPT_B)
    assert load_prompt(ReadVariant.A, "v1") != load_prompt(ReadVariant.B, "v1")


def test_F03_R2_missing_prompt_version_errors() -> None:
    with pytest.raises(FileNotFoundError):
        load_prompt(ReadVariant.A, "v999")
    with pytest.raises(FileNotFoundError):
        load_prompt(ReadVariant.B, "v999")
