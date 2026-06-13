---
name: TAC dataset workflow
description: How to seed and refresh the local TAC->brand/model lookup table
type: reference
---

`TacLookup` (catalog app, PK = 8-digit TAC) is the source of truth for `IMEI -> brand+model`.
Optional online fallback (ImeiCheck via `IMEI_ONLINE_LOOKUP_ENABLED=1`) is consulted only on a
local miss, and any failure silently falls back to manual model entry.

Refresh commands:
- `python manage.py seed_tac --builtin` — loads the small curated list of popular Apple/Samsung/
  Xiaomi/Pixel TACs hardcoded inside `apps/catalog/management/commands/seed_tac.py` (this is
  what the entrypoint runs on first boot).
- `python manage.py seed_tac --file <path>.csv` or `.json` — bulk load from a real dataset.
  CSV columns expected: `tac, brand, model, [device_type]`. JSON is a list of dicts with the
  same keys.
- Add `--truncate` to wipe existing rows first.

Recommended public datasets:
- https://tacdb.osmocom.org/ (Osmocom community TAC DB)
- https://github.com/MoazEb/tac-database (GitHub mirror, CSV/JSON dumps)

How to apply: when the user wants better TAC coverage, fetch one of those dumps, normalize the
columns, and run `seed_tac --file ...`. Do not hard-code anything in `BUILTIN_TAC` beyond the
~17 demo entries that are there now — they are only for empty-DB startup.
