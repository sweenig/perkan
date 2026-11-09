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
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

DATA_DIR = os.environ.get('PERKAN_DATA_DIR') or os.path.join(os.path.dirname(__file__), 'data')
DB_FILE = os.path.join(DATA_DIR, 'perkan.db')
LEGACY_JSON = os.path.join(DATA_DIR, 'kanban.json')

SCHEMA_VERSION = 7
ORDER_STEP = 1_000_000.0
MIN_GAP = 1e-3   # below this, neighbouring order values are renumbered to make room

SCHEMA = """
CREATE TABLE IF NOT EXISTS columns (
    id       TEXT PRIMARY KEY,
    title    TEXT NOT NULL,
    color    TEXT NOT NULL,
    hidden   INTEGER NOT NULL DEFAULT 0,
    position INTEGER NOT NULL,
    is_done  INTEGER NOT NULL DEFAULT 0,
    is_current_done INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS projects (
    name     TEXT PRIMARY KEY,
    color    TEXT NOT NULL,
    position INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sprints (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    goal       TEXT NOT NULL DEFAULT '',
    start_date TEXT,
    end_date   TEXT NOT NULL,
    state      TEXT NOT NULL DEFAULT 'planned',
    started_at TEXT,
    closed_at  TEXT,
    committed  INTEGER,
    completed  INTEGER,
    carried    INTEGER,
    added      INTEGER
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
    created_at  TEXT,
    sprint_id   TEXT REFERENCES sprints(id) ON DELETE SET NULL
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
CREATE TABLE IF NOT EXISTS card_sprint_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id     TEXT NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
    from_sprint TEXT,
    to_sprint   TEXT,
    at          TEXT NOT NULL,
    source      TEXT NOT NULL DEFAULT 'api'
);
CREATE INDEX IF NOT EXISTS idx_card_sprint_history_card ON card_sprint_history(card_id, id);
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
    if row['sprint_id']:
        card['sprint'] = row['sprint_id']
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


def load_board(conn, include_hidden=False, include_closed=False):
    """Return the board in the API's JSON shape. Hidden columns (and their cards) are
    left out unless include_hidden is set. Done cards that belong to a closed sprint are also
    left out unless include_closed is set."""
    columns = []
    by_id = {}
    sql = 'SELECT * FROM columns' + ('' if include_hidden else ' WHERE hidden = 0') + ' ORDER BY position'
    for row in conn.execute(sql):
        col = {'id': row['id'], 'title': row['title'], 'color': row['color'],
               'hidden': bool(row['hidden']), 'is_done': bool(row['is_done']),
               'is_current_done': bool(row['is_current_done']), 'cards': []}
        columns.append(col)
        by_id[col['id']] = col

    closed = set() if include_closed else {r['id'] for r in conn.execute("SELECT id FROM sprints WHERE state = 'closed'")}
    rows = [r for r in conn.execute('SELECT * FROM cards ORDER BY ord, id') if r['column_id'] in by_id
            and not (r['sprint_id'] in closed and by_id[r['column_id']]['is_done'])]
    ids = [r['id'] for r in rows]
    links = _links_for(conn, ids)
    dates = card_dates(conn, ids)
    adds = mid_sprint_additions(conn)
    for row in rows:
        card = card_from_row(row, links.get(row['id'], []), dates)
        if row['id'] in adds:
            card['added_after_start'] = True
            card['added_to_sprint_at'] = adds[row['id']]
        by_id[row['column_id']]['cards'].append(card)

    projects = [{'name': r['name'], 'color': r['color']}
                for r in conn.execute('SELECT name, color FROM projects ORDER BY position')]
    return {'columns': columns, 'projects': projects, 'active_sprint': active_sprint(conn)}


def list_columns(conn, include_hidden=False):
    sql = 'SELECT * FROM columns' + ('' if include_hidden else ' WHERE hidden = 0') + ' ORDER BY position'
    return [{'id': r['id'], 'title': r['title'], 'color': r['color'], 'hidden': bool(r['hidden']),
             'is_done': bool(r['is_done']), 'is_current_done': bool(r['is_current_done'])}
            for r in conn.execute(sql)]


def current_done_column(conn):
    """The column marked as the current done column (hidden or not), or None."""
    row = conn.execute('SELECT id, title FROM columns WHERE is_current_done = 1').fetchone()
    return {'id': row['id'], 'title': row['title']} if row else None


def set_current_done(conn, col_id):
    """Mark col_id as the one current done column (clearing it elsewhere); it is also a done column."""
    conn.execute('UPDATE columns SET is_current_done = 0 WHERE is_current_done = 1')
    conn.execute('UPDATE columns SET is_current_done = 1, is_done = 1 WHERE id = ?', (col_id,))


def resolve_column(conn, ref):
    """Column id for `ref`. "done" and "current_done" mean the current done column whenever one is
    marked (even if some other column's id happens to be "done"); otherwise `ref` must be a column id.
    Returns None when nothing matches."""
    if ref in ('done', 'current_done'):
        cur = current_done_column(conn)
        if cur:
            return cur['id']
    if ref and conn.execute('SELECT 1 FROM columns WHERE id = ?', (ref,)).fetchone():
        return ref
    return None


def list_cards(conn, column=None, project=None, assignee=None, query=None,
               limit=None, offset=0, include_hidden=False, sprint=None, include_closed=False):
    """Cards in global `ord` order, each tagged with its column id. Returns (cards, total).

    Cards in hidden columns are skipped unless include_hidden is set or `column`
    names the (hidden) column explicitly. Done cards of a closed sprint are skipped unless
    include_closed is set or `sprint` is given. `sprint` is a sprint id, "active" or "backlog".
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
    if sprint:
        sid = resolve_sprint(conn, sprint)
        if sid is None:
            where.append('cards.sprint_id IS NULL' if sprint in ('backlog', 'none') else '0')
        else:
            where.append('cards.sprint_id = ?')
            params.append(sid)
    elif not include_closed:
        where.append("NOT (columns.is_done = 1 AND cards.sprint_id IN (SELECT id FROM sprints WHERE state = 'closed'))")
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
    return _flag_additions(conn, cards), total


def mid_sprint_additions(conn, card_ids=None, by_sprint=False):
    """{card id: time it was added} for cards now in a sprint that they entered after it started.

    A card counts when its latest move into its current sprint happened after that sprint's started_at
    (cards carried over or planned before the start do not). Backlog and planned-sprint cards never count.
    With by_sprint (and no card_ids) returns {sprint id: number of such cards} instead.
    """
    sql = ('SELECT cards.id AS id, cards.sprint_id AS sid, MAX(h.at) AS at FROM cards '
           'JOIN sprints ON sprints.id = cards.sprint_id AND sprints.started_at IS NOT NULL '
           'JOIN card_sprint_history h ON h.card_id = cards.id AND h.to_sprint = cards.sprint_id '
           'GROUP BY cards.id, sprints.started_at HAVING MAX(h.at) > sprints.started_at')
    rows = conn.execute(sql).fetchall()
    if card_ids is None and by_sprint:
        counts = {}
        for r in rows:
            counts[r['sid']] = counts.get(r['sid'], 0) + 1
        return counts
    out = {r['id']: r['at'] for r in rows}
    if card_ids is not None:
        keep = set(card_ids)
        out = {k: v for k, v in out.items() if k in keep}
    return out


def _flag_additions(conn, cards):
    adds = mid_sprint_additions(conn, [c['id'] for c in cards]) if any(c.get('sprint') for c in cards) else {}
    for c in cards:
        if c['id'] in adds:
            c['added_after_start'] = True
            c['added_to_sprint_at'] = adds[c['id']]
    return cards


def get_card(conn, card_id):
    row = conn.execute('SELECT * FROM cards WHERE id = ?', (card_id,)).fetchone()
    if row is None:
        return None, None
    card = card_from_row(row, _links_for(conn, [card_id]).get(card_id, []), card_dates(conn, [card_id]))
    return _flag_additions(conn, [card])[0], row['column_id']


# ---------------------------------------------------------------- writing cards

def insert_card(conn, column_id, card):
    conn.execute(
        'INSERT INTO cards (id, column_id, title, description, project, assignee, color, ord, created_at, sprint_id) '
        'VALUES (?,?,?,?,?,?,?,?,?, (SELECT id FROM sprints WHERE id = ?))',   # unknown sprint ids become backlog
        (card['id'], column_id, card['title'], card.get('description', ''),
         card.get('project'), card.get('assignee'), card['color'], float(card['order']),
         card.get('created_at'), card.get('sprint')))
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
            if r['from_column'] is None and r['source'] not in ('backfill', 'migration'):
                out.setdefault((cid, 'created'), r['at'])
            if r['from_column'] is not None and started is None and r['source'] not in ('backfill', 'migration'):
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


def card_stats(conn, project=None, created_since=None, completed_since=None, now=None, sprint=None):
    """Durations in days for every card, plus summaries overall, per project and per column.

    backlog_days     created -> started        (how long it sat before being worked)
    work_days        started -> completed
    lead_days        created -> completed
    in_column_days   column_since -> now, for cards not in a done column
    A metric is absent when a timestamp it needs is unknown. Cards with no recorded dates at all
    (they predate history) are left out.
    created_since / completed_since are yyyy-mm-dd (or ISO) lower bounds on created_at / completed_at.
    sprint (an id, "active" or "backlog") limits the cards to one sprint. by_sprint summarises every sprint:
    committed / completed / carried (snapshotted when it closed, live counts while it is open) and the
    lead/work time summaries of its completed cards.
    """
    now = now or now_iso()
    cards, _total = list_cards(conn, project=project, include_hidden=True, include_closed=True, sprint=sprint)
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
        if c.get('sprint'):
            row['sprint'] = c['sprint']
        if c.get('added_after_start'):
            row['added_after_start'] = True
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
    by_sprint = {}
    for r in rows:
        by_sprint.setdefault(r.get('sprint'), []).append(r)
    sprint_summaries = {}
    for sp in list_sprints(conn, include_closed=True):
        if sprint and sp['id'] not in by_sprint:
            continue
        sub = by_sprint.get(sp['id'], [])
        closed = sp['state'] == 'closed'
        entry = {'name': sp['name'], 'state': sp['state'], 'start_date': sp['start_date'], 'end_date': sp['end_date'],
                 'committed': sp['committed'] if closed else sp['cards'] - sp['added'],
                 'completed': sp['completed'] if closed else sp['done']}
        if sp['added'] is not None:
            entry['added'] = sp['added']
        if closed:
            entry['carried'] = sp['carried']
        entry.update(summarize(sub))
        sprint_summaries[sp['id']] = entry
    return {
        'as_of': now,
        'cards': rows,
        'by_sprint': sprint_summaries,
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
    kept_sprint = [tuple(r) for r in conn.execute(
        'SELECT card_id, from_sprint, to_sprint, at, source FROM card_sprint_history ORDER BY id')]
    created = {r['id']: r['created_at'] for r in conn.execute('SELECT id, created_at FROM cards')}
    conn.execute('DELETE FROM card_links')
    conn.execute('DELETE FROM cards')
    if 'sprints' in board:      # an import without the key leaves the sprints alone
        conn.execute('DELETE FROM sprints')
        for sp in board['sprints']:
            conn.execute('INSERT INTO sprints (id, name, goal, start_date, end_date, state, started_at, closed_at, '
                         'committed, completed, carried, added) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                         (sp['id'], sp['name'], sp['goal'], sp['start_date'], sp['end_date'], sp['state'],
                          sp['started_at'], sp['closed_at'], sp['committed'], sp['completed'], sp['carried'], sp.get('added')))
    conn.execute('DELETE FROM columns')
    conn.execute('DELETE FROM projects')
    for i, col in enumerate(board.get('columns', [])):
        conn.execute('INSERT INTO columns (id, title, color, hidden, position, is_done, is_current_done) '
                     'VALUES (?,?,?,?,?,?,?)',
                     (col['id'], col['title'], col['color'], 1 if col.get('hidden') else 0, i,
                      1 if col.get('is_done') else 0, 1 if col.get('is_current_done') else 0))
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
    conn.executemany(
        'INSERT INTO card_sprint_history (card_id, from_sprint, to_sprint, at, source) VALUES (?,?,?,?,?)',
        [h for h in kept_sprint if h[0] in new_ids])
    backfill_done_dates(conn)


# ---------------------------------------------------------------- sprints

DATE_RE = re.compile(r'\d{4}-\d{2}-\d{2}')


class SprintError(ValueError):
    """A sprint request that cannot be honoured (bad input, wrong state, unknown id)."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def today():
    """Today's date (yyyy-mm-dd) in PERKAN_TZ (default UTC): a sprint ends at the end of its end_date."""
    tz = os.environ.get('PERKAN_TZ')
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo(tz)) if tz else datetime.now(timezone.utc)
    except Exception:
        now = datetime.now(timezone.utc)
    return now.strftime('%Y-%m-%d')


