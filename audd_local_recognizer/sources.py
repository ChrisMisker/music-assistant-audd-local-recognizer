"""Which live sources the user selected (MA-independent, so it can be tested without MA)."""

from __future__ import annotations

from collections.abc import Collection

SEPARATOR = "|"


def source_key(provider_instance: str, item_id: str) -> str:
    """Return the stored value for one source: '<provider instance>|<item id>'."""
    return f"{provider_instance}{SEPARATOR}{item_id}"


def selected_match(
    selected: Collection[str],
    provider_instance: str,
    item_id: str,
    loaded_instances: Collection[str],
) -> str | None:
    """
    Return the selected value that covers this source, or None.

    An exact match wins. Otherwise a selection whose provider instance no longer exists still
    counts when its item id matches: Music Assistant moved the Sendspin input from the separate
    `sendspin_source` plugin into the Sendspin provider (server #7909, Nov 2026), so the same
    input now comes from another provider instance and the stored selection would silently stop
    matching. A selection that names a provider which is still loaded is never reused for
    another provider.
    """
    exact = source_key(provider_instance, item_id)
    if exact in selected:
        return exact
    for value in sorted(selected):
        instance, separator, selected_item = value.partition(SEPARATOR)
        if separator and selected_item == item_id and instance not in loaded_instances:
            return value
    return None
