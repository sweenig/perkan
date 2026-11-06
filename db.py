"""SQLite storage for PerKan.

Every request opens its own connection. Writers use BEGIN IMMEDIATE, so a
read-modify-write is atomic across threads *and* gunicorn worker processes
(WAL mode lets readers keep going while a writer holds the lock).

Card ordering: every card has one globally unique `ord` value. A column shows
its cards sorted by `ord`; the list view sorts all cards by it. Moving a card
to another column never touches `ord`, so cards keep their relative position
across columns. Reordering changes only the moved card's value, and only as
far as needed to land between its new neighbours.
"""
import json
import logging
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

DATA_DIR = os.environ.get('PERKAN_DATA_DIR') or os.path.join(os.path.dirname(__file__), 'data')
DB_FILE = os.path.join(DATA_DIR, 'perkan.db')
LEGACY_JSON = os.path.join(DATA_DIR, 'kanban.json')

SCHEMA_VERSION = 4
ORDER_STEP = 1_000_000.0
MIN_GAP = 1e-3   # below this, neighbouring order values are renumbered to make room

SCHEMA = """
CREATE TABLE IF NOT EXISTS columns (
    id       TEXT PRIMARY KEY,
    title    TEXT NOT NULL,
    color    TEXT NOT NULL,
    hidden   INTEGER NOT NULL DEFAULT 0,
    position INTEGER NOT NULL,
    is_done  INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS projects (
    name     TEXT PRIMARY KEY,
    color    TEXT NOT NULL,
    position INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS cards (
    id          TEXT PRIMARY KEY,
    column_id   TEXT NOT NULL REFERENCES columns(id) ON DELETE CASCADE,
    title       TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    project     TEXT,
    assignee    TEXT,
    color       TEXT NOT NULL,
    ord         REAL NOT NULL,
    created_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_cards_column_ord ON cards(column_id, ord);
CREATE INDEX IF NOT EXISTS idx_cards_ord ON cards(ord);
CREATE TABLE IF NOT EXISTS card_links (
    card_id TEXT NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
    idx     INTEGER NOT NULL,
    text    TEXT NOT NULL,
    url     TEXT NOT NULL,
    PRIMARY KEY (card_id, idx)
);
CREATE TABLE IF NOT EXISTS card_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id     TEXT NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
    from_column TEXT,
    to_column   TEXT NOT NULL,
    at          TEXT NOT NULL,
    source      TEXT NOT NULL DEFAULT 'api'
);
CREATE INDEX IF NOT EXISTS idx_card_history_card ON card_history(card_id, id);
"""


def _connect():
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_FILE, timeout=30, isolation_level=None)  # we manage transactions
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA busy_timeout = 30000')
    conn.execute('PRAGMA foreign_keys = ON')
    conn.execute('PRAGMA journal_mode = WAL')
    conn.execute('PRAGMA synchronous = NORMAL')
    conn.create_function('pylower', 1, lambda s: (s or '').lower(), deterministic=True)
    return conn


@contextmanager
def read():
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def write():
    """Exclusive transaction: commits on normal exit, rolls back on exception."""
    conn = _connect()
    try:
        conn.execute('BEGIN IMMEDIATE')
        try:
            yield conn
        except BaseException:
            conn.execute('ROLLBACK')
            raise
        else:
            conn.execute('COMMIT')
    finally:
        conn.close()


# ---------------------------------------------------------------- reading

def _links_for(conn, card_ids):
    links = {}
    ids = list(card_ids)
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        marks = ','.join('?' * len(chunk))
        for row in conn.execute(
                f'SELECT card_id, text, url FROM card_links WHERE card_id IN ({marks}) ORDER BY card_id, idx', chunk):
            links.setdefault(row['card_id'], []).append({'text': row['text'], 'url': row['url']})
    return links


def card_from_row(row, links, dates=None):
    card = {'id': row['id'], 'title': row['title'], 'description': row['description'], 'links': links}
    if row['project']:
        card['project'] = row['project']
    if row['assignee']:
        card['assignee'] = row['assignee']
    card['color'] = row['color']
    card['order'] = float(row['ord'])
    created = row['created_at']
    started, completed, since = (dates or {}).get(row['id'], (None, None, None))
    created = created or (dates or {}).get((row['id'], 'created'))
    if created:
        card['created_at'] = created
    if started:
        card['started_at'] = started
    if completed:
        card['completed_at'] = completed
    if since:
        card['column_since'] = since
    return card


