import pytest

from app.fingerprint import compute_fingerprint, similarity
from app.reader import read_document
from app.registry import MatchKind, Registry
from samples.generate import generate_all
from samples.sample_data import ALL_SAMPLES, PASSWORDS
from app.fingerprint import _normalise


@pytest.mark.parametrize("a, b", [
    ("Cost (INR)", "Cost (USD)"),
    ("Amount (INR)", "Amount (EUR)"),
    ("All amounts in INR", "All amounts in USD"),
    ("Value (Rs.)", "Value (INR)"),
])
def test_currency_does_not_change_labels(a, b):
    assert _normalise(a) == _normalise(b)


def test_currency_stripping_keeps_real_words():
    assert _normalise("Cost (INR)") == "cost"
    assert _normalise("Currency") == "currency"      # the word itself is a stable key
    assert _normalise("Total Return") == "total return"


@pytest.fixture(scope="module")
def fps(tmp_path_factory):
    root = tmp_path_factory.mktemp("samples")
    generate_all(root)
    return {name: compute_fingerprint(read_document(root / "pdfs" / f"{name}.pdf",
                                                    PASSWORDS.get(name)))
            for name in ALL_SAMPLES}


def test_fingerprint_ignores_monthly_data(fps):
    labels = fps["abc_pms"].labels
    assert "opening value" in labels
    assert "security | isin | qty | cost | mkt price | mkt value | weight %" in labels
    assert not any("infosys" in l for l in labels)       # holdings are monthly
    assert not any("ab1234" in l for l in labels)        # client id is not a label


def test_different_providers_have_different_fingerprints(fps):
    names = list(fps)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            assert fps[a].digest != fps[b].digest
            assert similarity(fps[a].labels, fps[b].labels) < 0.5, (a, b)


def test_known_format_matches(fps):
    reg = Registry()
    reg.add_format("abc_pms", fps["abc_pms"])
    result = reg.match(fps["abc_pms"])
    assert result.kind == MatchKind.MATCH and result.format_name == "abc_pms"


def test_unknown_format_is_unknown(fps):
    reg = Registry()
    reg.add_format("abc_pms", fps["abc_pms"])
    assert reg.match(fps["helix_aif"]).kind == MatchKind.UNKNOWN


def test_each_sample_matches_only_itself(fps):
    reg = Registry()
    for name, fp in fps.items():
        reg.add_format(name, fp)
    for name, fp in fps.items():
        r = reg.match(fp)
        assert r.kind == MatchKind.MATCH and r.format_name == name


def test_renamed_column_is_drift_not_match(fps):
    reg = Registry()
    reg.add_format("abc_pms", fps["abc_pms"])
    changed = set(fps["abc_pms"].labels)
    changed.discard("security | isin | qty | cost | mkt price | mkt value | weight %")
    changed.add("security | isin | units | cost | price | value | weight %")
    from app.fingerprint import Fingerprint
    r = reg.match(Fingerprint(frozenset(changed), "x"))
    assert r.kind in (MatchKind.MATCH, MatchKind.DRIFT)
    assert r.format_name == "abc_pms" and r.score < 1.0


def test_provider_title_is_one_label(fps):
    assert "abc portfolio managers" in fps["abc_pms"].labels


def test_strategy_name_is_not_in_fingerprint(fps):
    assert not any("large cap" in l for l in fps["abc_pms"].labels)