def _check_date(value, name):
    if not isinstance(value, str) or not DATE_RE.fullmatch(value):
        raise SprintError(f'{name} must be yyyy-mm-dd')
    try:
        return datetime.strptime(value, '%Y-%m-%d').date()
    except ValueError:
        raise SprintError(f'{name} is not a valid date')


def _sprint_dict(row, counts, today_str, added=0):
    d = {'id': row['id'], 'name': row['name'], 'goal': row['goal'], 'state': row['state'],
         'start_date': row['start_date'], 'end_date': row['end_date'],
         'started_at': row['started_at'], 'closed_at': row['closed_at'],
         'cards': counts[0], 'done': counts[1]}
    if row['state'] == 'closed':
        d.update(committed=row['committed'], completed=row['completed'], carried=row['carried'], added=row['added'])
    else:
        d['added'] = added
    if row['state'] == 'active' and row['end_date'] < today_str:
        d['overdue_days'] = (_check_date(today_str, 'today') - _check_date(row['end_date'], 'end_date')).days
    return d


def _sprint_counts(conn):
    counts = {}
    for r in conn.execute(
            'SELECT cards.sprint_id AS sid, COUNT(*) AS n, SUM(columns.is_done) AS d FROM cards '
            'JOIN columns ON columns.id = cards.column_id WHERE cards.sprint_id IS NOT NULL GROUP BY cards.sprint_id'):
        counts[r['sid']] = (r['n'], r['d'] or 0)
    return counts