def load_board(conn, include_hidden=False):
    """Return the board in the API's JSON shape. Hidden columns (and their cards) are
    left out unless include_hidden is set."""
    columns = []
    by_id = {}
    sql = 'SELECT * FROM columns' + ('' if include_hidden else ' WHERE hidden = 0') + ' ORDER BY position'
    for row in conn.execute(sql):
        col = {'id': row['id'], 'title': row['title'], 'color': row['color'],
               'hidden': bool(row['hidden']), 'is_done': bool(row['is_done']), 'cards': []}
        columns.append(col)
        by_id[col['id']] = col

    rows = [r for r in conn.execute('SELECT * FROM cards ORDER BY ord, id') if r['column_id'] in by_id]
    ids = [r['id'] for r in rows]
    links = _links_for(conn, ids)
    dates = card_dates(conn, ids)
    for row in rows:
        by_id[row['column_id']]['cards'].append(card_from_row(row, links.get(row['id'], []), dates))

    projects = [{'name': r['name'], 'color': r['color']}
                for r in conn.execute('SELECT name, color FROM projects ORDER BY position')]
    return {'columns': columns, 'projects': projects}


def list_columns(conn, include_hidden=False):
    sql = 'SELECT * FROM columns' + ('' if include_hidden else ' WHERE hidden = 0') + ' ORDER BY position'
    return [{'id': r['id'], 'title': r['title'], 'color': r['color'], 'hidden': bool(r['hidden']),
             'is_done': bool(r['is_done'])} for r in conn.execute(sql)]


def list_cards(conn, column=None, project=None, assignee=None, query=None,
               limit=None, offset=0, include_hidden=False):
    """Cards in global `ord` order, each tagged with its column id. Returns (cards, total).

    Cards in hidden columns are skipped unless include_hidden is set or `column`
    names the (hidden) column explicitly.
    """
    where, params = [], []
    if column:
        where.append('cards.column_id = ?')
        params.append(column)
    elif not include_hidden:
        where.append('columns.hidden = 0')
    if project:
        where.append('cards.project = ?')
        params.append(project)
    if assignee:
        where.append('cards.assignee = ?')
        params.append(assignee)
    if query:
        where.append("instr(pylower(cards.title || ' ' || cards.description), ?) > 0")
        params.append(query.lower())
    clause = (' WHERE ' + ' AND '.join(where)) if where else ''
    base = 'FROM cards JOIN columns ON columns.id = cards.column_id' + clause

    total = conn.execute('SELECT COUNT(*) ' + base, params).fetchone()[0]
    sql = 'SELECT cards.*, columns.id AS col_id ' + base + ' ORDER BY cards.ord, cards.id LIMIT ? OFFSET ?'
    rows = conn.execute(sql, params + [-1 if limit is None else limit, offset or 0]).fetchall()
    ids = [r['id'] for r in rows]
    links = _links_for(conn, ids)
    dates = card_dates(conn, ids)
    cards = [dict(card_from_row(r, links.get(r['id'], []), dates), column=r['col_id']) for r in rows]
    return cards, total


def get_card(conn, card_id):
    row = conn.execute('SELECT * FROM cards WHERE id = ?', (card_id,)).fetchone()
    if row is None:
        return None, None
    return (card_from_row(row, _links_for(conn, [card_id]).get(card_id, []), card_dates(conn, [card_id])),
            row['column_id'])


# ---------------------------------------------------------------- writing cards

def insert_card(conn, column_id, card):
    conn.execute(
        'INSERT INTO cards (id, column_id, title, description, project, assignee, color, ord, created_at) '
        'VALUES (?,?,?,?,?,?,?,?,?)',
        (card['id'], column_id, card['title'], card.get('description', ''),
         card.get('project'), card.get('assignee'), card['color'], float(card['order']),
         card.get('created_at')))
    set_links(conn, card['id'], card.get('links') or [])


def update_card_row(conn, card, column_id=None):
    conn.execute(
        'UPDATE cards SET title=?, description=?, project=?, assignee=?, color=?, ord=?, '
        'column_id=COALESCE(?, column_id) WHERE id=?',
        (card['title'], card.get('description', ''), card.get('project'), card.get('assignee'),
         card['color'], float(card['order']), column_id, card['id']))
    set_links(conn, card['id'], card.get('links') or [])


