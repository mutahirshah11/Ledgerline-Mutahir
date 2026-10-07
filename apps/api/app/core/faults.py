"""Fault injection (Section 25, research R4).

`active_fault` reads the `fault` query parameter and returns it only when `ALLOW_FAULTS=true`;
otherwise it is ignored. With faults on, an unknown name is 400 `BAD_REQUEST`. The fault is then
passed explicitly into the stage service; there is no global fault state.
"""

from typing import Annotated

from fastapi import Query

from app.core.config import get_settings
from app.core.errors import ApiError
from app.domain.enums import ErrorCode, Fault

__all__ = ["Fault", "active_fault"]


def active_fault(fault: Annotated[str | None, Query()] = None) -> Fault | None:
    """FastAPI dependency: the requested `Fault`, or None when faults are off or none is asked."""
    if fault is None or not get_settings().ALLOW_FAULTS:
        return None
    try:
        return Fault(fault)
    except ValueError:
        raise ApiError(
            ErrorCode.BAD_REQUEST,
            400,
            "Unknown fault",
            details={"fault": fault, "allowed": [f.value for f in Fault]},
        ) from None