def list_sprints(conn, include_closed=False):
    """Sprints in the order they are worked: active first, then planned by end date, then closed (newest first)."""
    counts = _sprint_counts(conn)
    t = today()
    rows = conn.execute('SELECT * FROM sprints').fetchall()
    rank = {'active': 0, 'planned': 1, 'closed': 2}
    rows = [r for r in rows if include_closed or r['state'] != 'closed']
    rows.sort(key=lambda r: (rank[r['state']], r['end_date'] if r['state'] != 'closed' else '', r['id']))
    closed = sorted([r for r in rows if r['state'] == 'closed'], key=lambda r: r['end_date'], reverse=True)
    rows = [r for r in rows if r['state'] != 'closed'] + closed
    added = mid_sprint_additions(conn, by_sprint=True)
    return [_sprint_dict(r, counts.get(r['id'], (0, 0)), t, added.get(r['id'], 0)) for r in rows]


def get_sprint(conn, sprint_id):
    row = conn.execute('SELECT * FROM sprints WHERE id = ?', (sprint_id,)).fetchone()
    if row is None:
        return None
    added = mid_sprint_additions(conn, by_sprint=True).get(sprint_id, 0)
    return _sprint_dict(row, _sprint_counts(conn).get(sprint_id, (0, 0)), today(), added)


