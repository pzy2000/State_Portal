import json
import math

from warptest.fuzz_llm import parse_json


def test_parse_json_clamps_non_finite_numbers_for_strict_json_roundtrip() -> None:
    parsed = parse_json(
        """
        {
          "target": {
            "pos": [Infinity, -Infinity, NaN],
            "nested": {"value": Infinity}
          }
        }
        """
    )

    assert parsed is not None
    values = parsed["target"]["pos"]
    assert values == [2_147_483_647.0, -2_147_483_647.0, 0.0]
    assert parsed["target"]["nested"]["value"] == 2_147_483_647.0
    assert all(math.isfinite(value) for value in values)
    json.dumps(parsed, allow_nan=False)
