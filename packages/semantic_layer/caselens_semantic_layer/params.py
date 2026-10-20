"""Validate and normalize operation params per the tool contract and the YAML `param_types`.

Error messages name the param and the rule it broke but never echo the supplied value."""

import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from caselens_semantic_layer.layer import get_layer, get_tool_validator
from caselens_semantic_layer.models import OperationError

NormalizedValue = str | int | float | datetime
_PHONE_STRIP = re.compile(r"[\s.\-()]")
_PLATE_STRIP = re.compile(r"[\s\-]")


def _normalize_phone(value: str) -> str:
    digits = _PHONE_STRIP.sub("", value)
    if re.fullmatch(r"[0-9]{10}", digits):
        return "+1" + digits
    return digits


def _normalize_plate(value: str) -> str:
    return _PLATE_STRIP.sub("", value).upper()


def _parse_timestamp(name: str, value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        raise OperationError(f"param {name!r}: must be an ISO-8601 timestamp") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise OperationError(f"param {name!r}: timestamp must include a UTC offset")
    return parsed


def normalize_value(name: str, type_name: str, value: Any) -> NormalizedValue:
    """Normalize one scalar param value by its declared param type, then re-check `pattern`."""
    ptype = get_layer()["param_types"][type_name]
    base = ptype["base"]
    if base == "string" and not isinstance(value, str):
        raise OperationError(f"param {name!r}: must be a string")
    result: NormalizedValue
    if ptype.get("format") == "date-time":
        result = _parse_timestamp(name, value)
    elif base == "integer":
        try:
            result = int(str(value).strip())
        except ValueError:
            raise OperationError(f"param {name!r}: must be an integer") from None
    elif base == "number":
        try:
            result = float(str(value).strip())
        except ValueError:
            raise OperationError(f"param {name!r}: must be a number") from None
    elif type_name == "phone":
        result = _normalize_phone(value)
    elif type_name == "plate":
        result = _normalize_plate(value)
    else:
        result = value.strip()
    if isinstance(result, str):
        if not result:
            raise OperationError(f"param {name!r}: must not be empty")
        max_length = ptype.get("max_length")
        if max_length is not None and len(result) > max_length:
            raise OperationError(f"param {name!r}: must be at most {max_length} characters")
        pattern = ptype.get("pattern")
        if pattern is not None and re.fullmatch(pattern, result) is None:
            raise OperationError(f"param {name!r}: not a valid {type_name}")
    return result


def _schema_error_message(operation: str, params: Mapping[str, Any]) -> str | None:
    errors = sorted(
        get_tool_validator().iter_errors({"operation": operation, "params": dict(params)}),
        key=lambda e: list(e.absolute_path),
    )
    if not errors:
        return None
    err = errors[0]
    path = [str(p) for p in err.absolute_path]
    where = f"param {path[1]!r}" if len(path) >= 2 and path[0] == "params" else "params"
    if err.validator == "required":
        missing = [p for p in err.validator_value if p not in params]
        return f"{operation}: missing required param(s): {', '.join(sorted(missing))}"
    if err.validator == "additionalProperties":
        allowed = sorted(get_layer()["operations"][operation]["params"])
        return f"{operation}: unknown param; allowed params are {', '.join(allowed)}"
    rule = {
        "type": "has the wrong type",
        "minLength": "must not be empty",
        "maxLength": "is too long",
        "format": "has the wrong format",
    }.get(str(err.validator), "is invalid")
    return f"{operation}: {where} {rule}"


def validate_params(operation: str, params: Mapping[str, Any]) -> dict[str, Any]:
    """Validate params for `operation` and return them normalized (lists stay lists for `in`)."""
    layer = get_layer()
    operations = layer["operations"]
    if not isinstance(operation, str) or operation not in operations:
        raise OperationError(f"unknown operation; allowed operations are {', '.join(operations)}")
    if not isinstance(params, Mapping):
        raise OperationError(f"{operation}: params must be an object")
    message = _schema_error_message(operation, params)
    if message is not None:
        raise OperationError(message)
    declared = operations[operation]["params"]
    unknown = set(params) - set(declared)
    if unknown:
        raise OperationError(
            f"{operation}: unknown param; allowed params are {', '.join(declared)}"
        )
    normalized: dict[str, Any] = {}
    for name, spec in declared.items():
        if name not in params:
            if spec["required"]:
                raise OperationError(f"{operation}: missing required param(s): {name}")
            continue
        value = params[name]
        if isinstance(value, Sequence) and not isinstance(value, str):
            normalized[name] = [normalize_value(name, spec["type"], v) for v in value]
        else:
            normalized[name] = normalize_value(name, spec["type"], value)
    return normalized