def active_sprint(conn):
    row = conn.execute("SELECT id FROM sprints WHERE state = 'active'").fetchone()
    return row['id'] if row else None


def resolve_sprint(conn, ref):
    """Sprint id for `ref` (an id or "active"), or None for backlog / no match. Use sprint_ref_ok to validate."""
    if not ref or ref in ('backlog', 'none'):
        return None
    if ref == 'active':
        return active_sprint(conn)
    row = conn.execute('SELECT id FROM sprints WHERE id = ?', (ref,)).fetchone()
    return row['id'] if row else None


def create_sprint(conn, end_date, name=None, goal='', start_date=None):
    """A new planned sprint ending at the end of end_date (yyyy-mm-dd)."""
    end = _check_date(end_date, 'end_date')
    if start_date:
        if _check_date(start_date, 'start_date') > end:
            raise SprintError('start_date is after end_date')
    name = (name or '').strip() or 'Sprint ' + end.strftime('%m/%d')
    sid = str(uuid.uuid4())
    conn.execute('INSERT INTO sprints (id, name, goal, start_date, end_date, state) VALUES (?,?,?,?,?,?)',
                 (sid, name, goal or '', start_date or None, end_date, 'planned'))
    return sid


def update_sprint(conn, sprint_id, **fields):
    row = conn.execute('SELECT * FROM sprints WHERE id = ?', (sprint_id,)).fetchone()
    if row is None:
        raise SprintError('sprint not found', 404)
    if row['state'] == 'closed':
        raise SprintError('a closed sprint cannot be edited')
    new = {k: v for k, v in fields.items() if v is not None}
    if 'name' in new:
        new['name'] = str(new['name']).strip()
        if not new['name']:
            raise SprintError('name cannot be empty')
    for key in ('start_date', 'end_date'):
        if new.get(key):
            _check_date(new[key], key)
    start = new.get('start_date', row['start_date'])
    end = new.get('end_date', row['end_date'])
    if start and start > end:
        raise SprintError('start_date is after end_date')
    for key in ('name', 'goal', 'start_date', 'end_date'):
        if key in new:
            conn.execute(f'UPDATE sprints SET {key} = ? WHERE id = ?', (new[key] or None if key == 'start_date' else new[key], sprint_id))


