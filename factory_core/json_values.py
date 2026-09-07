"""Strict JSON conversion for numerical producer outputs, with field diagnostics."""
import json
import math


def json_value(value, path="$"):
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError(f"{path}: non-finite number is not valid result JSON")
        return value
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise TypeError(f"{path}: JSON object keys must be strings")
        return {key: json_value(item, f"{path}.{key}") for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(item, f"{path}[{index}]") for index, item in enumerate(value)]
    if type(value).__module__.split(".", 1)[0] == "numpy":
        import numpy as np
        if isinstance(value, np.ndarray):
            return json_value(value.tolist(), path)
        if isinstance(value, (np.bool_, np.integer, np.floating)):
            converted = value.item()
            if type(converted) is type(value):
                raise TypeError(f"{path}: extended-precision scalar requires an explicit decimal contract")
            return json_value(converted, path)
    raise TypeError(f"{path}: unsupported JSON result type {type(value).__name__}")


def dumps(value, **kwargs):
    kwargs["allow_nan"] = False
    return json.dumps(json_value(value), **kwargs)
