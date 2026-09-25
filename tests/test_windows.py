import pytest

from semcull.models import SemcullError
from semcull.windows import coverage, merge, parse_range, parse_selector, subtract, trim_utf8


def test_interval_union_and_gaps():
    ranges = [(10, 20), (0, 5), (4, 12), (40, 45)]
    assert merge(ranges) == [(0, 20), (40, 45)]
    assert subtract((0, 50), ranges) == [(20, 40), (45, 50)]
    assert coverage(50, ranges)["evaluated_bytes"] == 25
    assert subtract((10, 15), [(0, 100)]) == []
    assert subtract((10, 15), [(0, 5), (20, 30)]) == [(10, 15)]


def test_range_parsing():
    assert parse_range("0:0") == (0, 0)
    assert parse_range("1:1", lines=True) == (1, 1)
    assert parse_selector("lines:2-7") == ("lines", (2, 7))
    for value in ["remaining", "lines:4-2", "lines:0-1", "lines:1-2,3-4"]:
        with pytest.raises(SemcullError):
            parse_selector(value)


def test_utf8_cut_edges():
    data = "a🙂z".encode()
    assert trim_utf8(data[:3], 0, tail=False) == (0, b"a")
    assert trim_utf8(data[3:], 3, tail=True) == (5, b"z")