def start_sprint(conn, sprint_id, start_date=None):
    row = conn.execute('SELECT * FROM sprints WHERE id = ?', (sprint_id,)).fetchone()
    if row is None:
        raise SprintError('sprint not found', 404)
    if row['state'] != 'planned':
        raise SprintError(f"only a planned sprint can be started (this one is {row['state']})")
    other = active_sprint(conn)
    if other:
        raise SprintError('another sprint is already active; complete it first')
    start = start_date or row['start_date'] or today()
    if _check_date(start, 'start_date') > _check_date(row['end_date'], 'end_date'):
        raise SprintError('start_date is after end_date')
    conn.execute("UPDATE sprints SET state = 'active', start_date = ?, started_at = ? WHERE id = ?",
                 (start, now_iso(), sprint_id))


def set_card_sprint(conn, card_id, sprint_id, source='api', allow_closed=False):
    """Put a card in a sprint (None = backlog) and record the change. Closed sprints cannot receive cards."""
    row = conn.execute('SELECT sprint_id FROM cards WHERE id = ?', (card_id,)).fetchone()
    if row is None:
        raise SprintError('card not found', 404)
    if sprint_id is not None:
        target = conn.execute('SELECT state FROM sprints WHERE id = ?', (sprint_id,)).fetchone()
        if target is None:
            raise SprintError('sprint not found', 404)
        if target['state'] == 'closed' and row['sprint_id'] != sprint_id and not allow_closed:
            raise SprintError('cards cannot be moved into a closed sprint')
    if row['sprint_id'] == sprint_id:
        return False
    conn.execute('UPDATE cards SET sprint_id = ? WHERE id = ?', (sprint_id, card_id))
    conn.execute('INSERT INTO card_sprint_history (card_id, from_sprint, to_sprint, at, source) VALUES (?,?,?,?,?)',
                 (card_id, row['sprint_id'], sprint_id, now_iso(), source))
    return True


