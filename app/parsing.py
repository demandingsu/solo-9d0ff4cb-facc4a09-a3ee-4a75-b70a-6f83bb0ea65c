"""Parsing and validation for plate-map layouts and reader exports.

Supported inputs are delimited text files (CSV, TSV or semicolon separated)
encoded as UTF-8 (with or without BOM) or GB18030.

Layout files come in two shapes:

* long form with a header containing ``well`` and ``role`` (plus an optional
  ``sample`` column)::

      well,role,sample_id
      A1,blank,
      A2,sample,S1
      A3,sample,S1

* a grid with column numbers in the header and row letters in the first
  column, where the cells hold a sample id, ``BLANK`` or ``PC``::

      ,1,2,3
      A,BLANK,S1,S1
      B,PC,S2,S2

Reading files come in the matching long form (``well,absorbance``) or as a
numeric grid with the same header layout.

Validation here is structural: malformed files are rejected.  Experimental
conditions (two replicates, positive-control floor, ...) are evaluated later
so that a mistaken layout can be stored, inspected and corrected rather than
silently producing a publication.
"""

from __future__ import annotations

import csv
import io
import math
import re
from dataclasses import dataclass, field
from typing import Any

MAX_ROW = "P"  # supports 96-well (A-H) and 384-well (A-P) plates
MAX_COLUMN = 24

_WELL_RE = re.compile(r"^([A-Za-z])\s*0*([0-9]{1,2})$")
_ROW_ONLY_RE = re.compile(r"^([A-Za-z])$")
_INT_RE = re.compile(r"^[0-9]{1,2}$")

_WELL_HEADERS = {
    "well",
    "wells",
    "well_id",
    "position",
    "孔位",
    "孔位编号",
    "孔",
}
_ROLE_HEADERS = {"role", "type", "well_type", "孔位类型", "类型", "角色"}
_SAMPLE_HEADERS = {"sample", "sample_id", "sampleid", "样本", "样本编号", "样本id"}
_VALUE_HEADERS = {
    "absorbance",
    "abs",
    "a",
    "od",
    "od600",
    "value",
    "reading",
    "读数",
    "吸光度",
    "吸光值",
    "数值",
}

_ROLE_ALIASES = {
    "sample": "sample",
    "s": "sample",
    "样本": "sample",
    "样本孔": "sample",
    "blank": "blank",
    "blk": "blank",
    "b": "blank",
    "空白": "blank",
    "空白孔": "blank",
    "pc": "positive_control",
    "pos": "positive_control",
    "positive": "positive_control",
    "positive control": "positive_control",
    "positive_control": "positive_control",
    "positivecontrol": "positive_control",
    "阳性": "positive_control",
    "阳性对照": "positive_control",
    "阳性对照孔": "positive_control",
    "阳对": "positive_control",
    "unused": "unused",
    "empty": "unused",
    "none": "unused",
    "空": "unused",
    "未用": "unused",
}

# Single-letter aliases are only honoured in long-form files; in a grid a cell
# such as ``B`` is far more likely to be a sample id than a blank marker.
GRID_ROLE_LABELS = {
    "blank": "blank",
    "blk": "blank",
    "空白": "blank",
    "空白孔": "blank",
    "pc": "positive_control",
    "pos": "positive_control",
    "positive": "positive_control",
    "positive control": "positive_control",
    "positive_control": "positive_control",
    "positivecontrol": "positive_control",
    "阳性": "positive_control",
    "阳性对照": "positive_control",
    "阳性对照孔": "positive_control",
    "阳对": "positive_control",
}
GRID_UNUSED_LABELS = {"unused", "empty", "none", "空", "未用", "-"}
GRID_SAMPLE_WORD_LABELS = {"sample", "s", "样本", "样本孔"}


class InputError(ValueError):
    """A user-supplied file could not be interpreted."""


@dataclass
class ParsedLayout:
    layout: dict[str, Any]
    warnings: list[str] = field(default_factory=list)


@dataclass
class ParsedReadings:
    readings: dict[str, Any]
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Generic helpers


def decode_text(data: bytes) -> str:
    if not data:
        raise InputError("文件为空")
    if b"\x00" in data:
        raise InputError("文件包含二进制内容，请上传 CSV/TSV 文本文件")
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            text = data.decode(encoding)
        except UnicodeDecodeError:
            continue
        if text.strip():
            return text
    raise InputError("文件编码无法识别，请使用 UTF-8 或 GB18030")


