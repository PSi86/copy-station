"""One persisted, self-healing overlay for ALL runtime-mutable user settings.

A few choices must survive a restart WITHOUT rewriting the user's commented
``config.yaml``: whether auto-transcode is on, the default transcode preset, and
whether the WiFi access point is up. They all live in a **single** JSON overlay
file under ``/var/lib/copystation`` (``user_settings_file``) that **wins over the
config defaults** when present -- the config value is only the factory default.
The daemon runs as root, so it can write there.

The file is organised into named sections (one per feature)::

    {"transcode": {"auto_transcode": true, "default_preset": "720p-h264"},
     "wifi_ap":   {"enabled": true}}

:class:`SettingsStore` owns the file and hands each feature a :class:`_Section`
view (``has``/``get``/``update``/``as_dict``) scoped to its section, so the
consumers stay simple while there is exactly **one** file and one in-memory copy
(no two-writers-one-file races).

It is **robust across software updates**: on load it keeps only the sections and
keys it currently knows about (see ``USER_SETTINGS_SCHEMA``), dropping anything a
later version renamed or removed and re-saving the cleaned file -- the
``config.yaml`` is never touched. A missing, corrupt or non-object file reads as
empty. Writes are atomic (temp file + replace). Only *key names* are validated
here (they change across releases); value validation stays with each feature.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any, Iterable, Mapping

_LOG = logging.getLogger("copystation.settings")

# The single source of truth for which runtime settings may be persisted. Each
# feature owns one section; keys not listed here are dropped from the overlay on
# load (so a config-key rename/removal in an update self-heals).
USER_SETTINGS_SCHEMA: dict[str, tuple[str, ...]] = {
    "transcode": ("auto_transcode", "default_preset"),
    "wifi_ap": ("enabled",),
}

DEFAULT_USER_SETTINGS_FILE = "/var/lib/copystation/user-settings.json"


class _Section:
    """A view of one section of a :class:`SettingsStore` (fixed section name)."""

    __slots__ = ("_store", "_name")

    def __init__(self, store: "SettingsStore", name: str) -> None:
        self._store = store
        self._name = name

    def has(self, key: str) -> bool:
        return self._store._has(self._name, key)

    def get(self, key: str, default: Any = None) -> Any:
        return self._store._get(self._name, key, default)

    def as_dict(self) -> dict[str, Any]:
        return self._store._section_dict(self._name)

    def update(self, **values: Any) -> dict[str, Any]:
        return self._store._update(self._name, values)


class SettingsStore:
    """A sectioned JSON overlay of the allowed runtime settings, pruned on load."""

    def __init__(self, path: Any,
                 schema: Mapping[str, Iterable[str]] = USER_SETTINGS_SCHEMA) -> None:
        self._path = Path(path)
        self._schema = {s: frozenset(keys) for s, keys in schema.items()}
        self._lock = threading.Lock()
        self._data: dict[str, dict[str, Any]] = {}
        self.reload()

    def section(self, name: str) -> _Section:
        """A :class:`_Section` view for feature ``name`` (must be in the schema)."""
        if name not in self._schema:
            raise KeyError(f"unknown settings section {name!r}")
        return _Section(self, name)

    def reload(self) -> None:
        """(Re)load the file, dropping unknown sections/keys and re-saving if so."""
        clean, dropped = self._prune(self._read())
        with self._lock:
            self._data = clean
        if dropped:
            _LOG.info(
                "Dropped unknown user-setting(s) %s from %s (config keys changed in "
                "an update?)", dropped, self._path,
            )
            self._write(clean)

    def as_dict(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {s: dict(kv) for s, kv in self._data.items()}

    # ----- section operations (used via _Section) -------------------------------

    def _has(self, section: str, key: str) -> bool:
        with self._lock:
            return key in self._data.get(section, {})

    def _get(self, section: str, key: str, default: Any) -> Any:
        with self._lock:
            return self._data.get(section, {}).get(key, default)

    def _section_dict(self, section: str) -> dict[str, Any]:
        with self._lock:
            return dict(self._data.get(section, {}))

    def _update(self, section: str, values: Mapping[str, Any]) -> dict[str, Any]:
        allowed = self._schema.get(section, frozenset())
        with self._lock:
            current = self._data.setdefault(section, {})
            for key, value in values.items():
                if key in allowed:
                    current[key] = value
                else:  # pragma: no cover - guards against caller typos
                    _LOG.warning("Ignoring unknown setting %s.%s for %s",
                                 section, key, self._path)
            snapshot = {s: dict(kv) for s, kv in self._data.items()}
            result = dict(current)
        self._write(snapshot)
        return result

    # ----- pruning + persistence ------------------------------------------------

    def _prune(self, raw: Any) -> tuple[dict[str, dict[str, Any]], list[str]]:
        """Keep only known sections/keys; return ``(clean, dropped-labels)``."""
        if not isinstance(raw, dict):
            return {}, []
        clean: dict[str, dict[str, Any]] = {}
        dropped: list[str] = []
        for section, keys in raw.items():
            allowed = self._schema.get(section)
            if allowed is None or not isinstance(keys, dict):
                dropped.append(str(section))  # unknown / malformed whole section
                continue
            kept = {k: v for k, v in keys.items() if k in allowed}
            dropped += [f"{section}.{k}" for k in keys if k not in allowed]
            if kept:
                clean[section] = kept
        return clean, sorted(dropped)

    def _read(self) -> Any:
        try:
            return json.loads(self._path.read_text())
        except (OSError, ValueError):
            return {}

    def _write(self, data: dict[str, dict[str, Any]]) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
            tmp.replace(self._path)
        except OSError as exc:  # pragma: no cover - best effort
            _LOG.warning("Could not persist settings %s: %s", self._path, exc)
