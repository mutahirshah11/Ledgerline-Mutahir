"""F-03.R2 / F-03.R3: the two read prompts (contracts/prompt-rules.md) and `load_prompt`.

Every shipped prompt version is checked (FR-036: improved prompts get a new version; none is
deleted).
"""

import json
import re
from pathlib import Path
from typing import Any

import pytest

from app.core.config import Settings
from app.domain.enums import ReadVariant
from app.pipeline.extract import load_prompt, load_read_schema

PROMPTS_DIR = Path(__file__).resolve().parents[1] / "app" / "pipeline" / "prompts"
PROMPT_A = PROMPTS_DIR / "read_a_v1.md"
PROMPT_B = PROMPTS_DIR / "read_b_v1.md"
READ_SCHEMA = PROMPTS_DIR / "read_schema_v1.json"

# FR-036: every prompt version ever shipped stays on disk; v2 and v3 strengthen verbatim copying
# (T034).
VERSIONS = ["v1", "v2", "v3"]


def _prompt(variant: str, version: str) -> Path:
    return PROMPTS_DIR / f"read_{variant}_{version}.md"


PROMPT_PAIRS = [(_prompt("a", v), _prompt("b", v)) for v in VERSIONS]
PROMPT_FILES = [path for pair in PROMPT_PAIRS for path in pair]
FILE_IDS = [f"{v}-{x}" for v in VERSIONS for x in ("A", "B")]

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
    schema: dict[str, Any] = json.loads(_text(READ_SCHEMA))
    keys: list[str] = schema["properties"]["rows"]["items"]["properties"]["cells"]["required"]
    return keys


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

# From v2 on (T034, FR-036), PR-02 states the stricter verbatim rule: copy each character's shape,
# never swap letters and digits, never substitute a likelier word, and use UNCLEAR when a
# character could be read two ways.
VERBATIM_PHRASES = [
    "shape",
    "letters into digits",
    "digits into letters",
    "more familiar",
    "could be read two ways",
    "UNCLEAR",
    "both readings",
]
# From v3 on, PR-02 also says familiar spellings are no evidence and asks for a character check.
VERBATIM_PHRASES_V3 = [
    "never evidence",
    "check each character again",
]


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


def test_F03_R2_prompt_versions_on_disk() -> None:
    on_disk = sorted(p.stem.removeprefix("read_a_") for p in PROMPTS_DIR.glob("read_a_v*.md"))
    assert on_disk == VERSIONS
    assert Settings.model_fields["PROMPT_VERSION"].default == VERSIONS[-1]


@pytest.mark.parametrize("version", VERSIONS)
def test_F03_R4_every_prompt_version_ships_the_read_schema(version: str) -> None:
    assert load_read_schema(version) == load_read_schema("v1")


@pytest.mark.parametrize(("path_a", "path_b"), PROMPT_PAIRS, ids=VERSIONS)
def test_F03_R3_prompts_contain_all_rules(path_a: Path, path_b: Path) -> None:
    a, b = _text(path_a), _text(path_b)
    for rule in SHARED_RULES:
        assert f"[{rule}]" in a, f"{rule} missing from {path_a.name}"
        assert f"[{rule}]" in b, f"{rule} missing from {path_b.name}"
    assert "[PR-A1]" in a
    assert "[PR-A1]" not in b
    assert "[PR-B1]" in b
    assert "[PR-B1]" not in a


@pytest.mark.parametrize("path", PROMPT_FILES, ids=FILE_IDS)
def test_F03_R3_prompts_forbidden_content(path: Path) -> None:
    text = _text(path)
    for pattern in FORBIDDEN_PATTERNS:
        match = re.search(pattern, text)
        assert match is None, f"{path.name} contains forbidden {match.group(0)!r}"


@pytest.mark.parametrize("path", PROMPT_FILES, ids=FILE_IDS)
def test_F03_R3_rule_key_phrases(path: Path) -> None:
    assert _missing_phrases(_text(path)) == []


@pytest.mark.parametrize("path", PROMPT_FILES, ids=FILE_IDS)
def test_F03_R3_clear_bullet_points_to_blank_rule(path: Path) -> None:
    assert "(or null for a blank cell, PR-05)" in _flat(_text(path))


def test_F03_R3_rule_phrase_check_catches_regression() -> None:
    text = _text(PROMPT_A)
    mutated = text.replace("never guess", "pick the likeliest")
    assert any("never guess" in m for m in _missing_phrases(mutated))


@pytest.mark.parametrize(
    "path", [_prompt(x, v) for v in VERSIONS[1:] for x in ("a", "b")], ids=FILE_IDS[2:]
)
def test_F03_R3_verbatim_rule_from_v2(path: Path) -> None:
    rule = _flat(_rule_text(_text(path), "PR-02"))
    assert [p for p in VERBATIM_PHRASES if p not in rule] == []


@pytest.mark.parametrize(
    "path", [_prompt(x, v) for v in VERSIONS[2:] for x in ("a", "b")], ids=FILE_IDS[4:]
)
def test_F03_R3_familiar_spelling_rule_from_v3(path: Path) -> None:
    rule = _flat(_rule_text(_text(path), "PR-02"))
    assert [p for p in VERBATIM_PHRASES_V3 if p not in rule] == []


@pytest.mark.parametrize(("path_a", "path_b"), PROMPT_PAIRS, ids=VERSIONS)
def test_F03_R3_prompts_identical_apart_from_reading_order(path_a: Path, path_b: Path) -> None:
    a, b = _text(path_a), _text(path_b)
    assert "## Reading order" in a and "## Reading order" in b
    assert _without_reading_order(a) == _without_reading_order(b)


@pytest.mark.parametrize("version", VERSIONS)
def test_F03_R2_load_prompt_by_version(version: str) -> None:
    assert load_prompt(ReadVariant.A, version) == _text(_prompt("a", version))
    assert load_prompt(ReadVariant.B, version) == _text(_prompt("b", version))
    assert load_prompt(ReadVariant.A, version) != load_prompt(ReadVariant.B, version)


def test_F03_R2_missing_prompt_version_errors() -> None:
    with pytest.raises(FileNotFoundError):
        load_prompt(ReadVariant.A, "v999")
    with pytest.raises(FileNotFoundError):
        load_prompt(ReadVariant.B, "v999")
