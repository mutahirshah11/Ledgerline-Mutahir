"""F-04 acceptance criteria (blueprint Section 16; spec US1). The pack is parsed once per module."""

from datetime import date

from app.pipeline.normalise import excel_serial_to_date


def test_F04_AC5_serial_46292() -> None:
    assert excel_serial_to_date(46292) == date(2026, 9, 27)
    assert excel_serial_to_date(46266) == date(2026, 9, 1)
