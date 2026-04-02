from collections.abc import Mapping
from typing import Any, TypeAlias, cast

JSONScalar: TypeAlias = str | int | float | bool | None
JSONValue: TypeAlias = JSONScalar | list["JSONValue"] | dict[str, "JSONValue"]
JSONObject: TypeAlias = dict[str, JSONValue]


def object_dict(value: object | None) -> dict[str, Any]:
    if isinstance(value, dict):
        return cast(dict[str, Any], value)
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    return {}


def mapping_dict(value: Mapping[object, object] | None) -> dict[str, Any]:
    if value is None:
        return {}
    return {str(key): item for key, item in value.items()}


def object_list(value: object | None) -> list[Any]:
    if isinstance(value, list):
        return value
    return []
