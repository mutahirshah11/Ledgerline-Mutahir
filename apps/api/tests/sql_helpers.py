"""Small static parsers for `supabase/migrations/0001_init.sql` (no database needed).

The migration is checked offline (contracts/migration-0001.md, Verification). These helpers work
on SQL text with comments removed; they are deliberately simple and only need to understand the
style used in our own migration.
"""

from __future__ import annotations

import re
from functools import cache
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
MIGRATION_PATH = REPO_ROOT / "supabase" / "migrations" / "0001_init.sql"


@cache
def raw_sql() -> str:
    """The migration text exactly as written (raises FileNotFoundError if absent)."""
    return MIGRATION_PATH.read_text(encoding="utf-8")


def strip_comments(sql: str) -> str:
    """Remove `-- line` and `/* block */` comments, leaving string literals intact."""
    out: list[str] = []
    i, n = 0, len(sql)
    in_str = False
    while i < n:
        ch = sql[i]
        if in_str:
            out.append(ch)
            if ch == "'":
                in_str = False
            i += 1
        elif ch == "'":
            in_str = True
            out.append(ch)
            i += 1
        elif sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j == -1 else j
        elif sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            i = n if j == -1 else j + 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


@cache
def sql() -> str:
    """Migration text, comments removed, lower-cased, whitespace collapsed to single spaces."""
    return re.sub(r"\s+", " ", strip_comments(raw_sql())).lower()


def _balanced(text: str, open_at: int) -> str:
    """Contents between the parenthesis at `open_at` and its matching close."""
    assert text[open_at] == "("
    depth = 0
    in_str = False
    for i in range(open_at, len(text)):
        ch = text[i]
        if in_str:
            in_str = ch != "'"
            continue
        if ch == "'":
            in_str = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return text[open_at + 1 : i]
    raise ValueError("unbalanced parentheses")


def split_top_level(body: str) -> list[str]:
    """Split on commas that are not inside parentheses or string literals."""
    parts: list[str] = []
    depth = 0
    in_str = False
    start = 0
    for i, ch in enumerate(body):
        if in_str:
            in_str = ch != "'"
        elif ch == "'":
            in_str = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(body[start:i].strip())
            start = i + 1
    tail = body[start:].strip()
    if tail:
        parts.append(tail)
    return parts


_NAME = r"(?:public\.)?\"?(\w+)\"?"


def enum_types() -> dict[str, list[str]]:
    """`CREATE TYPE name AS ENUM ('A', 'B')` → {name: [A, B]}; labels keep their case."""
    text = re.sub(r"\s+", " ", strip_comments(raw_sql()))
    found: dict[str, list[str]] = {}
    for m in re.finditer(rf"(?i)create type {_NAME} as enum ?\(", text):
        body = _balanced(text, m.end() - 1)
        found[m.group(1).lower()] = re.findall(r"'([^']*)'", body)
    return found


def table_bodies() -> dict[str, str]:
    """`CREATE TABLE name ( ... )` → {name: body} (lower-cased, comments removed)."""
    text = sql()
    found: dict[str, str] = {}
    for m in re.finditer(rf"create table (?:if not exists )?{_NAME} ?\(", text):
        found[m.group(1)] = _balanced(text, m.end() - 1)
    return found


_CONSTRAINT_START = re.compile(r"(constraint|primary key|unique|check|foreign key|exclude)\b")


def table_columns(body: str) -> dict[str, str]:
    """Column definitions of a table body → {column: rest of definition}."""
    cols: dict[str, str] = {}
    for part in split_top_level(body):
        if _CONSTRAINT_START.match(part):
            continue
        name, _, rest = part.partition(" ")
        cols[name.strip('"')] = rest.strip()
    return cols


def table_constraints(body: str) -> list[str]:
    """Table-level constraint clauses of a table body."""
    return [p for p in split_top_level(body) if _CONSTRAINT_START.match(p)]


def function_body(name: str) -> str:
    """Text of `CREATE [OR REPLACE] FUNCTION name(...) ... AS $tag$ body $tag$` (lower-cased)."""
    text = sql()
    for m in re.finditer(rf"create (?:or replace )?function {_NAME} ?\(", text):
        if m.group(1) != name:
            continue
        tag = re.compile(r"as (\$\w*\$)").search(text, m.end())
        if tag is None:
            break
        body_end = text.find(tag.group(1), tag.end())
        return text[m.start() : body_end + len(tag.group(1))]
    raise LookupError(f"function {name} not found")
