"""Tests for the Phase C issue body parser (C2).

The parser is the one place humans supply arbitrary input that becomes
a job record, so every validation branch is exercised explicitly. The
round-trip test proves the parser's output is directly usable as
`_make_job_record` kwargs — the design doc's "done means" criterion.
"""

from __future__ import annotations

from textwrap import dedent

import pytest

import issue_body
import server
from issue_body import InvalidIssue, parse_issue_body

# ---------- happy paths -------------------------------------------------


def test_parse_minimal_body():
    body = dedent(
        """
        ---
        scope:
          - path/to/file.py
        ---

        Rewrite everything to be in rhyming couplets.
        """
    ).lstrip()

    out = parse_issue_body(body)

    assert out["scope"] == ["path/to/file.py"]
    assert out["spec"] == "Rewrite everything to be in rhyming couplets."
    # Optional fields are omitted when absent — poller/_make_job_record pick their defaults
    assert set(out) == {"scope", "spec"}


def test_parse_full_body():
    body = dedent(
        """
        ---
        scope:
          - a.py
          - b.py
        base_branch: develop
        test_cmd:
          - pytest
          - -x
        skip_tests: false
        title: "chore: do the thing"
        body: "Reviewer-facing context goes here."
        group_id: fe-touch-up
        ---

        Add return type annotations.
        """
    ).lstrip()

    out = parse_issue_body(body)

    assert out == {
        "scope": ["a.py", "b.py"],
        "base_branch": "develop",
        "test_cmd": ["pytest", "-x"],
        "skip_tests": False,
        "title": "chore: do the thing",
        "body": "Reviewer-facing context goes here.",
        "group_id": "fe-touch-up",
        "spec": "Add return type annotations.",
    }


def test_parse_null_optionals_normalize_to_none():
    """`group_id: null` is how humans spell "omit" in the design doc's example."""
    body = dedent(
        """
        ---
        scope:
          - x.py
        group_id: null
        title: null
        body: null
        test_cmd: null
        ---

        Do a thing.
        """
    ).lstrip()

    out = parse_issue_body(body)

    assert out["group_id"] is None
    assert out["title"] is None
    assert out["body"] is None
    assert out["test_cmd"] is None


def test_parse_multiline_spec_is_dedented_and_stripped():
    body = dedent(
        """
        ---
        scope: [a.py]
        ---

            Line one with extra indent.
            Line two shares the indent.

            Trailing paragraph.

        """
    ).lstrip()

    out = parse_issue_body(body)

    assert out["spec"] == (
        "Line one with extra indent.\nLine two shares the indent.\n\nTrailing paragraph."
    )


def test_parse_tolerates_crlf_line_endings():
    """GitHub normalizes to LF but a user's clipboard sometimes doesn't."""
    body = "---\r\nscope:\r\n  - a.py\r\n---\r\n\r\nDo the thing.\r\n"
    out = parse_issue_body(body)
    assert out["scope"] == ["a.py"]
    assert out["spec"] == "Do the thing."


def test_parse_tolerates_blank_lines_before_opening_fence():
    """A rendered issue body sometimes gets leading whitespace from the editor."""
    body = "\n\n---\nscope: [a.py]\n---\n\nDo it.\n"
    out = parse_issue_body(body)
    assert out["scope"] == ["a.py"]


# ---------- round-trip through `_make_job_record` -----------------------


def test_round_trip_through_make_job_record(tmp_path):
    """Parser output + `repo_root` → `_make_job_record` with no manual plumbing."""
    body = dedent(
        """
        ---
        scope:
          - a.py
        title: "chore: trip"
        body: "longer body"
        group_id: round-trip
        skip_tests: true
        ---

        Do the thing.
        """
    ).lstrip()
    parsed = parse_issue_body(body)

    record = server._make_job_record(**parsed, repo_root=str(tmp_path))

    assert record["spec"] == "Do the thing."
    assert record["scope"] == ["a.py"]
    assert record["title"] == "chore: trip"
    assert record["body"] == "longer body"
    assert record["group_id"] == "round-trip"
    assert record["skip_tests"] is True
    # Defaulted by `_make_job_record`:
    assert record["base_branch"] == "main"
    assert record["gate_cmds"] == server.DEFAULT_GATE_CMDS


def test_round_trip_group_id_pattern_matches_server():
    """Belt-and-braces: parser and server share the exact same regex string."""
    assert issue_body._GROUP_ID_PATTERN.pattern == server._GROUP_ID_PATTERN.pattern


