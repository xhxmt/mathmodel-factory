import json

import numpy as np
import pytest

from factory_core.json_values import dumps


def test_numpy_scalar_and_array_producer_values_are_real_json_types():
    output = json.loads(dumps({"ok": np.bool_(True), "count": np.int64(4),
        "value": np.float64(0.1), "array": np.array([1, 2]), "python_bool": True}))
    assert output == {"ok": True, "count": 4, "value": 0.1, "array": [1, 2], "python_bool": True}
    assert type(output["ok"]) is bool and type(output["count"]) is int


@pytest.mark.parametrize("value,error", [(np.float64(float("nan")), ValueError), (np.complex128(1j), TypeError)])
def test_invalid_numeric_output_reports_exact_field(value, error):
    with pytest.raises(error, match=r"\$\.result"):
        dumps({"result": value})
