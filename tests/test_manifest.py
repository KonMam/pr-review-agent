import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from pr_review.manifest import ManifestError, load_manifest
from pr_review.models import BugClass, Finding, LineRange


def case(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": "dagr-001",
        "provenance": {
            "repo": "github.com/konmam/dagr",
            "fix_commit": "a" * 40,
            "licence": "MIT",
        },
        "base_ref": "a" * 40,
        "diff_path": "cases/dagr-001.diff",
        "bug_class": "ignored_err",
        "bugs": [{"file": "dag/run.go", "lines": {"start": 41, "end": 43}}],
    }
    data.update(overrides)
    return data


def write(tmp_path: Path, *lines: str) -> Path:
    path = tmp_path / "manifest.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_loads_valid_manifest(tmp_path: Path) -> None:
    path = write(tmp_path, json.dumps(case()), "", json.dumps(case(id="dagr-002")))
    cases = load_manifest(path)
    assert [c.id for c in cases] == ["dagr-001", "dagr-002"]
    assert cases[0].bug_class is BugClass.IGNORED_ERR
    assert cases[0].bugs[0].lines == LineRange(start=41, end=43)


@pytest.mark.parametrize(
    ("bad", "expected"),
    [
        (case(bug_class="style"), "bug_class"),
        (case(bugs=[]), "bugs"),
        (
            case(bugs=[{"file": "a.go", "lines": {"start": 0, "end": 1}}]),
            "bugs.0.lines.start",
        ),
        (
            case(bugs=[{"file": "a.go", "lines": {"start": 5, "end": 4}}]),
            "end (4) is before start",
        ),
        (
            case(provenance={"repo": "r", "fix_commit": "abc", "licence": "MIT"}),
            "fix_commit",
        ),
        (case(base_ref=None), "base_ref"),
        (case(extra="x"), "extra"),
        ({"id": "x"}, "provenance"),
    ],
)
def test_rejects_invalid_case(
    tmp_path: Path, bad: dict[str, Any], expected: str
) -> None:
    path = write(tmp_path, json.dumps(case(id="ok")), json.dumps(bad))
    with pytest.raises(ManifestError, match=r"manifest\.jsonl:2: ") as exc:
        load_manifest(path)
    assert expected in str(exc.value)


def test_rejects_invalid_json(tmp_path: Path) -> None:
    path = write(tmp_path, "{not json")
    with pytest.raises(ManifestError, match=r"manifest\.jsonl:1: invalid JSON"):
        load_manifest(path)


def test_rejects_duplicate_id(tmp_path: Path) -> None:
    path = write(tmp_path, json.dumps(case()), json.dumps(case()))
    with pytest.raises(
        ManifestError, match=r":2: duplicate case id 'dagr-001' \(first on line 1\)"
    ):
        load_manifest(path)


def test_finding_rejects_reversed_line_range() -> None:
    with pytest.raises(ValidationError, match="before start"):
        Finding.model_validate(
            {
                "file": "a.go",
                "lines": {"start": 3, "end": 1},
                "category": "nil_deref",
                "claim": "c",
                "evidence": "e",
            }
        )