def set_links(conn, card_id, links):
    conn.execute('DELETE FROM card_links WHERE card_id = ?', (card_id,))
    conn.executemany('INSERT INTO card_links (card_id, idx, text, url) VALUES (?,?,?,?)',
                     [(card_id, i, l['text'], l['url']) for i, l in enumerate(links)])


# ---------------------------------------------------------------- column-change history

def now_iso():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def record_move(conn, card_id, from_column, to_column, source='api', at=None):
    """Append one history row: a card was created in `to_column` (from_column None) or moved there."""
    conn.execute('INSERT INTO card_history (card_id, from_column, to_column, at, source) VALUES (?,?,?,?,?)',
                 (card_id, from_column, to_column, at or now_iso(), source))


def card_history(conn, card_id):
    return [{'from': r['from_column'], 'to': r['to_column'], 'at': r['at'], 'source': r['source']}
            for r in conn.execute('SELECT * FROM card_history WHERE card_id = ? ORDER BY id', (card_id,))]


def card_dates(conn, card_ids):
    """{card_id: (started_at, completed_at, column_since)} derived from history; any may be None.
    Also {(card_id, 'created'): ts} for cards whose creation is only known from history.

    started_at   first time the card was moved to another column (creation does not count).
    completed_at when the card last entered a done column from a non-done one; None while it
                 is outside every done column. Moving between done columns keeps it.
                 Moving a card back out clears it; entering Done again sets a fresh date.
    column_since when the card entered the column it is in now (its last history row).
    """
    ids = list(card_ids)
    if not ids:
        return {}
    done = {r['id'] for r in conn.execute('SELECT id FROM columns WHERE is_done = 1')}
    state = {}
    out = {}
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        marks = ','.join('?' * len(chunk))
        for r in conn.execute(
                f'SELECT card_id, from_column, to_column, at, source FROM card_history '
                f'WHERE card_id IN ({marks}) ORDER BY id', chunk):
            cid = r['card_id']
            started, completed, _since = state.get(cid, (None, None, None))
            if r['from_column'] is None and r['source'] != 'backfill':
                out.setdefault((cid, 'created'), r['at'])
            if r['from_column'] is not None and started is None and r['source'] != 'backfill':
                started = r['at']
            into_done = r['to_column'] in done
            was_done = r['from_column'] in done
            if into_done and not was_done:
                completed = r['at']
            elif not into_done:
                completed = None
            state[cid] = (started, completed, (r['to_column'], r['at']))
    current = {}
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        marks = ','.join('?' * len(chunk))
        for r in conn.execute(f'SELECT id, column_id FROM cards WHERE id IN ({marks})', chunk):
            current[r['id']] = r['column_id']
    for cid, (started, completed, last) in state.items():
        since = last[1] if last and last[0] == current.get(cid) else None
        out[cid] = (started, completed, since)
    return out


def _parse(ts):
    return datetime.strptime(ts, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)


def _days(a, b):
    return round((_parse(b) - _parse(a)).total_seconds() / 86400, 2)


def _summary(values):
    values = sorted(values)
    if not values:
        return {'n': 0}
    mid = len(values) // 2
    median = values[mid] if len(values) % 2 else round((values[mid - 1] + values[mid]) / 2, 2)
    return {'n': len(values), 'avg': round(sum(values) / len(values), 2), 'median': median,
            'min': values[0], 'max': values[-1]}


