"""Checks of the provider's files that need neither the server nor its packages (run everywhere)."""

import json
import re
from pathlib import Path
from typing import Any

from tests import DOMAIN, REPOSITORY

PROVIDER = REPOSITORY / DOMAIN


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _keys(texts: Any, prefix: str = "") -> set[str]:
    if not isinstance(texts, dict):
        return {prefix}
    return {key for name, value in texts.items() for key in _keys(value, f"{prefix}{name}.")}


def test_manifest_basics() -> None:
    manifest = _json(PROVIDER / "manifest.json")
    assert manifest["domain"] == DOMAIN
    assert manifest["type"] == "audio_analysis"
    assert manifest["codeowners"]
    assert manifest["requirements"] == ["audalign==1.3.2", "soxr==1.1.0"]
    # the manifest must disclose that audio is sent to AudD (catalog rule: no undisclosed behaviour)
    assert "AudD" in manifest["description"]
    assert _json(PROVIDER / "strings.json")["manifest"]["description"] == manifest["description"]


def test_every_setting_has_texts() -> None:
    """Each config entry key in provider.py has a label in strings.json and the translations."""
    source = (PROVIDER / "provider.py").read_text(encoding="utf-8")
    keys = set(re.findall(r'^CONF_[A-Z_]+ = "([a-z_]+)"', source, re.MULTILINE))
    assert keys
    english = _json(PROVIDER / "strings.json")["config_entries"]
    assert set(english) == keys
    for path in (PROVIDER / "translations").glob("*.json"):
        translated = _json(path)
        assert _keys(translated) <= _keys(_json(PROVIDER / "strings.json")), path.name
        assert set(translated["config_entries"]) == keys, path.name


def test_icons_are_small_svgs() -> None:
    for name in ("icon.svg", "icon_dark.svg"):
        content = (PROVIDER / name).read_text(encoding="utf-8")
        assert content.lstrip().startswith("<svg")
        assert len(content.encode()) <= 5 * 1024
        assert "<script" not in content
        assert "href" not in content


def test_ci_checks_this_provider() -> None:
    ci = (REPOSITORY / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert f"folder: {DOMAIN}\n" in ci
    assert "server-line: nightly\n" in ci


def test_all_sources_is_off_by_default() -> None:
    """MA maintainers' advice: users choose the sources to analyse; 'all' is opt-in."""
    source = (PROVIDER / "provider.py").read_text(encoding="utf-8")
    entry = re.search(r"key=CONF_ALL_AUDIO_SOURCES,(.*?)label=", source, re.DOTALL)
    assert entry is not None
    assert "default_value=False" in entry.group(1)
