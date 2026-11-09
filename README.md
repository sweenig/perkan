# PerKan (Simple Flask Kanban)

A minimal web-based Kanban board powered by Flask and a SQLite database (`data/perkan.db`).

## Python

1. python -m venv venv
2. venv\Scripts\activate (Windows) or source venv/bin/activate
3. pip install --break-system-packages -r requirements.txt
4. python app.py

Open http://127.0.0.1:5000

## Docker

- Run with the local `data` directory (recommended — run from the `perkan` folder):

`docker run -p 5000:5000 -v "$(pwd)/data:/app/data" perkan`

- Docker Compose (recommended):

From the `perkan` directory:

`docker compose up --build -d`

The service runs Gunicorn with conservative settings (2 workers, 4 threads, 60s timeout).

## Data storage

The board lives in SQLite (`data/perkan.db`, WAL mode, standard library only). Every write is one exclusive transaction, so concurrent edits from the UI, the MCP server and multiple Gunicorn workers cannot overwrite each other.

On first start, if a legacy `data/kanban.json` exists it is imported once and renamed to `kanban.json.migrated` (kept as a backup; safe to delete once you are happy). With no data at all, a blank board is created. The UI's Download/Import buttons still read and write `kanban.json`-format files, so they work as a portable backup. Back up the database by copying the whole `data` directory (the `-wal` and `-shm` files belong with it).

## Card order

Every card has one global `order` value (unique, lowest first). A column shows its cards sorted by it, and the list view sorts all cards by it.

- Moving a card to another column (`PUT /api/card/<id>` with only `column`) keeps its `order`, so cards keep their relative position across columns.
- `position` (an index among the column's other cards, 0 = top) works on create and update, within the current column or while moving. It changes only that card's `order`, and only as far as needed to land between its new neighbours.
- An explicit `order` value is honoured (and nudged if another card already has it); it takes precedence over `position`.

## Reading cards

- `GET /api/board`, `GET /api/columns` and `GET /api/cards` leave out hidden columns and their cards unless `?include_hidden=true`. Export always contains everything.
- `GET /api/cards` returns `{cards, total}` in global order. Filters: `column`, `project`, `assignee`, `query`; paging: `limit`, `offset`. Naming a hidden column in `column` counts as asking for it. For example, the top card of To Do is `/api/cards?column=todo&limit=1`.

## Tests

`python -m unittest discover -s tests -v` (needs Flask; uses a temporary data directory).

## Card dates and history

Every column change is recorded in `card_history` (card, from column, to column, UTC time, source: `ui`, `mcp` or `api`). Creating a card records its first row. Cards carry two derived dates (omitted when empty):

- `started_at`: the first time the card was moved to another column.
- `completed_at`: when the card last entered a done column (`is_done` on the column) from a non-done one. Moving between done columns keeps it; moving out clears it, and re-entering sets a new one.

Cards also carry `created_at` (set once, when the card is created; never changed by moves) and `column_since` (when the card entered its current column). Cards that predate history have none of these dates.

`GET /api/card/<id>/history` returns the history, oldest first. Existing cards in done columns were backfilled from a `DONE yyyy-mm-dd` line in their description (history source `backfill`, no `started_at`). Columns titled "Done..." were flagged `is_done` on upgrade; toggle it per column in settings, or with `is_done` on `create_column` / `update_column`. Replace-mode import clears history for cards that no longer exist and keeps it for ids that survive.

`GET /api/stats` (MCP: `get_card_stats`) reports durations in days per card, with n / avg / median / min / max summaries overall and per project: `backlog_days` (created to started), `work_days` (started to completed), `lead_days` (created to completed), plus `in_column_days` and `open_by_column` for cards not yet done (for example how long cards have sat in Burn-In). Filters: `project`, `created_since`, `completed_since`; `cards=false` returns summaries only. Cards with no recorded dates are left out.

## Sprints

A sprint is a time box that cards belong to; columns stay the workflow status, so there is a single Done column. `cards.sprint_id` is null for the backlog. A sprint is `planned`, `active` (only one at a time) or `closed`, and ends at the end of its `end_date`. An active sprint past its end stays active and shows "ended N days ago"; nothing closes it automatically. `PERKAN_TZ` (default UTC) sets which day counts as today.

- `/sprints` is the planning page: the active sprint on top, planned sprints below, then the backlog; closed sprints on request. Drag cards between sections and reorder them. New sprints take an end date from a date picker.
- Completing a sprint (UI dialog, `POST /api/sprint/<id>/complete`, `complete_sprint`) handles unfinished cards (not in a done column): `carry_over` to the next sprint keeping their column (an existing planned sprint, or a new one), `backlog`, or `leave`. With nothing unfinished the dialog only asks for confirmation. Committed (the cards present when the sprint started) / added (cards that joined after it started) / completed / carried are snapshotted on the sprint. A card added to the active sprint after it started carries `added_after_start` and `added_to_sprint_at` in every card payload, shown as a badge on the board and the sprints page; `get_card_stats` marks such cards and reports `added` per sprint.
- Done cards of a closed sprint are hidden from the board and `list_cards` (they stay in the Done column); `include_closed=true` or a `sprint` filter shows them.
- Cards take `sprint` (an id, `"active"` or `"backlog"`) on `create_card` / `update_card`; `list_cards` and `get_card_stats` filter by `sprint`, and stats include `by_sprint`. Sprint moves are recorded in `card_sprint_history`.
- Sprints are part of board export and replace-mode import.
- `column: "done"` (or `"current_done"`) on `create_card` / `update_card` means the column marked `is_current_done` (set it with `update_column`; only one column holds it).
- `scripts/migrate_to_sprints.py` converts weekly "Done m/d" columns into closed sprints and opens a first active sprint (dry run unless `--apply`; back up the database first).

## MCP server

PerKan exposes an [MCP](https://modelcontextprotocol.io) server at `POST /mcp` (Streamable HTTP, stateless, JSON responses). It shares the web app's process and data, and like the rest of the app it has no authentication.

Tools: `get_board`, `list_cards` (with `limit`/`offset`), `create_card`, `update_card` (also moves cards), `get_card_history`, `get_card_stats`, `get_card_sprint_history`, `delete_card`, `list_columns`, `create_column`, `update_column`, `delete_column`, `list_sprints`, `create_sprint`, `update_sprint`, `delete_sprint`, `start_sprint`, `complete_sprint`, `list_projects`, `create_project`, `update_project`, `delete_project`. Each calls the matching `/api/*` route, so behavior is identical to the UI.

Connect from Claude Code:

`claude mcp add --transport http perkan http://localhost:5000/mcp`

Browser requests with a foreign `Origin` header are rejected (DNS-rebinding protection); set `MCP_ALLOWED_ORIGINS` to a comma-separated list to allow others.