def card_sprint_history(conn, card_id):
    return [{'from': r['from_sprint'], 'to': r['to_sprint'], 'at': r['at'], 'source': r['source']}
            for r in conn.execute('SELECT * FROM card_sprint_history WHERE card_id = ? ORDER BY id', (card_id,))]


def complete_sprint(conn, sprint_id, unfinished='carry_over', next_sprint=None, next_end_date=None,
                    next_name=None, start_next=False, source='api'):
    """Close the active sprint in one go.

    Unfinished cards (not in a done column) are handled by `unfinished`:
      carry_over  move them to the next sprint (next_sprint id, or a new one made from
                  next_end_date / next_name; default end = this sprint's end + 7 days), keeping their column
      backlog     take them out of any sprint
      leave       keep them tagged to the closed sprint
    Done cards stay tagged to the closed sprint (the board then hides them from the Done column).
    committed / completed / carried are snapshotted on the sprint. Returns a summary dict.
    """
    if unfinished not in ('carry_over', 'backlog', 'leave'):
        raise SprintError('unfinished must be carry_over, backlog or leave')
    row = conn.execute('SELECT * FROM sprints WHERE id = ?', (sprint_id,)).fetchone()
    if row is None:
        raise SprintError('sprint not found', 404)
    if row['state'] != 'active':
        raise SprintError(f"only the active sprint can be completed (this one is {row['state']})")
    if unfinished != 'carry_over' and (next_sprint or next_end_date or next_name or start_next):
        raise SprintError('next_sprint, next_end_date, next_name and start_next only apply to carry_over')

    cards = conn.execute(
        'SELECT cards.id, columns.is_done AS d FROM cards JOIN columns ON columns.id = cards.column_id '
        'WHERE cards.sprint_id = ? ORDER BY cards.ord', (sprint_id,)).fetchall()
    open_ids = [c['id'] for c in cards if not c['d']]
    done_n = len(cards) - len(open_ids)

    target = None
    if unfinished == 'carry_over':
        if next_sprint:
            target = resolve_sprint(conn, next_sprint)
            if target is None or target == sprint_id:
                raise SprintError('next_sprint not found', 404)
            if conn.execute('SELECT state FROM sprints WHERE id = ?', (target,)).fetchone()['state'] != 'planned':
                raise SprintError('next_sprint must be a planned sprint')
        elif open_ids or start_next or next_end_date or next_name:
            end = next_end_date or (_check_date(row['end_date'], 'end_date') + timedelta(days=7)).strftime('%Y-%m-%d')
            target = create_sprint(conn, end, name=next_name)

    added_n = mid_sprint_additions(conn, by_sprint=True).get(sprint_id, 0)
    now = now_iso()
    conn.execute("UPDATE sprints SET state = 'closed', closed_at = ?, committed = ?, completed = ?, carried = ?, "
                 "added = ? WHERE id = ?",
                 (now, len(cards) - added_n, done_n, len(open_ids) if unfinished == 'carry_over' else 0, added_n, sprint_id))
    if unfinished == 'carry_over':
        for cid in open_ids:
            conn.execute('UPDATE cards SET sprint_id = ? WHERE id = ?', (target, cid))
            conn.execute('INSERT INTO card_sprint_history (card_id, from_sprint, to_sprint, at, source) '
                         'VALUES (?,?,?,?,?)', (cid, sprint_id, target, now, source))
    elif unfinished == 'backlog':
        for cid in open_ids:
            conn.execute('UPDATE cards SET sprint_id = NULL WHERE id = ?', (cid,))
            conn.execute('INSERT INTO card_sprint_history (card_id, from_sprint, to_sprint, at, source) '
                         'VALUES (?,?,?,?,?)', (cid, sprint_id, None, now, source))
    if start_next and target:
        start_sprint(conn, target)
    return {'sprint': get_sprint(conn, sprint_id), 'next_sprint': get_sprint(conn, target) if target else None,
            'committed': len(cards) - added_n, 'added': added_n, 'completed': done_n, 'unfinished': len(open_ids), 'unfinished_action': unfinished}


