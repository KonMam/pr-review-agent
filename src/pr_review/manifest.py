"""Load and validate `cases/manifest.jsonl`."""

import json
from pathlib import Path

from pydantic import ValidationError

from pr_review.models import Case


class ManifestError(ValueError):
    """The manifest is malformed. The message names the file and line."""


def load_manifest(path: Path) -> list[Case]:
    """Parse one `Case` per non-blank line. Raises `ManifestError` on the first bad line."""
    cases: list[Case] = []
    seen: dict[str, int] = {}
    text = path.read_text(encoding="utf-8")
    for lineno, raw in enumerate(text.splitlines(), start=1):
        if not raw.strip():
            continue
        where = f"{path}:{lineno}"
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ManifestError(f"{where}: invalid JSON: {e.msg}") from e
        try:
            case = Case.model_validate(data)
        except ValidationError as e:
            raise ManifestError(f"{where}: {_describe(e)}") from e
        if case.id in seen:
            raise ManifestError(
                f"{where}: duplicate case id {case.id!r} (first on line {seen[case.id]})"
            )
        seen[case.id] = lineno
        cases.append(case)
    return cases


def _describe(e: ValidationError) -> str:
    parts = []
    for err in e.errors():
        loc = ".".join(str(p) for p in err["loc"]) or "<root>"
        parts.append(f"{loc}: {err['msg']}")
    return "; ".join(parts)