def card_stats(conn, project=None, created_since=None, completed_since=None, now=None):
    """Durations in days for every card, plus summaries overall, per project and per column.

    backlog_days     created -> started        (how long it sat before being worked)
    work_days        started -> completed
    lead_days        created -> completed
    in_column_days   column_since -> now, for cards not in a done column
    A metric is absent when a timestamp it needs is unknown. Cards with no recorded dates at all
    (they predate history) are left out.
    created_since / completed_since are yyyy-mm-dd (or ISO) lower bounds on created_at / completed_at.
    """
    now = now or now_iso()
    cards, _total = list_cards(conn, project=project, include_hidden=True)
    done = {r['id'] for r in conn.execute('SELECT id FROM columns WHERE is_done = 1')}
    titles = {r['id']: r['title'] for r in conn.execute('SELECT id, title FROM columns')}
    rows = []
    for c in cards:
        if created_since and not (c.get('created_at') and c['created_at'] >= created_since):
            continue
        if completed_since and not (c.get('completed_at') and c['completed_at'] >= completed_since):
            continue
        row = {'id': c['id'], 'title': c['title'], 'project': c.get('project'), 'column': c['column'],
               'is_done': c['column'] in done}
        for key in ('created_at', 'started_at', 'completed_at', 'column_since'):
            if c.get(key):
                row[key] = c[key]
        if row.get('created_at') and row.get('started_at'):
            row['backlog_days'] = _days(row['created_at'], row['started_at'])
        if row.get('started_at') and row.get('completed_at'):
            row['work_days'] = _days(row['started_at'], row['completed_at'])
        if row.get('created_at') and row.get('completed_at'):
            row['lead_days'] = _days(row['created_at'], row['completed_at'])
        if not row['is_done'] and row.get('column_since'):
            row['in_column_days'] = _days(row['column_since'], now)
        if not any(k in row for k in ('created_at', 'started_at', 'completed_at', 'column_since')):
            continue        # nothing was ever recorded for this card (it predates history)
        rows.append(row)

    metrics = ('backlog_days', 'work_days', 'lead_days')

    def summarize(subset):
        return {m: _summary([r[m] for r in subset if m in r]) for m in metrics}

    by_project = {}
    for r in rows:
        by_project.setdefault(r['project'] or '(none)', []).append(r)
    by_column = {}
    for r in rows:
        if 'in_column_days' in r:
            by_column.setdefault(r['column'], []).append(r['in_column_days'])
    return {
        'as_of': now,
        'cards': rows,
        'summary': dict(summarize(rows), cards=len(rows), completed=sum(1 for r in rows if 'completed_at' in r)),
        'by_project': {name: dict(summarize(sub), cards=len(sub)) for name, sub in sorted(by_project.items())},
        'open_by_column': {cid: dict(_summary(v), title=titles.get(cid, cid)) for cid, v in by_column.items()},
    }


_DONE_RE = re.compile(r'\bDONE\s+(\d{4}-\d{2}-\d{2})', re.IGNORECASE)


def backfill_done_dates(conn):
    """Give cards that sit in a done column but have no history a completion date taken from
    a "DONE yyyy-mm-dd" line in their description (source 'backfill'). Returns how many."""
    rows = conn.execute(
        'SELECT cards.id, cards.column_id, cards.description FROM cards '
        'JOIN columns ON columns.id = cards.column_id WHERE columns.is_done = 1 '
        'AND NOT EXISTS (SELECT 1 FROM card_history WHERE card_history.card_id = cards.id)').fetchall()
    n = 0
    for r in rows:
        found = _DONE_RE.findall(r['description'] or '')
        if found:
            record_move(conn, r['id'], None, r['column_id'], source='backfill', at=found[-1] + 'T00:00:00Z')
            n += 1
    return n


# ---------------------------------------------------------------- ordering

def renumber_all(conn):
    """Respace every card's `ord` evenly, keeping the current global order."""
    ids = [r['id'] for r in conn.execute('SELECT id FROM cards ORDER BY ord, id')]
    conn.executemany('UPDATE cards SET ord = ? WHERE id = ?',
                     [((i + 1) * ORDER_STEP, cid) for i, cid in enumerate(ids)])


def end_order(conn):
    """An order value after every existing card."""
    top = conn.execute('SELECT MAX(ord) FROM cards').fetchone()[0]
    return (top if top is not None else 0.0) + ORDER_STEP


def _above(conn, value, exclude_id):
    row = conn.execute('SELECT MIN(ord) FROM cards WHERE ord > ? AND id IS NOT ?', (value, exclude_id)).fetchone()
    return row[0]


def _below(conn, value, exclude_id):
    row = conn.execute('SELECT MAX(ord) FROM cards WHERE ord < ? AND id IS NOT ?', (value, exclude_id)).fetchone()
    return row[0]


