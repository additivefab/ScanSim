"""Individual observable decoding keeps exact integers and a bounded inflater."""

import base64
import zlib

import numpy as np
import pytest

from zlscan.io.observable_arrays import decode_sample, encode_arrays


def test_single_sample_matches_original_integer_and_flag_vectors():
    values = {
        "capture_time": np.array([12, 13], np.int64),
        "capture_count": np.array([2**63 + 1, 2**63 + 2], dtype=object),
        "overflow": np.array([False, True]),
        "overflow_valid": np.array([True, True]),
    }
    encoded = encode_arrays(values)
    assert decode_sample(encoded, 1, 2) == {
        name: value[1].item() if hasattr(value[1], "item") else value[1]
        for name, value in values.items()
    }


def test_declared_scalar_cannot_expand_into_an_unbounded_blob():
    encoded = {
        "capture_time": {
            "kind": "i8",
            "count": 1,
            "data": base64.b64encode(zlib.compress(b"\0" * (1 << 20))).decode(),
        }
    }
    with pytest.raises(ValueError, match="layout"):
        decode_sample(encoded, 0, 1)


@pytest.mark.parametrize("count,index", [(0, 0), (2, -1), (2, 2), (1000001, 0)])
def test_sample_decode_budget_and_identity_are_explicit(count, index):
    with pytest.raises(ValueError):
        decode_sample({}, index, count)
