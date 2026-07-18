"""Ordered mutation state used while analyzing one guarded tool call."""
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class ReplacementMutation:
    """One replace operation in the order supplied by the editing tool."""

    paths: tuple
    old: str
    new: str
    replace_all: bool = False


@dataclass(frozen=True)
class PatchMutation:
    """One theory-targeting added hunk normalized from an apply_patch envelope."""

    path: str
    added_text: str
    before_text: str
    context_text: str
    changed_ranges: tuple


class VirtualFileStore:
    """Lazily loaded per-path snapshots shared by an ordered mutation batch."""

    def __init__(self, reader):
        self._reader = reader
        self._snapshots = {}
        self._originals = {}
        self._paths = {}

    @staticmethod
    def _key(path):
        return os.path.normcase(os.path.abspath(path))

    def read(self, paths):
        for path in paths:
            if not isinstance(path, str):
                continue
            key = self._key(path)
            if key in self._snapshots:
                return path, self._snapshots[key]
        path, content = self._reader(paths)
        if path is not None and content is not None:
            key = self._key(path)
            self._snapshots[key] = content
            self._originals.setdefault(key, content)
            self._paths[key] = path
        return path, content

    def write(self, path, content):
        if isinstance(path, str) and isinstance(content, str):
            key = self._key(path)
            self._originals.setdefault(key, self._snapshots.get(key))
            self._snapshots[key] = content
            self._paths[key] = path

    def changes(self):
        """Return path -> (original, current) for snapshots changed by this call."""
        return {
            self._paths[key]: (self._originals.get(key), current)
            for key, current in self._snapshots.items()
            if self._originals.get(key) != current
        }