def order_for_position(conn, column_id, index, card_id=None):
    """Order value that puts a card at `index` among the *other* cards of `column_id`.

    The card's current value is kept whenever it already sits between the neighbours
    at that index (so a plain column move never disturbs relative order). Otherwise it
    moves the shortest distance: just past the nearest neighbour it has to cross.
    `card_id` is None for a card that doesn't exist yet.
    """
    for _attempt in (0, 1):
        sibs = [r['ord'] for r in conn.execute(
            'SELECT ord FROM cards WHERE column_id = ? AND id IS NOT ? ORDER BY ord, id', (column_id, card_id))]
        cur = None
        if card_id is not None:
            row = conn.execute('SELECT ord FROM cards WHERE id = ?', (card_id,)).fetchone()
            cur = row['ord'] if row else None
        index = max(0, min(int(index), len(sibs)))
        lo = sibs[index - 1] if index > 0 else None
        hi = sibs[index] if index < len(sibs) else None

        if lo is None and hi is None:                       # empty column: nothing to be ordered against
            return cur if cur is not None else end_order(conn)
        if cur is not None and (lo is None or cur > lo) and (hi is None or cur < hi):
            return cur                                      # already in the right slot

        if lo is not None and (cur is None or cur <= lo):   # must move up: land just above `lo`
            nxt = _above(conn, lo, card_id)
            upper = hi if nxt is None else (nxt if hi is None else min(hi, nxt))
            if upper is None:
                return lo + ORDER_STEP
            low, high = lo, upper
        else:                                               # must move down: land just below `hi`
            prv = _below(conn, hi, card_id)
            lower = lo if prv is None else (prv if lo is None else max(lo, prv))
            if lower is None:
                return hi - ORDER_STEP
            low, high = lower, hi

        if high - low >= MIN_GAP:
            return (low + high) / 2
        renumber_all(conn)                                  # no room left between floats; respace and retry
    return end_order(conn)


def order_for_explicit(conn, value, card_id=None):
    """Honour an explicitly requested order value, keeping values globally unique."""
    value = float(value)
    taken = conn.execute('SELECT 1 FROM cards WHERE ord = ? AND id IS NOT ?', (value, card_id)).fetchone()
    if not taken:
        return value
    nxt = _above(conn, value, card_id)                      # collision: slot in right after the holder
    if nxt is None:
        return value + ORDER_STEP
    if nxt - value >= MIN_GAP:
        return (value + nxt) / 2
    return end_order(conn)


# ---------------------------------------------------------------- columns / projects

def column_ids(conn):
    return [r['id'] for r in conn.execute('SELECT id FROM columns ORDER BY position')]


def place_columns(conn, ids):
    conn.executemany('UPDATE columns SET position = ? WHERE id = ?', [(i, cid) for i, cid in enumerate(ids)])


def project_names(conn):
    return [r['name'] for r in conn.execute('SELECT name FROM projects ORDER BY position')]


def place_projects(conn, names):
    conn.executemany('UPDATE projects SET position = ? WHERE name = ?', [(i, n) for i, n in enumerate(names)])


# ---------------------------------------------------------------- whole-board replace

def replace_board(conn, board):
    """Overwrite everything with an already-normalized board dict (globally unique orders)."""
    # history belongs to a card id: keep it for cards that survive the replace (merge imports)
    kept = [tuple(r) for r in conn.execute(
        'SELECT card_id, from_column, to_column, at, source FROM card_history ORDER BY id')]
    created = {r['id']: r['created_at'] for r in conn.execute('SELECT id, created_at FROM cards')}
    conn.execute('DELETE FROM card_links')
    conn.execute('DELETE FROM cards')
    conn.execute('DELETE FROM columns')
    conn.execute('DELETE FROM projects')
    for i, col in enumerate(board.get('columns', [])):
        conn.execute('INSERT INTO columns (id, title, color, hidden, position, is_done) VALUES (?,?,?,?,?,?)',
                     (col['id'], col['title'], col['color'], 1 if col.get('hidden') else 0, i,
                      1 if col.get('is_done') else 0))
        for card in col.get('cards', []):
            if not card.get('created_at') and created.get(card['id']):
                card = dict(card, created_at=created[card['id']])
            insert_card(conn, col['id'], card)
    for i, proj in enumerate(board.get('projects', [])):
        conn.execute('INSERT INTO projects (name, color, position) VALUES (?,?,?)', (proj['name'], proj['color'], i))
    new_ids = {r['id'] for r in conn.execute('SELECT id FROM cards')}
    conn.executemany(
        'INSERT INTO card_history (card_id, from_column, to_column, at, source) VALUES (?,?,?,?,?)',
        [h for h in kept if h[0] in new_ids and conn.execute(
            'SELECT 1 FROM columns WHERE id = ?', (h[2],)).fetchone()])
    backfill_done_dates(conn)


