import pytest

from pr_review.models import Bug, Finding, LineRange
from pr_review.scoring import Score, matches, score_case


def bug(file: str, start: int, end: int) -> Bug:
    return Bug(file=file, lines=LineRange(start=start, end=end))


def finding(file: str, start: int, end: int | None = None) -> Finding:
    return Finding(
        file=file,
        lines=LineRange(start=start, end=start if end is None else end),
        category="nil_deref",
        claim="c",
        evidence="e",
    )


BUG = bug("pkg/a.go", 10, 12)


def test_exact_hit() -> None:
    s = score_case([finding("pkg/a.go", 10, 12)], [BUG])
    assert (s.precision, s.recall, s.f1) == (1.0, 1.0, 1.0)


@pytest.mark.parametrize(
    ("start", "end", "hit"),
    [
        (13, 15, True),  # starts inside +3 window
        (15, 15, True),  # end + 3
        (16, 16, False),  # end + 4
        (7, 7, True),  # start - 3
        (6, 6, False),  # start - 4
        (1, 6, False),  # range ends at start - 4
        (1, 7, True),  # range ends at start - 3
    ],
)
def test_tolerance_window(start: int, end: int, hit: bool) -> None:
    assert matches(finding("pkg/a.go", start, end), BUG) is hit


def test_near_miss_scores_zero() -> None:
    s = score_case([finding("pkg/a.go", 16)], [BUG])
    assert s == Score(findings=1, matched_findings=0, bugs=1, matched_bugs=0)
    assert (s.precision, s.recall, s.f1) == (0.0, 0.0, 0.0)


def test_wrong_file_never_matches() -> None:
    s = score_case([finding("pkg/b.go", 10, 12)], [BUG])
    assert (s.matched_findings, s.matched_bugs) == (0, 0)


def test_several_findings_on_one_bug() -> None:
    fs = [finding("pkg/a.go", 10), finding("pkg/a.go", 12), finding("pkg/a.go", 40)]
    s = score_case(fs, [BUG])
    assert s == Score(findings=3, matched_findings=2, bugs=1, matched_bugs=1)
    assert s.precision == pytest.approx(2 / 3)
    assert s.recall == 1.0
    assert s.f1 == pytest.approx(0.8)


def test_one_finding_spanning_two_bugs() -> None:
    bugs = [bug("pkg/a.go", 10, 12), bug("pkg/a.go", 30, 31)]
    s = score_case([finding("pkg/a.go", 11, 30)], bugs)
    assert s == Score(findings=1, matched_findings=1, bugs=2, matched_bugs=2)
    assert (s.precision, s.recall) == (1.0, 1.0)


def test_no_findings() -> None:
    s = score_case([], [BUG])
    assert s == Score(findings=0, matched_findings=0, bugs=1, matched_bugs=0)
    assert (s.precision, s.recall, s.f1) == (0.0, 0.0, 0.0)


def test_no_bugs() -> None:
    s = score_case([finding("pkg/a.go", 10)], [])
    assert s == Score(findings=1, matched_findings=0, bugs=0, matched_bugs=0)
    assert (s.precision, s.recall, s.f1) == (0.0, 0.0, 0.0)


def test_nothing_at_all() -> None:
    s = score_case([], [])
    assert (s.precision, s.recall, s.f1) == (0.0, 0.0, 0.0)


def test_micro_average_across_cases() -> None:
    hit = score_case([finding("pkg/a.go", 10)], [BUG])
    silent = score_case([], [bug("pkg/b.go", 5, 5)])
    total = hit + silent
    assert total == Score(findings=1, matched_findings=1, bugs=2, matched_bugs=1)
    assert (total.precision, total.recall) == (1.0, 0.5)
