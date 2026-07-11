"""Capability primitives shared by Maestro resources."""

from typing import Annotated

from pydantic import Field

CapabilityName = Annotated[
    str,
    Field(min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9.\-]*$"),
]