def delete_sprint(conn, sprint_id):
    """Delete a sprint; its cards go back to the backlog (the history keeps the move)."""
    if get_sprint(conn, sprint_id) is None:
        raise SprintError('sprint not found', 404)
    for r in conn.execute('SELECT id FROM cards WHERE sprint_id = ?', (sprint_id,)).fetchall():
        set_card_sprint(conn, r['id'], None)
    conn.execute('DELETE FROM sprints WHERE id = ?', (sprint_id,))


_WEEKLY_RE = re.compile(r'^\s*done\s+(\d{1,2})/(\d{1,2})\s*$', re.IGNORECASE)


def migrate_to_sprints(conn, today_str=None):
    """One-off conversion from "a done column per week" to sprints. Returns a report dict.

    * Every weekly done column ("Done 10/30") becomes a closed sprint ending on that date (7 days
      long); its cards are tagged to it and moved into the current done column. Done -> done moves
      keep completed_at. The emptied weekly columns are left in place (hidden).
    * Cards in the current done column that finished before today become one more closed sprint
      ending yesterday.
    * Cards in an "In Progress" or "Burn-In" column, plus cards that finished today, go into an
      active sprint ending at the end of today.
    * Everything else (To Do, Priority, Blocked, ...) stays in the backlog.
    Refuses to run when any sprint already exists.
    """
    if conn.execute('SELECT 1 FROM sprints').fetchone():
        raise SprintError('sprints already exist; the migration only runs on a board without any')
    cur = current_done_column(conn)
    if cur is None:
        raise SprintError('no current done column is marked (set is_current_done on the Done column first)')
    t = today_str or today()
    today_d = _check_date(t, 'today')
    report = {'closed_sprints': [], 'active_sprint': None, 'backlog': 0}

    def card_ids(column_id):
        return [r['id'] for r in conn.execute('SELECT id FROM cards WHERE column_id = ? ORDER BY ord', (column_id,))]

    def tag(ids, sprint_id, to_column=None):
        for cid in ids:
            if to_column:
                old = conn.execute('SELECT column_id FROM cards WHERE id = ?', (cid,)).fetchone()['column_id']
                if old != to_column:
                    conn.execute('UPDATE cards SET column_id = ? WHERE id = ?', (to_column, cid))
                    record_move(conn, cid, old, to_column, 'migration')
            set_card_sprint(conn, cid, sprint_id, 'migration', allow_closed=True)

    def closed_sprint(end_d, n):
        sid = create_sprint(conn, end_d.strftime('%Y-%m-%d'), start_date=(end_d - timedelta(days=6)).strftime('%Y-%m-%d'))
        stamp = end_d.strftime('%Y-%m-%d') + 'T23:59:59Z'
        conn.execute("UPDATE sprints SET state = 'closed', started_at = ?, closed_at = ?, committed = ?, "
                     "completed = ?, carried = 0 WHERE id = ?",
                     (end_d.strftime('%Y-%m-%d') + 'T00:00:00Z', stamp, n, n, sid))
        return sid

    weekly = []
    for col in conn.execute('SELECT id, title FROM columns WHERE is_done = 1 AND id != ?', (cur['id'],)).fetchall():
        m = _WEEKLY_RE.match(col['title'])
        if not m:
            continue
        try:
            end_d = datetime(today_d.year, int(m.group(1)), int(m.group(2))).date()
        except ValueError:
            continue
        if end_d > today_d:
            end_d = end_d.replace(year=end_d.year - 1)
        weekly.append((end_d, col))
    for end_d, col in sorted(weekly, key=lambda x: x[0]):
        ids = card_ids(col['id'])
        if not ids:
            continue
        sid = closed_sprint(end_d, len(ids))
        tag(ids, sid, cur['id'])
        report['closed_sprints'].append({'name': 'Sprint ' + end_d.strftime('%m/%d'), 'end_date': end_d.strftime('%Y-%m-%d'),
                                         'from_column': col['title'], 'cards': len(ids)})

    dates = card_dates(conn, card_ids(cur['id']))
    earlier, finished_today = [], []
    for cid in card_ids(cur['id']):
        completed = (dates.get(cid) or (None, None, None))[1]
        if conn.execute('SELECT sprint_id FROM cards WHERE id = ?', (cid,)).fetchone()['sprint_id']:
            continue
        (finished_today if completed and completed[:10] >= t else earlier).append(cid)
    if earlier:
        end_d = today_d - timedelta(days=1)
        sid = closed_sprint(end_d, len(earlier))
        tag(earlier, sid)
        report['closed_sprints'].append({'name': 'Sprint ' + end_d.strftime('%m/%d'), 'end_date': end_d.strftime('%Y-%m-%d'),
                                         'from_column': cur['title'] + ' (finished before today)', 'cards': len(earlier)})

    working = [r['id'] for r in conn.execute('SELECT id, title FROM columns') if
               re.search(r'in[\s-]*progress|burn[\s-]*in', r['title'], re.IGNORECASE)]
    active = [cid for col in working for cid in card_ids(col)] + finished_today
    if active:
        sid = create_sprint(conn, t, name='Sprint ' + today_d.strftime('%m/%d'), start_date=t)
        start_sprint(conn, sid, start_date=t)
        tag(active, sid)
        report['active_sprint'] = {'name': 'Sprint ' + today_d.strftime('%m/%d'), 'end_date': t,
                                   'cards': len(active), 'finished_today': len(finished_today)}
    report['backlog'] = conn.execute('SELECT COUNT(*) FROM cards WHERE sprint_id IS NULL').fetchone()[0]
    return report


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


