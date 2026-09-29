"""SQLite repositories (Prompt Pack v8, prompt #030), area X2 Persistence.

One repository per aggregate. Each takes an open connection from
``Database.transaction()`` and never commits on its own.

- Immutable records only have ``add``: no update and no delete.
- Entities that change have ``add`` and ``update``. ``update`` takes the value
  the caller last read (``expected_updated_at``, ``expected_version`` or
  ``expected_status``) and raises ``ConcurrencyError`` if the stored row has
  changed since, so no update is lost. It writes only the columns that can
  change.
- No repository deletes anything.

Datetimes and money are written through ``core/db/codec.py``.
"""
