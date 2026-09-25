import pytest

from semcull.models import Intent, SemcullError, new_id, strict_json, validate_id


@pytest.mark.parametrize(
    "value",
    [
        None,
        {},
        {"question": "q", "expectations": {}},
        {"question": " ", "expectations": {"ok": "yes"}},
        {"question": "q", "expectations": {"ambiguous": "yes"}},
        {"question": "q", "expectations": {"Bad": "yes"}},
        {"question": "q", "expectations": {"x": " "}},
        {"question": "q", "expectations": {"x": "yes"}, "extra": 1},
    ],
)
def test_invalid_intents(value):
    with pytest.raises(SemcullError):
        Intent.parse(value)


def test_inline_equals_and_duplicate():
    assert Intent.inline("q", ["x=a=b"]).expectations == {"x": "a=b"}
    with pytest.raises(SemcullError):
        Intent.inline("q", ["x=a", "x=b"])


@pytest.mark.parametrize("value", ['{"x":1,"x":2}', '{"x":NaN}', '{"x":Infinity}'])
def test_strict_json(value):
    with pytest.raises(ValueError):
        strict_json(value)


def test_ids():
    value = new_id("obs")
    assert validate_id(value, "obs") == value
    for bad in ["../obs_x", "/tmp/obs_x", "obs_short", new_id("eval")]:
        with pytest.raises(SemcullError):
            validate_id(bad, "obs")