def _migrate_4_to_5(conn):
    """v5 adds columns.is_current_done. Start it on the done column titled exactly "Done", if there is one."""
    conn.execute('ALTER TABLE columns ADD COLUMN is_current_done INTEGER NOT NULL DEFAULT 0')
    rows = conn.execute("SELECT id FROM columns WHERE is_done = 1 AND lower(trim(title)) = 'done'").fetchall()
    if len(rows) == 1:
        conn.execute('UPDATE columns SET is_current_done = 1 WHERE id = ?', (rows[0]['id'],))
    logger.info('Migrated schema v4 -> v5: added columns.is_current_done (%s)', rows[0]['id'] if len(rows) == 1 else 'unset')


def _migrate_5_to_6(conn):
    """v6 adds sprints: the sprints table, cards.sprint_id (null = backlog) and card_sprint_history."""
    conn.execute("""CREATE TABLE IF NOT EXISTS sprints (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, goal TEXT NOT NULL DEFAULT '', start_date TEXT,
        end_date TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'planned', started_at TEXT, closed_at TEXT,
        committed INTEGER, completed INTEGER, carried INTEGER)""")
    conn.execute('ALTER TABLE cards ADD COLUMN sprint_id TEXT REFERENCES sprints(id) ON DELETE SET NULL')
    conn.execute("""CREATE TABLE IF NOT EXISTS card_sprint_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        card_id TEXT NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
        from_sprint TEXT, to_sprint TEXT, at TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'api')""")
    conn.execute('CREATE INDEX IF NOT EXISTS idx_card_sprint_history_card ON card_sprint_history(card_id, id)')
    logger.info('Migrated schema v5 -> v6: added sprints (all cards start in the backlog)')


def _migrate_6_to_7(conn):
    """v7 adds sprints.added (cards added after the sprint started, snapshotted when it closes).
    Closed sprints keep it empty: committed there still counts every card."""
    conn.execute('ALTER TABLE sprints ADD COLUMN added INTEGER')
    logger.info('Migrated schema v6 -> v7: added sprints.added')


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
        if version in (1, 2, 3, 4, 5, 6):
            if version == 1:
                _migrate_1_to_2(conn)
            if version <= 2:
                _migrate_2_to_3(conn)
            if version <= 3:
                _migrate_3_to_4(conn)
            if version <= 4:
                _migrate_4_to_5(conn)
            if version <= 5:
                _migrate_5_to_6(conn)
            _migrate_6_to_7(conn)
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
