"""Write the typed model to JSON and read it back, without a dictionary crossing a tool's interface.

An `Evaluation` is produced by one command and consumed by two others (`checklist`, `record`). The
file between them is JSON. `encode` and `decode` are the only functions that see the untyped form:
`decode` rebuilds the dataclasses from their own type annotations, so a field added to the model
needs no change here.
"""

from __future__ import annotations

import dataclasses
import json
import types
import typing
from datetime import datetime
from enum import Enum
from typing import Any, TypeVar

T = TypeVar("T")


def _plain(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {field.name: _plain(getattr(value, field.name)) for field in dataclasses.fields(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return value


def encode(value: Any) -> str:
    return json.dumps(_plain(value), indent=1, sort_keys=False) + "\n"


def _build(kind: Any, data: Any) -> Any:
    origin = typing.get_origin(kind)
    if origin in (typing.Union, types.UnionType):
        if data is None:
            return None
        (inner,) = [option for option in typing.get_args(kind) if option is not type(None)]
        return _build(inner, data)
    if origin is tuple:
        (inner, _) = typing.get_args(kind)
        return tuple(_build(inner, item) for item in data)
    if isinstance(kind, type) and dataclasses.is_dataclass(kind):
        hints = typing.get_type_hints(kind)
        return kind(**{field.name: _build(hints[field.name], data[field.name]) for field in dataclasses.fields(kind) if field.name in data})
    if isinstance(kind, type) and issubclass(kind, Enum):
        return kind(data)
    if kind is datetime:
        return datetime.fromisoformat(data)
    return data


def decode(kind: type[T], text: str) -> T:
    return _build(kind, json.loads(text))
