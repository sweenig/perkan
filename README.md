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
- **Meeting cards.** Set `meeting: true` on `create_card`/`update_card` (or tick "Came from a meeting" in the card editor) to mark a card that came from a calendar meeting. It gets a 📅 badge, the board's three-way **📅 meetings** button (remembered, combines with Current sprint) cycles all → only meetings → all → hide meetings (left half filled = only meetings, right half = hide meetings, whole button = all), and `list_cards` takes `meeting=true|false`.
- `PERKAN_TZ` (default UTC) decides which day counts as today. Put it in `.env` (for example `PERKAN_TZ=America/Chicago`); docker-compose passes it to the container only when it is set.
- **Burn-up** (`/burnup`, `GET /api/sprint/<id|active>/burnup?step=hour|day`) plots the cumulative minutes a sprint's cards spent in columns marked Counts as work, across the sprint's dates. It is wall-clock time, counted only while the card was in that sprint (card sprint history) and inside the sprint's dates; cards in progress together add up. The minutes are split by how the card came into the sprint: **planned** (in the sprint when it was started) or **added** after it started (the same rule as the board's added-mid-sprint badge). The chart stacks the two (added is the hatched band on top, with a ▲ where each card came in) and the totals always add up; every point and the response carry `planned_minutes` and `added_minutes`, and `additions` lists the added cards. A card with no recorded column history is listed as unknown, not as 0; a card in the sprint with no sprint history counts for the whole sprint and is flagged. `PERKAN_TZ` also sets where each day starts and the times shown. **Working time** (Settings > Working time, `GET/PUT /api/settings/calendar`, `POST /api/settings/holidays`, `DELETE /api/settings/holidays/<date>`): per-weekday on/off and hours, plus holidays (one `yyyy-mm-dd [label]` per line to import). The burn-up's Hide non-working time toggle (or `?hide_non_working=true|false`, default from the saved setting) leaves those days, and in the hourly view the hours outside working hours, off the x-axis. Time tracked in hidden stretches is not dropped: it lands in the next shown point, so totals are identical either way.

Moving from a weekly "Done m/d" column setup: `scripts/migrate_to_sprints.py` turns them into closed sprints (dry run unless `--apply`; back up first).

## Card order

Every card has one global `order` (unique, lowest first). Moving a card to another column keeps its `order`, so relative positions survive. `position` (index in the column, 0 = top) works on create and update and changes only that card's `order`. An explicit `order` wins over `position`.

## Dates and stats

Every column change is recorded (`GET /api/card/<id>/history`). From that, cards carry `created_at`, `started_at` (first move out of the starting column), `completed_at` (last entry into a done column; clears if the card leaves) and `column_since`.

The ⏱ on a card is its total time worked: `work_minutes` on every board card, the wall-clock time spent in columns marked Counts as work (the open stretch counts up to now). The server computes it so auto-refresh repaints it as it grows; cards with no recorded column history show no ⏱. Time in the current column is in the card's history (`column_since`).

`GET /api/stats` (MCP: `get_card_stats`) turns these into durations in days (backlog, work, lead, time in current column) with summaries overall and per project and sprint. Filters: `project`, `sprint`, `created_since`, `completed_since`.

A column counts as done when `is_done` is set on it. `column: "done"` in `create_card` / `update_card` means the one marked `is_current_done`.

## API and MCP

Every MCP tool calls the matching `/api/*` route, so behavior is the same as the UI. The tool descriptions list the arguments. Things worth knowing:

- Hidden columns (and their cards) are left out of `GET /api/board`, `/api/columns` and `/api/cards` unless `?include_hidden=true`. Export always includes everything.
- `GET /api/cards` returns `{cards, total}`; filters are `column`, `project`, `assignee`, `sprint`, `query`, paged with `limit` and `offset`.
- Projects can carry `repo_path`, `repo_url`, `notes` and `links` (Settings > Projects, pencil icon; `/api/project`, MCP `create_project` / `update_project`). They are shown only when set. On update, a field you leave out is unchanged and an empty value clears it; `links` replaces the whole list. Merge-importing never blanks a project's fields. Projects have no order of their own: they are listed alphabetically, and `/api/project/<n>` counts in that order.
- `GET /api/card/<id>/history` (MCP `get_card_history`) also returns `time_in_columns`: wall-clock minutes the card spent in each column it has been in, repeated stays summed and the current column counted up to now (the card edit modal shows it). Cards with no recorded column history report `known: false`, not zeros.
- `POST /api/card/<id>/notes` (MCP `add_card_note`, body `{text}`) appends a timestamped progress note without touching the description and returns just the note. Notes are listed under `notes` in `GET /api/card/<id>/history` and shown in the card edit modal; they cannot be edited or deleted.
- Columns can be **stacked** (`stacked`): visible stacked columns that sit next to each other share one slot on the board and split its height equally. They can also be marked **Counts as work** (`counts_as_work`), which is what the burn-up counts as time spent. Both are in Settings > Statuses, `create_column` / `update_column` and export/import.

## Tests

`python -m unittest discover -s tests -v` (needs Flask; uses a temporary data directory).
