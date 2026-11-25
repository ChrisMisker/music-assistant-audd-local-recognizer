"""Tests for running the full garbage collection in a silence (MA-independent)."""

from __future__ import annotations

from audd_local_recognizer.quiet_gc import QuietCollector


def test_collects_only_when_quiet_and_not_too_often() -> None:
    collector = QuietCollector(min_interval=120.0)
    assert not collector.due(quiet=False, now=1000.0)  # a source is in the music
    assert collector.due(quiet=True, now=1000.0)
    collector.collect(now=1000.0)
    assert not collector.due(quiet=True, now=1060.0)  # too soon after the last one
    assert collector.due(quiet=True, now=1120.0)
    collector.running = True
    assert not collector.due(quiet=True, now=2000.0)  # one at a time
