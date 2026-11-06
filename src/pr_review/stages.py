"""Stages 2-4 after generate: drop low grades, collapse duplicates, suppress categories.

These run on stored candidates, with no model call, so the ablation table and
the threshold sweep are computed from one set of paid replies. The grade
scores themselves come from `grade.py`.

Order is the spec's: grade, then dedup, then suppress. Each is toggleable.
"""

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from pr_review.models import Finding


class Stage(StrEnum):
    GRADE = "grade"
    DEDUP = "dedup"
    SUPPRESS = "suppress"


ORDER = (Stage.GRADE, Stage.DEDUP, Stage.SUPPRESS)

# The spec's out-of-scope categories: gofmt, go vet and golangci-lint own them.
SUPPRESSED_CATEGORIES = frozenset(
    {"style", "naming", "formatting", "docs", "readability"}
)

# Logged with every CI finding (item 8), so item 9 can tell which stage rules
# produced it. Bump DEDUP_VERSION when `_same_claim` or `dedup` changes;
# SUPPRESS_VERSION follows the category list on its own.
DEDUP_VERSION = "dedup-1"
SUPPRESS_VERSION = (
    "suppress-"
    + hashlib.sha256(",".join(sorted(SUPPRESSED_CATEGORIES)).encode()).hexdigest()[:8]
)

# From the item-4 sweep (or-deepseek-flash, 41x3): the highest recall at the
# sweep's best precision (1.000). Tuned on the scored set itself; see the spec.
DEFAULT_THRESHOLD = 0.3


@dataclass(frozen=True)
class Candidate:
    """A generated finding and its grade (None when ungraded or the grader failed)."""

    finding: Finding
    score: float | None = None


def normalise_category(category: str) -> str:
    return category.strip().lower().replace("-", "_").replace(" ", "_")


def is_suppressed(finding: Finding) -> bool:
    return normalise_category(finding.category) in SUPPRESSED_CATEGORIES


def keep_graded(candidates: Sequence[Candidate], threshold: float) -> list[Candidate]:
    """Keep candidates scored at or above `threshold`.

    A candidate with no score (the grader's reply stayed malformed) is dropped:
    precision first, and the summary counts these reviews.
    """
    return [c for c in candidates if c.score is not None and c.score >= threshold]


def _same_claim(a: Finding, b: Finding) -> bool:
    """Same file, overlapping lines, same category."""
    return (
        a.file == b.file
        and a.lines.start <= b.lines.end
        and b.lines.start <= a.lines.end
        and normalise_category(a.category) == normalise_category(b.category)
    )


def dedup(candidates: Sequence[Candidate]) -> list[Candidate]:
    """Collapse findings that make the same claim about overlapping lines.

    Deterministic: same file, overlapping line ranges and the same category
    count as one claim. The best-graded one survives (the first on a tie),
    with its own range, never a merged one: widening a range would let it match
    bugs it doesn't name. Survivors keep the generator's order.
    """
    ranked = sorted(
        range(len(candidates)),
        key=lambda i: (-(candidates[i].score or 0.0), i),
    )
    kept: list[int] = []
    for i in ranked:
        if not any(
            _same_claim(candidates[i].finding, candidates[k].finding) for k in kept
        ):
            kept.append(i)
    return [candidates[i] for i in sorted(kept)]


def suppress(candidates: Sequence[Candidate]) -> list[Candidate]:
    return [c for c in candidates if not is_suppressed(c.finding)]


def apply(
    candidates: Sequence[Candidate],
    stages: Sequence[Stage],
    threshold: float = DEFAULT_THRESHOLD,
) -> list[Candidate]:
    """Run the enabled stages, in the spec's order whatever order they're given in."""
    out = list(candidates)
    for stage in ORDER:
        if stage not in stages:
            continue
        if stage is Stage.GRADE:
            out = keep_graded(out, threshold)
        elif stage is Stage.DEDUP:
            out = dedup(out)
        else:
            out = suppress(out)
    return out
