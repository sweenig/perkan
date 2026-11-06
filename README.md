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

## MCP server

PerKan exposes an [MCP](https://modelcontextprotocol.io) server at `POST /mcp` (Streamable HTTP, stateless, JSON responses). It shares the web app's process and data, and like the rest of the app it has no authentication.

Tools: `get_board`, `list_cards` (with `limit`/`offset`), `create_card`, `update_card` (also moves cards), `delete_card`, `list_columns`, `create_column`, `update_column`, `delete_column`, `list_projects`, `create_project`, `update_project`, `delete_project`. Each calls the matching `/api/*` route, so behavior is identical to the UI.

Connect from Claude Code:

`claude mcp add --transport http perkan http://localhost:5000/mcp`

Browser requests with a foreign `Origin` header are rejected (DNS-rebinding protection); set `MCP_ALLOWED_ORIGINS` to a comma-separated list to allow others.