# ---------- frontmatter structure errors --------------------------------


def test_body_without_frontmatter_rejected():
    with pytest.raises(InvalidIssue, match="must start with YAML frontmatter"):
        parse_issue_body("just prose, no fences in sight")


def test_body_without_closing_fence_rejected():
    body = "---\nscope: [a.py]\n\nNo closing fence here."
    with pytest.raises(InvalidIssue, match="frontmatter is not closed"):
        parse_issue_body(body)


def test_body_with_text_before_opening_fence_rejected():
    body = "hey\n---\nscope: [a.py]\n---\n\nDo it."
    with pytest.raises(InvalidIssue, match="must start with YAML frontmatter"):
        parse_issue_body(body)


def test_body_with_bad_yaml_rejected():
    body = "---\nnot: valid: yaml: here\n---\n\nspec"
    with pytest.raises(InvalidIssue, match="not valid YAML"):
        parse_issue_body(body)


def test_body_with_empty_frontmatter_rejected():
    body = "---\n---\n\nspec here"
    with pytest.raises(InvalidIssue, match="frontmatter is empty"):
        parse_issue_body(body)


def test_body_with_non_mapping_frontmatter_rejected():
    """`scope: a.py` without a list wrapping — a common hand-edit mistake."""
    body = "---\n- just\n- a\n- list\n---\n\nspec"
    with pytest.raises(InvalidIssue, match="frontmatter must be a mapping"):
        parse_issue_body(body)


def test_body_with_unknown_frontmatter_key_rejected():
    body = dedent(
        """
        ---
        scope: [a.py]
        priority: high
        ---

        spec
        """
    ).lstrip()
    with pytest.raises(InvalidIssue, match="unknown frontmatter key"):
        parse_issue_body(body)


def test_body_with_empty_spec_rejected():
    body = "---\nscope: [a.py]\n---\n   \n\n"
    with pytest.raises(InvalidIssue, match="spec .* is empty"):
        parse_issue_body(body)


# ---------- scope validation --------------------------------------------


def test_scope_missing_rejected():
    body = "---\ntitle: hi\n---\n\nspec"
    with pytest.raises(InvalidIssue, match="`scope` is required"):
        parse_issue_body(body)


def test_scope_not_a_list_rejected():
    body = '---\nscope: "a.py"\n---\n\nspec'
    with pytest.raises(InvalidIssue, match="`scope` must be a non-empty list"):
        parse_issue_body(body)


def test_scope_empty_list_rejected():
    body = "---\nscope: []\n---\n\nspec"
    with pytest.raises(InvalidIssue, match=r"`scope` must be a non-empty list"):
        parse_issue_body(body)


def test_scope_with_non_string_item_rejected():
    body = "---\nscope: [a.py, 42]\n---\n\nspec"
    with pytest.raises(InvalidIssue, match=r"`scope\[1\]` must be a string"):
        parse_issue_body(body)


def test_scope_with_blank_string_rejected():
    body = '---\nscope: ["a.py", "   "]\n---\n\nspec'
    with pytest.raises(InvalidIssue, match=r"`scope\[1\]` must be non-empty"):
        parse_issue_body(body)


# ---------- base_branch validation --------------------------------------


def test_base_branch_wrong_type_rejected():
    body = "---\nscope: [a.py]\nbase_branch: 7\n---\n\nspec"
    with pytest.raises(InvalidIssue, match="`base_branch` must be a non-empty string"):
        parse_issue_body(body)


def test_base_branch_empty_string_rejected():
    body = '---\nscope: [a.py]\nbase_branch: ""\n---\n\nspec'
    with pytest.raises(InvalidIssue, match="`base_branch` must be a non-empty string"):
        parse_issue_body(body)


# ---------- test_cmd validation -----------------------------------------


def test_test_cmd_wrong_type_rejected():
    body = '---\nscope: [a.py]\ntest_cmd: "pytest -x"\n---\n\nspec'
    with pytest.raises(InvalidIssue, match="`test_cmd` must be null or a non-empty list"):
        parse_issue_body(body)


def test_test_cmd_empty_list_rejected():
    body = "---\nscope: [a.py]\ntest_cmd: []\n---\n\nspec"
    with pytest.raises(InvalidIssue, match="`test_cmd` must be null or a non-empty list"):
        parse_issue_body(body)


def test_test_cmd_with_non_string_item_rejected():
    body = "---\nscope: [a.py]\ntest_cmd: [pytest, 42]\n---\n\nspec"
    with pytest.raises(InvalidIssue, match=r"`test_cmd\[1\]` must be a string"):
        parse_issue_body(body)