def _sniff_delimiter(text: str) -> str:
    lines = [line for line in text.splitlines() if line.strip()][:20]
    counts = {",": 0, ";": 0, "\t": 0}
    for line in lines:
        in_quotes = False
        for char in line:
            if char == '"':
                in_quotes = not in_quotes
            elif not in_quotes and char in counts:
                counts[char] += 1
    best = max(counts, key=counts.get)
    return best if counts[best] else ","


def _read_rows(text: str) -> list[list[str]]:
    delimiter = _sniff_delimiter(text)
    try:
        raw_rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    except csv.Error as exc:
        raise InputError(f"CSV 解析失败：{exc}") from exc
    rows: list[list[str]] = []
    for raw in raw_rows:
        cells = [cell.strip() for cell in raw]
        if not any(cells):
            continue
        rows.append(cells)
    if not rows:
        raise InputError("文件没有有效行")
    return rows


def _header_norm(cell: str) -> str:
    return re.sub(r"\s+", " ", cell.strip().lower())


def normalize_well(cell: str) -> str:
    """Return a canonical well label such as ``A1`` or raise ``InputError``."""
    match = _WELL_RE.match(cell.strip())
    if not match:
        raise InputError(f"无效孔位“{cell}”（应形如 A1）")
    row = match.group(1).upper()
    column = int(match.group(2))
    if not ("A" <= row <= MAX_ROW):
        raise InputError(f"孔位“{cell}”超出支持范围（行 A-{MAX_ROW}）")
    if not (1 <= column <= MAX_COLUMN):
        raise InputError(f"孔位“{cell}”超出支持范围（列 1-{MAX_COLUMN}）")
    return f"{row}{column}"


def _normalize_role(cell: str) -> str:
    key = _header_norm(cell)
    role = _ROLE_ALIASES.get(key)
    if not role:
        raise InputError(
            f"无法识别的孔位角色“{cell}”（可用 sample / blank / positive_control）"
        )
    return role


def _looks_like_grid(header: list[str]) -> bool:
    if not header:
        return False
    first = _header_norm(header[0])
    if first and first not in _WELL_HEADERS:
        return False
    rest = header[1:]
    if not rest:
        return False
    return all(_INT_RE.match(cell) and 1 <= int(cell) <= MAX_COLUMN for cell in rest)


def _plate_format(wells: dict[str, Any]) -> str:
    if not wells:
        return "empty"
    max_row = max(well[0] for well in wells)
    max_col = max(int(well[1:]) for well in wells)
    if max_row <= "H" and max_col <= 12:
        return "96-well"
    if max_row <= "P" and max_col <= 24:
        return "384-well"
    return "custom"


def _natural_key(value: str) -> list[Any]:
    return [
        int(part) if part.isdigit() else part.lower()
        for part in re.split(r"([0-9]+)", value)
    ]


# ---------------------------------------------------------------------------
# Layout parsing


def parse_layout(data: bytes) -> ParsedLayout:
    text = decode_text(data)
    rows = _read_rows(text)

    if _looks_like_grid(rows[0]):
        wells = _parse_layout_grid(rows)
    else:
        wells = _parse_layout_long(rows)

    if not wells:
        raise InputError("孔位图没有登记任何孔位")

    samples: dict[str, list[str]] = {}
    blanks: list[str] = []
    positives: list[str] = []
    for well in sorted(wells, key=lambda w: (w[0], int(w[1:]))):
        entry = wells[well]
        role = entry["role"]
        if role == "sample":
            samples.setdefault(entry["sample_id"], []).append(well)
        elif role == "blank":
            blanks.append(well)
        elif role == "positive_control":
            positives.append(well)
        # unused entries are dropped below

    warnings: list[str] = []
    if not blanks:
        warnings.append("孔位图未登记空白孔，整板不能发布")
    if not positives:
        warnings.append("孔位图未登记阳性对照孔，整板不能发布")
    if not samples:
        warnings.append("孔位图未登记任何样本，整板不能发布")
    for sample_id in sorted(samples, key=_natural_key):
        count = len(samples[sample_id])
        if count != 2:
            warnings.append(f"样本 {sample_id} 有 {count} 枚复孔（发布要求恰好 2 枚）")

    layout = {
        "format": _plate_format(wells),
        "wells": wells,
        "samples": {
            sample_id: {"wells": sorted(sample_wells, key=lambda w: (w[0], int(w[1:])))}
            for sample_id, sample_wells in samples.items()
        },
        "sample_order": sorted(samples, key=_natural_key),
        "blank_wells": blanks,
        "positive_control_wells": positives,
        "warnings": warnings,
    }
    return ParsedLayout(layout=layout, warnings=warnings)


