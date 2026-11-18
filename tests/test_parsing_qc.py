"""Unit tests for parsing and QC evaluation."""

from __future__ import annotations

import pytest

from app import parsing, qc


GOOD_LAYOUT = b"""well,role,sample_id
A1,blank,
A2,sample,S1
A3,sample,S1
A4,sample,S2
A5,sample,S2
A6,positive_control,
"""

GOOD_READINGS = b"""well,absorbance
A1,0.100
A2,0.600
A3,0.700
A4,1.000
A5,1.100
A6,1.200
"""


def test_parse_long_layout():
    parsed = parsing.parse_layout(GOOD_LAYOUT)
    layout = parsed.layout
    assert layout["samples"]["S1"]["wells"] == ["A2", "A3"]
    assert layout["blank_wells"] == ["A1"]
    assert layout["positive_control_wells"] == ["A6"]
    assert layout["format"] == "96-well"


def test_parse_grid_layout():
    data = " ,1,2,3\nA,BLANK,S1,S1\nB,PC,S2,S2\n".encode()
    layout = parsing.parse_layout(data).layout
    assert layout["blank_wells"] == ["A1"]
    assert layout["positive_control_wells"] == ["B1"]
    assert layout["samples"]["S1"]["wells"] == ["A2", "A3"]


def test_parse_grid_layout_chinese_labels_and_bom():
    data = "﻿,1,2\nA,空白,样本A\nB,阳性对照,样本A\n".encode("utf-8")
    layout = parsing.parse_layout(data).layout
    assert layout["blank_wells"] == ["A1"]
    assert layout["positive_control_wells"] == ["B1"]
    assert layout["samples"]["样本A"]["wells"] == ["A2", "B2"]


def test_parse_gb18030_readings():
    data = "孔位,吸光度\nA1,0.1\nA2,0.2\n".encode("gb18030")
    parsed = parsing.parse_readings(data)
    assert parsed.readings["wells"] == {"A1": 0.1, "A2": 0.2}


@pytest.mark.parametrize(
    "payload",
    [
        b"well,role,sample_id\nA1,blank,\nA1,blank,\n",  # duplicate well
        b"well,role,sample_id\nZ1,blank,\n",  # out of range
        b"well,role,sample_id\nA1,weird,\n",  # bad role
        b"",  # empty
        b"well,role\nA1,sample\n",  # sample without id
    ],
)
def test_layout_rejections(payload):
    with pytest.raises(parsing.InputError):
        parsing.parse_layout(payload)


def test_duplicate_reading_rejected():
    with pytest.raises(parsing.InputError):
        parsing.parse_readings(b"well,absorbance\nA1,0.1\nA1,0.2\n")


def test_non_numeric_reading_rejected():
    with pytest.raises(parsing.InputError):
        parsing.parse_readings(b"well,absorbance\nA1,abc\n")


def test_qc_passes_and_blank_correction():
    layout = parsing.parse_layout(GOOD_LAYOUT).layout
    readings = parsing.parse_readings(GOOD_READINGS).readings
    outcome = qc.evaluate(layout, readings, 0.5)
    assert outcome["passed"]
    assert outcome["qc"]["blank_mean"] == pytest.approx(0.1)
    s1 = next(s for s in outcome["result"]["samples"] if s["sample_id"] == "S1")
    assert s1["wells"][0]["corrected"] == pytest.approx(0.5)
    assert s1["mean"] == pytest.approx(0.55)
    pc = outcome["result"]["positive_control"]
    assert pc["mean"] == pytest.approx(1.1)


def test_qc_fails_when_positive_below_floor():
    layout = parsing.parse_layout(GOOD_LAYOUT).layout
    readings = parsing.parse_readings(
        b"well,absorbance\nA1,0.1\nA2,0.6\nA3,0.7\nA4,1.0\nA5,1.1\nA6,0.4\n"
    ).readings
    outcome = qc.evaluate(layout, readings, 0.5)
    assert not outcome["passed"]
    assert outcome["result"] is None
    assert any(check["id"] == "positive_floor" and not check["ok"] for check in outcome["qc"]["checks"])


def test_qc_fails_when_replicate_missing_reading():
    layout = parsing.parse_layout(GOOD_LAYOUT).layout
    readings = parsing.parse_readings(
        b"well,absorbance\nA1,0.1\nA2,0.6\nA4,1.0\nA5,1.1\nA6,1.2\n"
    ).readings
    outcome = qc.evaluate(layout, readings, 0.5)
    assert not outcome["passed"]
    detail = next(c for c in outcome["qc"]["checks"] if c["id"] == "sample_replicates")
    assert "A3" in detail["detail"]


def test_qc_fails_when_three_replicates():
    data = b"""well,role,sample_id
A1,blank,
A2,sample,S1
A3,sample,S1
A4,sample,S1
A5,positive_control,
"""
    layout = parsing.parse_layout(data).layout
    readings = parsing.parse_readings(
        b"well,absorbance\nA1,0.1\nA2,0.6\nA3,0.7\nA4,0.8\nA5,1.2\n"
    ).readings
    outcome = qc.evaluate(layout, readings, 0.5)
    assert not outcome["passed"]


def test_qc_fails_without_blank():
    layout = parsing.parse_layout(
        b"well,role,sample_id\nA2,sample,S1\nA3,sample,S1\nA4,positive_control,\n"
    ).layout
    readings = parsing.parse_readings(
        b"well,absorbance\nA2,0.6\nA3,0.7\nA4,1.2\n"
    ).readings
    outcome = qc.evaluate(layout, readings, 0.5)
    assert not outcome["passed"]
    assert outcome["qc"]["blank_mean"] is None
