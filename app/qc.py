"""Quality-control evaluation and blank correction.

A plate is published only as a whole.  Every sample must have exactly two
replicate wells with readings, every blank and positive-control well declared
in the plate map must have a reading, and the blank-corrected positive-control
mean must reach the registered lower bound.  Any failure blocks the plate.
"""

from __future__ import annotations

from typing import Any

R = 10  # stored precision


def _round(value: float) -> float:
    return round(float(value), R)


def _mean(values: list[float]) -> float:
    return sum(values) / len(values)


def evaluate(layout: dict[str, Any], readings: dict[str, Any], positive_min: float) -> dict[str, Any]:
    """Return ``{"passed": bool, "result": ..., "qc": ...}``.

    ``qc`` is always populated so failed attempts can explain themselves.
    ``result`` is only populated for a publishable plate.
    """
    well_values: dict[str, float] = readings.get("wells", {})
    mapped_wells: dict[str, dict[str, Any]] = layout.get("wells", {})
    blank_wells: list[str] = list(layout.get("blank_wells", []))
    pc_wells: list[str] = list(layout.get("positive_control_wells", []))
    samples: dict[str, dict[str, Any]] = layout.get("samples", {})
    sample_order: list[str] = list(layout.get("sample_order", list(samples)))

    checks: list[dict[str, Any]] = []
    issues: list[str] = []

    def add_check(check_id: str, ok: bool, label: str, detail: str = "") -> None:
        checks.append({"id": check_id, "ok": ok, "label": label, "detail": detail})
        if not ok:
            issues.append(f"{label}{('：' + detail) if detail else ''}")

    add_check(
        "blank_wells",
        bool(blank_wells),
        "孔位图登记了空白孔",
        "未登记空白孔，无法扣除空白" if not blank_wells else f"{len(blank_wells)} 枚",
    )
    missing_blank = [well for well in blank_wells if well not in well_values]
    add_check(
        "blank_readings",
        bool(blank_wells) and not missing_blank,
        "空白孔读数齐全",
        "缺少读数：" + "、".join(missing_blank) if missing_blank else "",
    )

    add_check(
        "positive_wells",
        bool(pc_wells),
        "孔位图登记了阳性对照孔",
        "未登记阳性对照孔" if not pc_wells else f"{len(pc_wells)} 枚",
    )
    missing_pc = [well for well in pc_wells if well not in well_values]
    add_check(
        "positive_readings",
        bool(pc_wells) and not missing_pc,
        "阳性对照孔读数齐全",
        "缺少读数：" + "、".join(missing_pc) if missing_pc else "",
    )

    replicate_problems: list[str] = []
    for sample_id in sample_order:
        wells = samples[sample_id]["wells"]
        if len(wells) != 2:
            replicate_problems.append(f"{sample_id} 有 {len(wells)} 枚复孔")
        missing = [well for well in wells if well not in well_values]
        if missing:
            replicate_problems.append(f"{sample_id} 缺少 {'、'.join(missing)} 的读数")
    add_check(
        "sample_replicates",
        bool(samples) and not replicate_problems,
        "样本复孔齐全（每样本 2 枚且均有读数）",
        "；".join(replicate_problems) if replicate_problems else f"{len(samples)} 个样本",
    )

    blank_values = [well_values[well] for well in blank_wells if well in well_values]
    blank_mean = _mean(blank_values) if blank_values else None
    blank_detail = {
        "wells": [
            {"well": well, "absorbance": _round(well_values[well])}
            for well in blank_wells
            if well in well_values
        ],
        "mean": _round(blank_mean) if blank_mean is not None else None,
    }

    pc_missing = [well for well in pc_wells if well not in well_values]
    pc_raw_values = [well_values[well] for well in pc_wells if well in well_values]
    pc_corrected_values = (
        [value - blank_mean for value in pc_raw_values] if blank_mean is not None else []
    )
    pc_mean = _mean(pc_corrected_values) if pc_corrected_values else None
    if pc_wells and not pc_missing and blank_mean is not None:
        pc_ok = pc_mean is not None and pc_mean >= positive_min
        add_check(
            "positive_floor",
            pc_ok,
            f"阳性对照扣除空白后均值 ≥ 登记下限 {positive_min:g}",
            (
                f"实测 {pc_mean:.6f}（下限 {positive_min:g}）"
                if pc_mean is not None
                else ""
            ),
        )
    else:
        add_check(
            "positive_floor",
            False,
            f"阳性对照扣除空白后均值 ≥ 登记下限 {positive_min:g}",
            "阳性对照读数或空白均值不可用",
        )

    sample_results: list[dict[str, Any]] = []
    for sample_id in sample_order:
        wells = samples[sample_id]["wells"]
        if len(wells) != 2 or any(well not in well_values for well in wells):
            continue
        if blank_mean is None:
            continue
        well_rows = [
            {
                "well": well,
                "absorbance": _round(well_values[well]),
                "corrected": _round(well_values[well] - blank_mean),
            }
            for well in wells
        ]
        sample_results.append(
            {
                "sample_id": sample_id,
                "wells": well_rows,
                "mean": _round(_mean([row["corrected"] for row in well_rows])),
            }
        )

    ignored_wells = sorted(
        (well for well in well_values if well not in mapped_wells),
        key=lambda well: (well[0], int(well[1:])),
    )
    if ignored_wells:
        checks.append(
            {
                "id": "extra_readings",
                "ok": True,
                "label": "读数文件包含孔位图外的读数（已忽略）",
                "detail": "、".join(ignored_wells),
            }
        )

    positive_detail = {
        "wells": [
            {
                "well": well,
                "absorbance": _round(well_values[well]),
                "corrected": (
                    _round(well_values[well] - blank_mean)
                    if blank_mean is not None
                    else None
                ),
            }
            for well in pc_wells
            if well in well_values
        ],
        "mean": _round(pc_mean) if pc_mean is not None else None,
        "floor": positive_min,
        "passed": (pc_mean is not None and pc_mean >= positive_min),
    }

    passed = all(check["ok"] for check in checks if check["id"] != "extra_readings")
    qc = {
        "passed": passed,
        "checks": checks,
        "issues": issues,
        "blank_mean": _round(blank_mean) if blank_mean is not None else None,
        "positive_control": positive_detail,
        "sample_count": len(samples),
    }
    if not passed:
        return {"passed": False, "result": None, "qc": qc}

    result = {
        "blank": blank_detail,
        "positive_control": positive_detail,
        "samples": sample_results,
        "ignored_readings": ignored_wells,
        "plate_format": layout.get("format"),
    }
    return {"passed": True, "result": result, "qc": qc}
