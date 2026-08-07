"""Bounded, local tabular profiling and reproducible analysis recipes."""

from __future__ import annotations

import csv
import io
import math
import statistics
from dataclasses import dataclass
from typing import Any, Mapping, Sequence


@dataclass(frozen=True)
class DatasetProfile:
    """Schema summary retained alongside a dataset version."""

    filename: str
    delimiter: str
    columns: tuple[dict[str, Any], ...]
    row_count: int
    preview: tuple[dict[str, str], ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "filename": self.filename,
            "delimiter": self.delimiter,
            "columns": [dict(column) for column in self.columns],
            "row_count": self.row_count,
            "preview": [dict(row) for row in self.preview],
        }


@dataclass(frozen=True)
class AnalysisResult:
    """Result of one safe recipe execution."""

    recipe: Mapping[str, Any]
    result: Mapping[str, Any]
    warnings: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {"recipe": dict(self.recipe), "result": dict(self.result), "warnings": list(self.warnings)}


def profile_csv(data: bytes, *, filename: str = "dataset.csv", max_rows: int = 100_000) -> DatasetProfile:
    """Inspect a CSV without evaluating formulas or executing embedded code."""

    text = data.decode("utf-8-sig", errors="replace")
    sample = text[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    fieldnames = tuple(reader.fieldnames or ())
    if not fieldnames:
        raise ValueError("Le fichier tabulaire ne contient pas d’en-tête CSV.")
    rows: list[dict[str, str]] = []
    for row in reader:
        rows.append({name: str(row.get(name, "") or "") for name in fieldnames})
        if len(rows) >= max_rows:
            break
    columns: list[dict[str, Any]] = []
    for name in fieldnames:
        values = [row[name] for row in rows]
        non_empty = [value for value in values if value.strip()]
        numeric = [_number(value) for value in non_empty]
        numeric_values = [value for value in numeric if value is not None]
        if non_empty and len(numeric_values) == len(non_empty):
            inferred = "number"
        elif _looks_boolean(non_empty):
            inferred = "boolean"
        else:
            inferred = "text"
        columns.append(
            {
                "name": name,
                "type": inferred,
                "missing": len(values) - len(non_empty),
                "distinct": len(set(non_empty)),
                "sample": non_empty[:3],
            }
        )
    return DatasetProfile(filename, delimiter, tuple(columns), len(rows), tuple(rows[:8]))


def execute_recipe(data: bytes, recipe: Mapping[str, Any], *, filename: str = "dataset.csv") -> AnalysisResult:
    """Run a small allowlisted analysis recipe deterministically.

    Supported operations are ``describe``, ``value_counts`` and
    ``correlation``.  The recipe is data, never Python code, and unknown
    operations fail closed.
    """

    profile = profile_csv(data, filename=filename)
    rows = _rows(data, profile.delimiter)
    operation = str(recipe.get("operation", "describe")).strip().lower()
    warnings: list[str] = []
    if operation == "describe":
        columns = recipe.get("columns")
        names = tuple(str(value) for value in columns) if isinstance(columns, list) else tuple(
            column["name"] for column in profile.columns if column["type"] == "number"
        )
        result = {"operation": operation, "row_count": len(rows), "columns": {name: _describe(rows, name) for name in names}}
    elif operation == "value_counts":
        column = str(recipe.get("column", "")).strip()
        if not column:
            raise ValueError("value_counts nécessite une colonne.")
        values = [row.get(column, "") for row in rows]
        result = {"operation": operation, "column": column, "counts": _counts(values)}
    elif operation == "correlation":
        columns = recipe.get("columns")
        if not isinstance(columns, list) or len(columns) < 2:
            raise ValueError("correlation nécessite au moins deux colonnes.")
        names = tuple(str(value) for value in columns)
        matrix: dict[str, dict[str, float | None]] = {}
        for left in names:
            matrix[left] = {}
            for right in names:
                matrix[left][right] = _correlation(rows, left, right)
        result = {"operation": operation, "columns": list(names), "matrix": matrix}
    else:
        raise ValueError("Opération d’analyse non autorisée.")
    if len(rows) < profile.row_count:
        warnings.append("Le profilage a été limité par la taille maximale de sécurité.")
    return AnalysisResult(dict(recipe), result, tuple(warnings))


def _rows(data: bytes, delimiter: str) -> list[dict[str, str]]:
    text = data.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    return [{name: str(row.get(name, "") or "") for name in (reader.fieldnames or ())} for row in reader]


def _describe(rows: Sequence[Mapping[str, str]], column: str) -> dict[str, Any]:
    values = [value for value in (_number(row.get(column, "")) for row in rows) if value is not None]
    if not values:
        return {"count": 0, "missing": len(rows), "mean": None, "min": None, "max": None, "stdev": None}
    return {
        "count": len(values),
        "missing": len(rows) - len(values),
        "mean": statistics.fmean(values),
        "min": min(values),
        "max": max(values),
        "stdev": statistics.stdev(values) if len(values) > 1 else 0.0,
    }


def _counts(values: Sequence[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        key = value.strip() or "(vide)"
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:100])


def _correlation(rows: Sequence[Mapping[str, str]], left: str, right: str) -> float | None:
    pairs = [(_number(row.get(left, "")), _number(row.get(right, ""))) for row in rows]
    values = [(x, y) for x, y in pairs if x is not None and y is not None]
    if len(values) < 2:
        return None
    xs, ys = zip(*values)
    mean_x, mean_y = statistics.fmean(xs), statistics.fmean(ys)
    numerator = sum((x - mean_x) * (y - mean_y) for x, y in values)
    denom = math.sqrt(sum((x - mean_x) ** 2 for x in xs) * sum((y - mean_y) ** 2 for y in ys))
    return numerator / denom if denom else None


def _number(value: Any) -> float | None:
    text = str(value or "").strip().replace(",", ".")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _looks_boolean(values: Sequence[str]) -> bool:
    return bool(values) and all(value.strip().lower() in {"true", "false", "yes", "no", "0", "1", "oui", "non"} for value in values)
