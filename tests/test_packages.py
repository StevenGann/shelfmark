import pytest

from app.packages import normalise_package


@pytest.mark.parametrize(
    ("package", "expected"),
    [
        ("0402 (1005 Metric)", "0402"),
        ("0201", "0201"),
        ("0603 (1608 Metric)", "0603"),
        ("0805 (2012 Metric)", "0805"),
        ("1206 (3216 Metric)", "1206"),
        ("1210 (3225 Metric)", "1210"),
        ("1812 (4532 Metric)", "1812"),
        ("2512 (6432 Metric)", "2512"),
        ("40-VFQFN Exposed Pad", "VQFN-40"),
        ('8-SOIC (0.154", 3.90mm Width)', "SOIC-8"),
    ],
)
def test_package_normalisation(package, expected):
    assert normalise_package(package) == expected


@pytest.mark.parametrize(
    "package",
    [
        "SOT-23",
        "TO-252 (DPAK)",
        "through-hole",
        "",
    ],
)
def test_unknown_package_is_unchanged(package):
    assert normalise_package(package) == package