# ---------- gate_cmds validation ----------------------------------------


def test_gate_cmds_happy_path():
    body = dedent(
        """
        ---
        scope: [a.py]
        gate_cmds:
          - [npm, run, lint:fix]
          - [npm, run, build]
        ---

        spec
        """
    ).lstrip()
    out = parse_issue_body(body)
    assert out["gate_cmds"] == [["npm", "run", "lint:fix"], ["npm", "run", "build"]]


def test_gate_cmds_null_allowed():
    body = "---\nscope: [a.py]\ngate_cmds: null\n---\n\nspec"
    assert parse_issue_body(body)["gate_cmds"] is None


def test_gate_cmds_wrong_type_rejected():
    body = '---\nscope: [a.py]\ngate_cmds: "pytest"\n---\n\nspec'
    with pytest.raises(InvalidIssue, match="`gate_cmds` must be a non-empty list of commands"):
        parse_issue_body(body)


def test_gate_cmds_empty_list_rejected():
    body = "---\nscope: [a.py]\ngate_cmds: []\n---\n\nspec"
    with pytest.raises(InvalidIssue, match="`gate_cmds` must be a non-empty list of commands"):
        parse_issue_body(body)


def test_gate_cmds_inner_not_a_list_rejected():
    body = "---\nscope: [a.py]\ngate_cmds: [pytest]\n---\n\nspec"
    with pytest.raises(InvalidIssue, match=r"`gate_cmds\[0\]` must be a non-empty list"):
        parse_issue_body(body)


def test_gate_cmds_inner_non_string_rejected():
    body = "---\nscope: [a.py]\ngate_cmds: [[pytest, 42]]\n---\n\nspec"
    with pytest.raises(InvalidIssue, match=r"`gate_cmds\[0\]\[1\]` must be a string"):
        parse_issue_body(body)


def test_test_cmd_and_gate_cmds_together_rejected():
    body = dedent(
        """
        ---
        scope: [a.py]
        test_cmd: [pytest]
        gate_cmds:
          - [pytest]
        ---

        spec
        """
    ).lstrip()
    with pytest.raises(InvalidIssue, match="either `test_cmd` or `gate_cmds`"):
        parse_issue_body(body)


# ---------- skip_tests validation ---------------------------------------


def test_skip_tests_wrong_type_rejected():
    body = '---\nscope: [a.py]\nskip_tests: "yes"\n---\n\nspec'
    with pytest.raises(InvalidIssue, match="`skip_tests` must be a boolean"):
        parse_issue_body(body)


def test_skip_tests_integer_not_coerced():
    """`1` is NOT a boolean here — surprise coercion would mask typos."""
    body = "---\nscope: [a.py]\nskip_tests: 1\n---\n\nspec"
    with pytest.raises(InvalidIssue, match="`skip_tests` must be a boolean"):
        parse_issue_body(body)


# ---------- title / body validation -------------------------------------


def test_title_wrong_type_rejected():
    body = "---\nscope: [a.py]\ntitle: [not, a, string]\n---\n\nspec"
    with pytest.raises(InvalidIssue, match="`title` must be a string"):
        parse_issue_body(body)


def test_body_wrong_type_rejected():
    body = "---\nscope: [a.py]\nbody: 42\n---\n\nspec"
    with pytest.raises(InvalidIssue, match="`body` must be a string"):
        parse_issue_body(body)


# ---------- group_id validation -----------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "UPPERCASE",
        "has spaces",
        "has/slash",
        "-leading-hyphen",
        "has_underscore",
        "a" * 64,  # too long
    ],
)
def test_group_id_malformed_rejected(bad):
    body = f'---\nscope: [a.py]\ngroup_id: "{bad}"\n---\n\nspec'
    with pytest.raises(InvalidIssue, match="`group_id` must match"):
        parse_issue_body(body)


def test_group_id_wrong_type_rejected():
    body = "---\nscope: [a.py]\ngroup_id: 42\n---\n\nspec"
    with pytest.raises(InvalidIssue, match="`group_id` must be a string"):
        parse_issue_body(body)


def test_group_id_63_chars_accepted():
    """Boundary: exactly 63 chars is allowed."""
    gid = "a" + "b" * 62
    body = f'---\nscope: [a.py]\ngroup_id: "{gid}"\n---\n\nspec'
    assert parse_issue_body(body)["group_id"] == gid
