# PerKan

A personal Kanban board with sprints. Flask, SQLite, one container. It has a web UI and an MCP server, so you and Claude work the same board.

## Run it

```bash
docker compose up --build -d      # http://localhost:5000 (set HOST_PORT to change)
```

Or without Docker: `pip install -r requirements.txt && python app.py`.

Data lives in `data/perkan.db`. To back it up, copy the whole `data` directory (the `-wal` and `-shm` files belong with it). The UI's Download/Import buttons also give a portable JSON backup.

Connect Claude Code: `claude mcp add --transport http perkan http://localhost:5000/mcp`

There is no authentication, so keep it on localhost or a trusted network. Requests with a foreign `Origin` header are rejected; `MCP_ALLOWED_ORIGINS` (comma-separated) allows others.

## Sprints

Columns are workflow status. A sprint is a time box that cards belong to, so there is one Done column. A card in no sprint is in the backlog. A few rules aren't obvious:

- **Only the active sprint's cards leave the first column.** A backlog card (or one in another sprint) stays in To Do until it joins the active sprint. Joining and moving in one update is fine.
- **Cards created past the first column join the active sprint.** Add a card straight to In Progress or Done and it lands in the sprint, flagged "added mid-sprint". Pass `sprint: "backlog"` to opt out. With no active sprint it stays in the backlog.
- **Sprints are never closed automatically.** One is active at a time; past its end date it shows "ended N days ago" until you complete it. Completing offers to carry unfinished cards to the next sprint, send them to the backlog, or leave them.
- **Done cards of a closed sprint are hidden** from the board and `list_cards`. `include_closed=true` or a `sprint` filter shows them.
- The board's **🏃 Current sprint** button hides everything outside the active sprint (your choice is remembered in the browser).
- `PERKAN_TZ` (default UTC) decides which day counts as today.

Moving from a weekly "Done m/d" column setup: `scripts/migrate_to_sprints.py` turns them into closed sprints (dry run unless `--apply`; back up first).

## Card order

Every card has one global `order` (unique, lowest first). Moving a card to another column keeps its `order`, so relative positions survive. `position` (index in the column, 0 = top) works on create and update and changes only that card's `order`. An explicit `order` wins over `position`.

## Dates and stats

Every column change is recorded (`GET /api/card/<id>/history`). From that, cards carry `created_at`, `started_at` (first move out of the starting column), `completed_at` (last entry into a done column; clears if the card leaves) and `column_since`.

`GET /api/stats` (MCP: `get_card_stats`) turns these into durations in days (backlog, work, lead, time in current column) with summaries overall and per project and sprint. Filters: `project`, `sprint`, `created_since`, `completed_since`.

A column counts as done when `is_done` is set on it. `column: "done"` in `create_card` / `update_card` means the one marked `is_current_done`.

## API and MCP

Every MCP tool calls the matching `/api/*` route, so behavior is the same as the UI. The tool descriptions list the arguments. Things worth knowing:

- Hidden columns (and their cards) are left out of `GET /api/board`, `/api/columns` and `/api/cards` unless `?include_hidden=true`. Export always includes everything.
- `GET /api/cards` returns `{cards, total}`; filters are `column`, `project`, `assignee`, `sprint`, `query`, paged with `limit` and `offset`.
- Projects can carry `repo_path`, `repo_url`, `notes` and `links` (Settings > Projects > Details, `/api/project`, MCP `create_project` / `update_project`). They are shown only when set. On update, a field you leave out is unchanged and an empty value clears it; `links` replaces the whole list. Merge-importing never blanks a project's fields.

## Tests

`python -m unittest discover -s tests -v` (needs Flask; uses a temporary data directory).
