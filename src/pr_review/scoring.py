"""The one scorer both arms share.

Matching rule: a finding matches a bug when it names the same file (exact
string match; normalising paths is the arm adapter's job) and its line range
overlaps the bug's range widened by `TOLERANCE` lines on each side.

- precision = findings matching some bug / all findings
- recall    = bugs matched by at least one finding / all bugs
- F1        = harmonic mean of the two

Several findings on one bug each count as a true positive for precision, as the
spec defines it over findings after dedup; deduplicating is the pipeline's job,
not the scorer's.

Known trade-offs, kept on purpose (owner's decision, 2026-11-06): an arm that
skips dedup (CodeRabbit, the single-pass ablation) can inflate precision by
repeating a true positive, and a very wide finding range matches every bug in
its file. Watch for both when reading those arms' numbers.

Zero-division convention: a ratio with a zero denominator is 0.0, never 1.0.
No findings gives precision 0.0, so an arm can't score well by staying silent;
no bugs gives recall 0.0. F1 is 0.0 when precision + recall is 0. The raw
counts are kept on `Score`, so callers can tell "undefined" from "measured 0".

Clean cases (no bugs): every finding is a false positive, so it counts in
precision's denominator, and the case adds nothing to recall. A clean case also
counts toward the false-positive rate: `fp_per_clean_review` (findings per
clean review) and `flagged_clean_share` (share of clean reviews with any
finding). Both are 0.0 when no clean case was scored. A case with no bugs is
clean; the manifest guarantees a bug case has at least one.

Aggregate over cases by adding per-case `Score`s (micro-average): match within
each case, then sum the counts. Matching across cases would let a finding in
one case hit a bug in another.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from pr_review.models import Bug, Finding

TOLERANCE = 3


def matches(finding: Finding, bug: Bug, tolerance: int = TOLERANCE) -> bool:
    """True when the finding is in the bug's file and overlaps its widened line range."""
    if finding.file != bug.file:
        return False
    lo = bug.lines.start - tolerance
    hi = bug.lines.end + tolerance
    return finding.lines.start <= hi and finding.lines.end >= lo


@dataclass(frozen=True)
class Score:
    """Match counts for one or more cases, with the ratios derived from them."""

    findings: int = 0
    matched_findings: int = 0
    bugs: int = 0
    matched_bugs: int = 0
    clean_reviews: int = 0
    clean_findings: int = 0
    flagged_clean_reviews: int = 0  # clean reviews with at least one finding

    def __add__(self, other: "Score") -> "Score":
        return Score(
            findings=self.findings + other.findings,
            matched_findings=self.matched_findings + other.matched_findings,
            bugs=self.bugs + other.bugs,
            matched_bugs=self.matched_bugs + other.matched_bugs,
            clean_reviews=self.clean_reviews + other.clean_reviews,
            clean_findings=self.clean_findings + other.clean_findings,
            flagged_clean_reviews=self.flagged_clean_reviews
            + other.flagged_clean_reviews,
        )

    @property
    def precision(self) -> float:
        return _ratio(self.matched_findings, self.findings)

    @property
    def recall(self) -> float:
        return _ratio(self.matched_bugs, self.bugs)

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 0.0 if p + r == 0 else 2 * p * r / (p + r)

    @property
    def fp_per_clean_review(self) -> float:
        return _ratio(self.clean_findings, self.clean_reviews)

    @property
    def flagged_clean_share(self) -> float:
        return _ratio(self.flagged_clean_reviews, self.clean_reviews)


def score_case(findings: Sequence[Finding], bugs: Sequence[Bug]) -> Score:
    """Score one case's findings against its ground-truth bugs (none: a clean case)."""
    matched_findings = sum(1 for f in findings if any(matches(f, b) for b in bugs))
    matched_bugs = sum(1 for b in bugs if any(matches(f, b) for f in findings))
    clean = not bugs
    return Score(
        findings=len(findings),
        matched_findings=matched_findings,
        bugs=len(bugs),
        matched_bugs=matched_bugs,
        clean_reviews=int(clean),
        clean_findings=len(findings) if clean else 0,
        flagged_clean_reviews=int(clean and bool(findings)),
    )


def _ratio(num: int, den: int) -> float:
    return 0.0 if den == 0 else num / den