def _parse_layout_long(rows: list[list[str]]) -> dict[str, dict[str, Any]]:
    header = [_header_norm(cell) for cell in rows[0]]
    well_idx = next((i for i, cell in enumerate(header) if cell in _WELL_HEADERS), None)
    role_idx = next((i for i, cell in enumerate(header) if cell in _ROLE_HEADERS), None)
    sample_idx = next((i for i, cell in enumerate(header) if cell in _SAMPLE_HEADERS), None)
    if well_idx is None or role_idx is None:
        raise InputError(
            "无法识别孔位图表头：请提供 well、role（可选 sample_id）列，"
            "或使用“行字母 + 列号”网格格式"
        )

    wells: dict[str, dict[str, Any]] = {}
    for line_no, row in enumerate(rows[1:], start=2):
        cell_well = row[well_idx] if well_idx < len(row) else ""
        cell_role = row[role_idx] if role_idx < len(row) else ""
        if not cell_well and not cell_role:
            continue
        if not cell_well:
            raise InputError(f"第 {line_no} 行缺少孔位")
        well = normalize_well(cell_well)
        if well in wells:
            raise InputError(f"孔位 {well} 在第 {line_no} 行重复登记")
        if not cell_role:
            raise InputError(f"第 {line_no} 行（{well}）缺少孔位角色")
        role = _normalize_role(cell_role)
        if role == "unused":
            continue
        entry: dict[str, Any] = {"role": role}
        if role == "sample":
            sample_id = row[sample_idx].strip() if sample_idx is not None and sample_idx < len(row) else ""
            if not sample_id:
                raise InputError(f"第 {line_no} 行（{well}）是样本孔，但缺少样本编号")
            entry["sample_id"] = sample_id
        elif sample_idx is not None and sample_idx < len(row) and row[sample_idx].strip():
            entry["label"] = row[sample_idx].strip()
        wells[well] = entry
    return wells


def _parse_layout_grid(rows: list[list[str]]) -> dict[str, dict[str, Any]]:
    header = rows[0]
    columns: list[int] = []
    for cell in header[1:]:
        if not cell:
            continue
        if not _INT_RE.match(cell):
            raise InputError(f"网格表头列号无效“{cell}”")
        number = int(cell)
        if not (1 <= number <= MAX_COLUMN):
            raise InputError(f"网格列号 {number} 超出范围（1-{MAX_COLUMN}）")
        if number in columns:
            raise InputError(f"网格列号 {number} 重复")
        columns.append(number)
    if not columns:
        raise InputError("网格格式缺少列号表头")

    wells: dict[str, dict[str, Any]] = {}
    seen_rows: set[str] = set()
    for line_no, row in enumerate(rows[1:], start=2):
        row_cell = row[0] if row else ""
        match = _ROW_ONLY_RE.match(row_cell)
        if not match:
            raise InputError(f"第 {line_no} 行行号无效“{row_cell}”（应为 A-P）")
        row_letter = match.group(1).upper()
        if not ("A" <= row_letter <= MAX_ROW):
            raise InputError(f"第 {line_no} 行行号“{row_cell}”超出范围（A-{MAX_ROW}）")
        if row_letter in seen_rows:
            raise InputError(f"行 {row_letter} 重复")
        seen_rows.add(row_letter)
        cells = row[1:]
        if len(cells) > len(columns) and any(cell for cell in cells[len(columns):]):
            raise InputError(f"第 {line_no} 行的列数超过表头")
        for index, column in enumerate(columns):
            raw = cells[index] if index < len(cells) else ""
            well = f"{row_letter}{column}"
            if not raw or raw == "-":
                continue
            role_key = _header_norm(raw)
            if role_key in GRID_UNUSED_LABELS:
                continue
            if role_key in GRID_ROLE_LABELS:
                wells[well] = {"role": GRID_ROLE_LABELS[role_key]}
            elif role_key in GRID_SAMPLE_WORD_LABELS:
                raise InputError(f"孔位 {well} 应填写样本编号，而不是“{raw}”")
            else:
                wells[well] = {"role": "sample", "sample_id": raw}
    return wells


