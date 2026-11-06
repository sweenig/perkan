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
import sqlite3
from contextlib import contextmanager

logger = logging.getLogger(__name__)

DATA_DIR = os.environ.get('PERKAN_DATA_DIR') or os.path.join(os.path.dirname(__file__), 'data')
DB_FILE = os.path.join(DATA_DIR, 'perkan.db')
LEGACY_JSON = os.path.join(DATA_DIR, 'kanban.json')

SCHEMA_VERSION = 2
ORDER_STEP = 1_000_000.0
MIN_GAP = 1e-3   # below this, neighbouring order values are renumbered to make room

SCHEMA = """
CREATE TABLE IF NOT EXISTS columns (
    id       TEXT PRIMARY KEY,
    title    TEXT NOT NULL,
    color    TEXT NOT NULL,
    hidden   INTEGER NOT NULL DEFAULT 0,
    position INTEGER NOT NULL
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
    ord         REAL NOT NULL
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


def card_from_row(row, links):
    card = {'id': row['id'], 'title': row['title'], 'description': row['description'], 'links': links}
    if row['project']:
        card['project'] = row['project']
    if row['assignee']:
        card['assignee'] = row['assignee']
    card['color'] = row['color']
    card['order'] = float(row['ord'])
    return card


def load_board(conn, include_hidden=False):
    """Return the board in the API's JSON shape. Hidden columns (and their cards) are
    left out unless include_hidden is set."""
    columns = []
    by_id = {}
    sql = 'SELECT * FROM columns' + ('' if include_hidden else ' WHERE hidden = 0') + ' ORDER BY position'
    for row in conn.execute(sql):
        col = {'id': row['id'], 'title': row['title'], 'color': row['color'],
               'hidden': bool(row['hidden']), 'cards': []}
        columns.append(col)
        by_id[col['id']] = col

    rows = [r for r in conn.execute('SELECT * FROM cards ORDER BY ord, id') if r['column_id'] in by_id]
    links = _links_for(conn, [r['id'] for r in rows])
    for row in rows:
        by_id[row['column_id']]['cards'].append(card_from_row(row, links.get(row['id'], [])))

    projects = [{'name': r['name'], 'color': r['color']}
                for r in conn.execute('SELECT name, color FROM projects ORDER BY position')]
    return {'columns': columns, 'projects': projects}


def list_columns(conn, include_hidden=False):
    sql = 'SELECT * FROM columns' + ('' if include_hidden else ' WHERE hidden = 0') + ' ORDER BY position'
    return [{'id': r['id'], 'title': r['title'], 'color': r['color'], 'hidden': bool(r['hidden'])}
            for r in conn.execute(sql)]


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
    links = _links_for(conn, [r['id'] for r in rows])
    cards = [dict(card_from_row(r, links.get(r['id'], [])), column=r['col_id']) for r in rows]
    return cards, total


def get_card(conn, card_id):
    row = conn.execute('SELECT * FROM cards WHERE id = ?', (card_id,)).fetchone()
    if row is None:
        return None, None
    return card_from_row(row, _links_for(conn, [card_id]).get(card_id, [])), row['column_id']


# ---------------------------------------------------------------- writing cards

def insert_card(conn, column_id, card):
    conn.execute(
        'INSERT INTO cards (id, column_id, title, description, project, assignee, color, ord) '
        'VALUES (?,?,?,?,?,?,?,?)',
        (card['id'], column_id, card['title'], card.get('description', ''),
         card.get('project'), card.get('assignee'), card['color'], float(card['order'])))
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
    conn.execute('DELETE FROM card_links')
    conn.execute('DELETE FROM cards')
    conn.execute('DELETE FROM columns')
    conn.execute('DELETE FROM projects')
    for i, col in enumerate(board.get('columns', [])):
        conn.execute('INSERT INTO columns (id, title, color, hidden, position) VALUES (?,?,?,?,?)',
                     (col['id'], col['title'], col['color'], 1 if col.get('hidden') else 0, i))
        for card in col.get('cards', []):
            insert_card(conn, col['id'], card)
    for i, proj in enumerate(board.get('projects', [])):
        conn.execute('INSERT INTO projects (name, color, position) VALUES (?,?,?)', (proj['name'], proj['color'], i))


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
        if version == 1:
            _migrate_1_to_2(conn)
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
