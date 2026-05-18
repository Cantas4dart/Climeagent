`pyapp` is the beginning of the TypeScript `app/` migration into Python.

This first step intentionally keeps scope small:
- `db.py` mirrors the current SQLite manager shape
- `secrets.py` mirrors the wallet secret encryption helpers
- `singleton.py` mirrors the lockfile utility

The trading logic is not changed here. This is just the Python foundation we can build on next.