# ---------------------------------------------------------------------------
# Readings parsing


def parse_readings(data: bytes) -> ParsedReadings:
    text = decode_text(data)
    rows = _read_rows(text)

    if _looks_like_grid(rows[0]):
        wells, warnings = _parse_readings_grid(rows)
    else:
        wells, warnings = _parse_readings_long(rows)

    if not wells:
        raise InputError("读数文件没有包含任何孔位读数")
    return ParsedReadings(
        readings={"wells": wells, "well_count": len(wells), "warnings": warnings},
        warnings=warnings,
    )


def _parse_float(cell: str, well: str, *, allow_empty: bool = False) -> float | None:
    raw = cell.strip()
    if not raw:
        if allow_empty:
            return None
        raise InputError(f"孔位 {well} 的读数为空")
    candidate = raw
    if "," in candidate and "." not in candidate and candidate.count(",") == 1:
        candidate = candidate.replace(",", ".")
    try:
        value = float(candidate)
    except ValueError as exc:
        raise InputError(f"孔位 {well} 的读数“{raw}”不是数字") from exc
    if not math.isfinite(value):
        raise InputError(f"孔位 {well} 的读数不是有限数值")
    return value


def _parse_readings_long(rows: list[list[str]]) -> tuple[dict[str, float], list[str]]:
    header = [_header_norm(cell) for cell in rows[0]]
    well_idx = next((i for i, cell in enumerate(header) if cell in _WELL_HEADERS), None)
    value_idx = next((i for i, cell in enumerate(header) if cell in _VALUE_HEADERS), None)
    if well_idx is None or value_idx is None:
        raise InputError(
            "无法识别读数表头：请提供 well、absorbance 列，"
            "或使用“行字母 + 列号”网格格式"
        )
    wells: dict[str, float] = {}
    for line_no, row in enumerate(rows[1:], start=2):
        cell_well = row[well_idx] if well_idx < len(row) else ""
        cell_value = row[value_idx] if value_idx < len(row) else ""
        if not cell_well and not cell_value:
            continue
        if not cell_well:
            raise InputError(f"第 {line_no} 行缺少孔位")
        well = normalize_well(cell_well)
        if well in wells:
            raise InputError(f"孔位 {well} 在读数文件中重复出现，无法确定采用哪一条")
        value = _parse_float(cell_value, well)
        assert value is not None
        wells[well] = value
    return wells, []


def _parse_readings_grid(rows: list[list[str]]) -> tuple[dict[str, float], list[str]]:
    header = rows[0]
    columns: list[int] = []
    for cell in header[1:]:
        if not cell:
            continue
        if not _INT_RE.match(cell):
            raise InputError(f"网格表头列号无效“{cell}”")
        number = int(cell)
        if not (1 <= number <= MAX_COLUMN):
            raise InputError(f"网格列号 {number} 超出范围（1-{MAX_COLUMN}）")
        if number in columns:
            raise InputError(f"网格列号 {number} 重复")
        columns.append(number)
    if not columns:
        raise InputError("网格格式缺少列号表头")

    wells: dict[str, float] = {}
    warnings: list[str] = []
    seen_rows: set[str] = set()
    for line_no, row in enumerate(rows[1:], start=2):
        row_cell = row[0] if row else ""
        match = _ROW_ONLY_RE.match(row_cell)
        if not match:
            raise InputError(f"第 {line_no} 行行号无效“{row_cell}”（应为 A-P）")
        row_letter = match.group(1).upper()
        if not ("A" <= row_letter <= MAX_ROW):
            raise InputError(f"第 {line_no} 行行号“{row_cell}”超出范围（A-{MAX_ROW}）")
        if row_letter in seen_rows:
            raise InputError(f"行 {row_letter} 重复")
        seen_rows.add(row_letter)
        cells = row[1:]
        if len(cells) > len(columns) and any(cell for cell in cells[len(columns):]):
            raise InputError(f"第 {line_no} 行的列数超过表头")
        for index, column in enumerate(columns):
            raw = cells[index] if index < len(cells) else ""
            well = f"{row_letter}{column}"
            value = _parse_float(raw, well, allow_empty=True)
            if value is not None:
                wells[well] = value
    if not wells:
        warnings.append("读数文件没有非空读数")
    return wells, warnings