# ---------------------------------------------------------------- startup / migration

def _migrate_1_to_2(conn):
    """v1 kept a per-column array position next to a mostly per-column `ord`. v2 has one
    global, unique `ord`. Keep the order people currently see: visible cards sorted by
    (ord, column, array position) -- exactly what the list view shows -- then hidden ones."""
    ids = [r['id'] for r in conn.execute(
        'SELECT cards.id FROM cards JOIN columns ON columns.id = cards.column_id '
        'ORDER BY columns.hidden, cards.ord, columns.position, cards.pos')]
    conn.executemany('UPDATE cards SET ord = ? WHERE id = ?', [((i + 1) * ORDER_STEP, cid) for i, cid in enumerate(ids)])
    conn.execute('DROP INDEX IF EXISTS idx_cards_column')
    conn.execute('ALTER TABLE cards DROP COLUMN pos')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_cards_column_ord ON cards(column_id, ord)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_cards_ord ON cards(ord)')
    logger.info('Migrated schema v1 -> v2: renumbered %d cards into one global order', len(ids))


def _migrate_2_to_3(conn):
    """v3 adds columns.is_done and the card_history table. Columns titled "Done..." are flagged
    as done, and cards already in them get a completion date from their description text."""
    conn.execute('ALTER TABLE columns ADD COLUMN is_done INTEGER NOT NULL DEFAULT 0')
    conn.execute("UPDATE columns SET is_done = 1 WHERE lower(title) LIKE 'done%'")
    conn.execute("""CREATE TABLE IF NOT EXISTS card_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        card_id TEXT NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
        from_column TEXT, to_column TEXT NOT NULL, at TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'api')""")
    conn.execute('CREATE INDEX IF NOT EXISTS idx_card_history_card ON card_history(card_id, id)')
    n = backfill_done_dates(conn)
    logger.info('Migrated schema v2 -> v3: added is_done and card_history, backfilled %d completion dates', n)


def _migrate_3_to_4(conn):
    """v4 adds cards.created_at. Existing cards stay empty: their creation time was never recorded."""
    conn.execute('ALTER TABLE cards ADD COLUMN created_at TEXT')
    logger.info('Migrated schema v3 -> v4: added cards.created_at')


def init(normalize, default_board):
    """Create or upgrade the database; on first run import kanban.json (or seed a blank board).

    `normalize` is app._normalize_board. Safe to call from several workers at once:
    the version check happens inside an exclusive transaction.
    """
    with read() as conn:
        version = conn.execute('PRAGMA user_version').fetchone()[0]
    if version == 0:
        # executescript commits implicitly, so it gets its own connection (IF NOT EXISTS makes races harmless)
        conn = _connect()
        try:
            conn.executescript(SCHEMA)
        finally:
            conn.close()

    migrated_from = None
    with write() as conn:
        version = conn.execute('PRAGMA user_version').fetchone()[0]
        if version >= SCHEMA_VERSION:
            return
        if version in (1, 2, 3):
            if version == 1:
                _migrate_1_to_2(conn)
            if version <= 2:
                _migrate_2_to_3(conn)
            _migrate_3_to_4(conn)
        else:
            if os.path.exists(LEGACY_JSON):
                with open(LEGACY_JSON, 'r', encoding='utf-8') as f:
                    board = normalize(json.load(f))
                migrated_from = LEGACY_JSON
            else:
                board = normalize(json.loads(json.dumps(default_board)))
            replace_board(conn, board)
            n_cards = sum(len(c['cards']) for c in board['columns'])
        conn.execute('PRAGMA user_version = %d' % SCHEMA_VERSION)

    if migrated_from:
        backup = migrated_from + '.migrated'
        try:
            os.replace(migrated_from, backup)
        except OSError:
            logger.exception('Imported %s but could not rename it', migrated_from)
        logger.info('Migrated %d columns / %d cards from %s into %s (original kept as %s)',
                    len(board['columns']), n_cards, migrated_from, DB_FILE, backup)
    elif version == 0:
        logger.info('Created new blank kanban board at %s', DB_FILE)
