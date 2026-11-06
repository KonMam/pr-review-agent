"""Snapshot tests of what `pr-review post` writes on a PR (spec item 8b).

The expected strings are the approved mock, `docs/mock-pr-comments.md`, with
the real go-taskqueue#2 findings. No network: these are pure formatting.
"""

import pytest

from pr_review.github import (
    MARKER,
    MAX_TITLE_CHARS,
    MAX_WHY_CHARS,
    category_label,
    comment_body,
    review_body,
    title_and_why,
)
from pr_review.models import Finding, LineRange
from pr_review.review import Report, ReportFinding

SHA = "b" * 40
CLAIM = (
    "The return value of json.NewEncoder(w).Encode(task) is now discarded. When "
    "encoding fails (e.g. an unencodable field or invalid raw JSON), the handler "
    "silently returns a 200 with a truncated or empty body instead of the previous "
    "500 error, so the client cannot tell the response is broken."
)


def rf(
    claim: str = CLAIM,
    category: str = "ignored_err",
    start: int = 61,
    end: int = 61,
    file: str = "api/server.go",
) -> ReportFinding:
    return ReportFinding(
        finding=Finding(
            file=file,
            lines=LineRange(start=start, end=end),
            category=category,
            claim=claim,
            evidence="json.NewEncoder(w).Encode(task)",
        )
    )


def report(*findings: ReportFinding) -> Report:
    return Report(
        arm="or-deepseek-flash",
        model="deepseek/deepseek-v4.1-flash",
        prompt_version="abc123def456",
        stage_versions={},
        findings=list(findings),
        go_files=1,
        cost_usd=0.0032,
    )


def test_inline_comment_snapshot() -> None:
    assert comment_body(rf()) == (
        "🐛 **Ignored error**\n\n"
        "**The return value of json.NewEncoder(w).Encode(task) is now discarded.**\n\n"
        "When encoding fails (e.g. an unencodable field or invalid raw JSON), the "
        "handler silently returns a 200 with a truncated or empty body instead of the "
        "previous 500 error, so the client cannot tell the response is broken.\n\n"
        f"{MARKER}"
    )


def test_one_sentence_claim_has_no_why() -> None:
    body = comment_body(
        rf("In the empty-result branch the error is no longer checked.")
    )
    assert body == (
        "🐛 **Ignored error**\n\n"
        "**In the empty-result branch the error is no longer checked.**\n\n"
        f"{MARKER}"
    )


def test_summary_snapshot_all_inline() -> None:
    r = report(rf(), rf(start=91, end=91), rf(start=148, end=148))
    body = review_body(
        r,
        3,
        [],
        server_url="https://github.com",
        repo="KonMam/go-taskqueue",
        head_sha=SHA,
    )
    assert body == (
        "**pr-review** found 3 possible bugs, commented inline.\n\n"
        "<sub>deepseek/deepseek-v4.1-flash · $0.0032</sub>\n\n"
        f"{MARKER}"
    )


def test_summary_snapshot_with_a_finding_outside_the_diff() -> None:
    out = rf(
        "`t` can be nil when the lookup misses. `Get` returns `nil, nil` for an "
        "unknown id, and the caller reads `t.Status` without a check.",
        category="nil_deref",
        start=40,
        end=42,
        file="store/store.go",
    )
    body = review_body(
        report(rf(), rf(start=91, end=91), out),
        2,
        [out],
        server_url="https://github.com/",
        repo="KonMam/go-taskqueue",
        head_sha=SHA,
    )
    assert body == (
        "**pr-review** found 3 possible bugs: 2 commented inline, 1 outside the diff."
        "\n\n"
        "🐛 **Nil dereference** · [`store/store.go:40-42`]"
        f"(https://github.com/KonMam/go-taskqueue/blob/{SHA}/store/store.go#L40-L42)"
        "\n\n"
        "**`t` can be nil when the lookup misses.**\n\n"
        "`Get` returns `nil, nil` for an unknown id, and the caller reads `t.Status` "
        "without a check.\n\n"
        "<sub>deepseek/deepseek-v4.1-flash · $0.0032</sub>\n\n"
        f"{MARKER}"
    )


