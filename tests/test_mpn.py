import pytest

from app.mpn import normalise_mpn


def test_strips_punctuation_and_uppercases():
    assert normalise_mpn("RC0402JR-7D4K7L") == "RC0402JR7D4K7L"
    assert normalise_mpn("RC0402JR-074K7L") == "RC0402JR074K7L"


def test_punctuation_only_difference_is_same_part():
    assert normalise_mpn("rc0402jr-7d4k7l") == normalise_mpn("RC-0402-JR-7D4-K7L")


def test_digit_difference_is_different_part():
    assert normalise_mpn("RC0402JR-7D4K7L") != normalise_mpn("RC0402JR-074K7L")


def test_is_idempotent():
    samples = ["RC0402JR-7D4K7L", "  lm358n ", "X7R 10uF 25V"]
    for s in samples:
        assert normalise_mpn(normalise_mpn(s)) == normalise_mpn(s)


@pytest.mark.parametrize("s", ["", "   ", "\t\n", "---", "!!!"])
def test_empty_or_whitespace_is_rejected(s):
    with pytest.raises(ValueError):
        normalise_mpn(s)
