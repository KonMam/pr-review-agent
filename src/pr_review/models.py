"""Shared data models: line ranges, golden-set cases and findings.

Both arms (the agent and the bought tool) are normalised to `Finding`, and the
golden set in `cases/manifest.jsonl` is one `Case` per line.
"""

from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Strict(BaseModel):
    """Base for manifest and finding models: unknown fields are an error, not ignored."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class LineRange(_Strict):
    """A 1-based, inclusive range of lines in one file."""

    start: int = Field(ge=1)
    end: int = Field(ge=1)

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.end < self.start:
            raise ValueError(f"end ({self.end}) is before start ({self.start})")
        return self


class BugClass(StrEnum):
    """Go bug classes linters miss. Every golden-set case carries one."""

    NIL_DEREF = "nil_deref"  # nil dereferences and nil maps
    IGNORED_ERR = "ignored_err"  # ignored or shadowed err
    GOROUTINE_LEAK = "goroutine_leak"  # goroutine leaks and missing cancellation
    DATA_RACE = "data_race"  # data races and mutex misuse
    DEFER_IN_LOOP = "defer_in_loop"
    SLICE_ALIASING = "slice_aliasing"  # slice aliasing after append
    CONTEXT_PROPAGATION = "context_propagation"
    OVERFLOW_OFF_BY_ONE = "overflow_off_by_one"  # integer overflow and off-by-one
    UNCLOSED_RESOURCE = "unclosed_resource"


class Provenance(_Strict):
    """Where a case came from."""

    repo: str = Field(min_length=1)
    fix_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    licence: str = Field(min_length=1)


class Bug(_Strict):
    """One ground-truth bug location."""

    file: str = Field(min_length=1)
    lines: LineRange


class Case(_Strict):
    """One golden-set case: a buggy diff, the commit it applies to, and where the bug is."""

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]*$")
    provenance: Provenance
    base_ref: str = Field(min_length=1)
    diff_path: str = Field(min_length=1)
    bug_class: BugClass
    bugs: list[Bug] = Field(min_length=1)


class Finding(_Strict):
    """One review comment, normalised. Both arms are scored in this shape."""

    file: str = Field(min_length=1)
    lines: LineRange
    category: str = Field(min_length=1)
    claim: str = Field(min_length=1)
    evidence: str