def test_summary_counts_one_bug_outside_the_diff() -> None:
    out = rf(start=5, end=5)
    body = review_body(
        report(out), 0, [out], server_url="https://github.com", repo="o/r", head_sha=SHA
    )
    assert body.startswith("**pr-review** found 1 possible bug, outside the diff.\n\n")
    assert f"(https://github.com/o/r/blob/{SHA}/api/server.go#L5)" in body


@pytest.mark.parametrize(
    ("category", "label"),
    [
        ("nil_deref", "Nil dereference"),
        ("ignored_err", "Ignored error"),
        ("goroutine_leak", "Goroutine leak"),
        ("data_race", "Data race"),
        ("defer_in_loop", "Defer in loop"),
        ("slice_aliasing", "Slice aliasing"),
        ("context_propagation", "Context propagation"),
        ("overflow_off_by_one", "Overflow or off-by-one"),
        ("unclosed_resource", "Unclosed resource"),
        ("other_bug", "Other bug"),
        ("Ignored-Err", "Ignored error"),
        ("some_new_thing", "Some new thing"),
        ("  ", "Bug"),
    ],
)
def test_no_snake_case_category_is_shown(category: str, label: str) -> None:
    assert category_label(category) == label
    assert "_" not in comment_body(rf(category=category)).removesuffix(MARKER)


def test_why_is_capped_so_comments_stay_terse() -> None:
    title, why = title_and_why("Short title. " + "Word " * 300)
    assert title == "Short title."
    assert MAX_WHY_CHARS - 10 < len(why) <= MAX_WHY_CHARS
    assert why.endswith("Word…")  # cut at a word boundary


def test_abbreviations_do_not_end_the_title() -> None:
    title, why = title_and_why(
        "It drops errors, e.g. Encode ones. That hides failures."
    )
    assert title == "It drops errors, e.g. Encode ones."
    assert why == "That hides failures."


def test_lowercase_second_sentence_still_splits() -> None:
    title, why = title_and_why(
        "Removing defer f.Close() leaks the file. net.FileListener dups the fd."
    )
    assert title == "Removing defer f.Close() leaks the file."
    assert why == "net.FileListener dups the fd."


def test_word_ending_like_an_abbreviation_still_ends_a_sentence() -> None:
    assert title_and_why("It breaks envs. Then it fails.") == (
        "It breaks envs.",
        "Then it fails.",
    )


def test_long_first_sentence_splits_at_a_semicolon() -> None:
    claim = (
        "The error returned by json.NewEncoder(w).Encode(task) is ignored in the "
        "create handler after the status is written; if encoding fails after headers "
        "are partially written, the failure is silently swallowed."
    )
    title, why = title_and_why(claim)
    assert title == (
        "The error returned by json.NewEncoder(w).Encode(task) is ignored in the "
        "create handler after the status is written."
    )
    assert why == (
        "If encoding fails after headers are partially written, the failure is "
        "silently swallowed."
    )


def test_long_first_sentence_loses_no_text_from_the_middle() -> None:
    claim = (
        "Removing the inner drain select means that when ctx is cancelled the "
        "function returns immediately while the spawned goroutine is still blocked "
        "sending on the unbuffered errs channel, which nobody will ever receive "
        "from, leaking the goroutine. This happens on every cancellation."
    )
    title, why = title_and_why(claim)
    assert len(title) <= MAX_TITLE_CHARS and title.endswith("…")
    assert why.startswith("…") and why.endswith("This happens on every cancellation.")
    words = lambda t: t.replace("…", " ").replace(",", " ").split()
    assert words(title) + words(why) == words(claim)


def test_model_markdown_cannot_break_the_title() -> None:
    title, why = title_and_why("The *Task is nil when **lookup** misses. See `*t`.")
    assert title == "The \\*Task is nil when \\*\\*lookup\\*\\* misses."
    assert why == "See `*t`."  # code spans are left alone


def test_a_code_span_cut_open_is_closed() -> None:
    title, _ = title_and_why("Calling `f(" + "x, " * 80 + "y)` panics.")
    assert title.count("`") % 2 == 0
