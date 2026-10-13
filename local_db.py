"""Keyless, in-memory workspace used when no Supabase project is configured.

It mimics the tiny slice of the supabase-py query builder that app.py uses
(table().select/insert/update/delete().eq().order().execute()), so the whole app
works end-to-end with zero credentials: guests get a private workspace that lives
in their browser session. Export to CSV to keep your data.
"""
from __future__ import annotations

import copy
import uuid
from datetime import datetime, timezone


class _Response:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, store: dict, table: str):
        self._rows = store.setdefault(table, [])
        self._op = "select"
        self._payload = None
        self._filters: list[tuple[str, str]] = []
        self._order = None

    def select(self, *_args, **_kwargs):
        self._op = "select"
        return self

    def insert(self, payload):
        self._op, self._payload = "insert", payload
        return self

    def update(self, payload):
        self._op, self._payload = "update", payload
        return self

    def delete(self):
        self._op = "delete"
        return self

    def eq(self, column, value):
        self._filters.append((column, str(value)))
        return self

    def order(self, column, desc=False, **_kwargs):
        self._order = (column, bool(desc))
        return self

    def _matches(self, row):
        return all(str(row.get(c)) == v for c, v in self._filters)

    def execute(self):
        now = datetime.now(timezone.utc).isoformat()
        if self._op == "insert":
            items = self._payload if isinstance(self._payload, list) else [self._payload]
            created = []
            for item in items:
                row = copy.deepcopy(item)
                row.setdefault("id", str(uuid.uuid4()))
                row.setdefault("created_at", now)
                self._rows.append(row)
                created.append(copy.deepcopy(row))
            return _Response(created)
        matched = [r for r in self._rows if self._matches(r)]
        if self._op == "update":
            for row in matched:
                row.update(copy.deepcopy(self._payload))
            return _Response(copy.deepcopy(matched))
        if self._op == "delete":
            for row in matched:
                self._rows.remove(row)
            return _Response(copy.deepcopy(matched))
        data = copy.deepcopy(matched)
        if self._order:
            column, desc = self._order
            data.sort(key=lambda r: str(r.get(column) or ""), reverse=desc)
        return _Response(data)


class LocalClient:
    """Drop-in stand-in for the Supabase client (guest workspace)."""

    def __init__(self, store: dict | None = None):
        self.store = store if store is not None else {}

    def table(self, name: str) -> _Query:
        return _Query(self.store, name)
