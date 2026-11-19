"""Tests for the source selection, including the move of the Sendspin input (server #7909)."""

from __future__ import annotations

from audd_local_recognizer.sources import selected_match, source_key

TURNTABLE = "client-abc"
OLD = source_key("sendspin_source", TURNTABLE)


def test_exact_selection_matches() -> None:
    """A source selected under its current provider is recognized."""
    selected = {source_key("sendspin", TURNTABLE)}
    assert selected_match(selected, "sendspin", TURNTABLE, {"sendspin"}) == source_key(
        "sendspin", TURNTABLE
    )


def test_selection_survives_the_provider_move() -> None:
    """A selection made under the retired sendspin_source plugin still covers the same input."""
    assert selected_match({OLD}, "sendspin", TURNTABLE, {"sendspin", "vban_receiver"}) == OLD


def test_selection_of_a_loaded_provider_is_not_reused() -> None:
    """While the selected provider still exists, its selection does not cover another one."""
    loaded = {"sendspin", "sendspin_source"}
    assert selected_match({OLD}, "sendspin", TURNTABLE, loaded) is None


def test_other_inputs_are_not_matched() -> None:
    """Another item id, an empty or a malformed selection never matches."""
    assert selected_match({OLD}, "sendspin", "client-other", {"sendspin"}) is None
    assert selected_match(set(), "sendspin", TURNTABLE, {"sendspin"}) is None
    assert selected_match({TURNTABLE}, "sendspin", TURNTABLE, {"sendspin"}) is None
