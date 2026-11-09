from flask import Flask, jsonify, request, render_template, Response
import json
import uuid
import re
import logging
import random
import copy
import math

import db

app = Flask(__name__, static_folder='static', template_folder='templates')
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DEFAULT_CARD_COLOR = '#5b2e8a'

DEFAULT_BOARD = {
    "columns": [
    {"id": "todo", "title": "To Do", "cards": [], "color": "#1f77b4", "hidden": False},
    {"id": "inprogress", "title": "In Progress", "cards": [], "color": "#ff8c00", "hidden": False},
    {"id": "blocked", "title": "Blocked", "cards": [], "color": "#d62728", "hidden": False},
    {"id": "done", "title": "Done", "cards": [], "color": "#2ca02c", "hidden": False, "is_done": True, "is_current_done": True}
    ],
    "projects": []
}


def _sanitize_card(card):
    if not isinstance(card, dict):
        return None
    sanitized = {}
    sanitized['id'] = str(card.get('id') or uuid.uuid4())
    sanitized['title'] = str(card.get('title') or '').strip() or 'Untitled'
    sanitized['description'] = str(card.get('description') or '')
    sanitized['links'] = _clean_links(card.get('links'))
    project_name = (card.get('project') or '').strip()
    if project_name:
        sanitized['project'] = project_name
    assignee = (card.get('assignee') or '').strip()
    if assignee:
        sanitized['assignee'] = assignee
    color = card.get('color')
    sanitized['color'] = color if color else DEFAULT_CARD_COLOR
    created_at = card.get('created_at')
    if isinstance(created_at, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z', created_at):
        sanitized['created_at'] = created_at
    sprint = card.get('sprint')
    if isinstance(sprint, str) and sprint:
        sanitized['sprint'] = sprint
    order = card.get('order')
    if order is not None:
        try:
            order = float(order)
            if math.isfinite(order):
                sanitized['order'] = order
        except (TypeError, ValueError):
            pass
    return sanitized


def _ensure_global_orders(columns):
    """Cards share one global order: every card needs a distinct `order`. If any are
    missing or repeated, renumber them all, keeping the order people see today (by
    existing order, then column, then position), with hidden columns' cards last."""
    entries = [(ci, idx, card) for ci, col in enumerate(columns) for idx, card in enumerate(col['cards'])]
    orders = [card.get('order') for _ci, _idx, card in entries]
    if all(o is not None for o in orders) and len(set(orders)) == len(orders):
        return

    def key(entry):
        ci, idx, card = entry
        order = card.get('order')
        return (bool(columns[ci]['hidden']), order if order is not None else math.inf, ci, idx)

    for n, (_ci, _idx, card) in enumerate(sorted(entries, key=key)):
        card['order'] = float((n + 1) * 1_000_000)


def _normalize_board(data):
    if not isinstance(data, dict):
        data = {}
    if 'columns' not in data or not isinstance(data['columns'], list):
        data['columns'] = []
    if 'projects' not in data or not isinstance(data['projects'], list):
        data['projects'] = []

    normalized_columns = []
    seen_ids = set()      # card ids must be unique across the whole board (they are a primary key)
    seen_columns = set()
    seen_current = False  # only one column may be the current done column
    for col in data['columns']:
        if not isinstance(col, dict):
            continue
        col_id = col.get('id') or str(uuid.uuid4())
        if col_id in seen_columns:
            col_id = str(uuid.uuid4())
        seen_columns.add(col_id)
        title = str(col.get('title') or '').strip() or 'Untitled'
        color = col.get('color') or '#9aa0a6'
        hidden = bool(col.get('hidden', False))
        is_done = _truthy(col['is_done']) if 'is_done' in col else title.lower().startswith('done')
        cards_payload = col.get('cards') if isinstance(col.get('cards'), list) else []
        normalized_cards = []
        for card in cards_payload:
            sanitized = _sanitize_card(card)
            if not sanitized:
                continue
            if sanitized['id'] in seen_ids:
                sanitized['id'] = str(uuid.uuid4())
            seen_ids.add(sanitized['id'])
            normalized_cards.append(sanitized)
        is_current = _truthy(col.get('is_current_done', False)) and not seen_current
        seen_current = seen_current or is_current
        normalized_columns.append({'id': col_id, 'title': title, 'color': color, 'hidden': hidden,
                                   'is_done': is_done or is_current, 'is_current_done': is_current,
                                   'cards': normalized_cards})

    _ensure_global_orders(normalized_columns)

    normalized_projects = []
    seen_projects = set()
    for proj in data['projects']:
        if not isinstance(proj, dict):
            continue
        name = (proj.get('name') or '').strip()
        if not name or name in seen_projects:
            continue
        normalized_projects.append({'name': name, 'color': proj.get('color') or DEFAULT_CARD_COLOR})
        seen_projects.add(name)

    data['columns'] = normalized_columns
    data['projects'] = normalized_projects
    if 'sprints' in data:
        data['sprints'] = _clean_sprints(data['sprints'])
    return data


def _clean_sprints(raw):
    """Valid sprint rows from an import payload (unknown fields dropped, at most one active)."""
    out, seen, active = [], set(), False
    for sp in raw if isinstance(raw, list) else []:
        if not isinstance(sp, dict) or not isinstance(sp.get('id'), str) or sp['id'] in seen:
            continue
        end = sp.get('end_date')
        if not isinstance(end, str) or not db.DATE_RE.fullmatch(end):
            continue
        state = sp.get('state') if sp.get('state') in ('planned', 'active', 'closed') else 'planned'
        if state == 'active':
            state, active = ('planned' if active else 'active'), True
        seen.add(sp['id'])
        row = {k: sp.get(k) for k in ('start_date', 'started_at', 'closed_at', 'committed', 'completed', 'carried', 'added')}
        row.update(id=sp['id'], name=str(sp.get('name') or '').strip() or 'Sprint ' + end[5:].replace('-', '/'),
                   goal=str(sp.get('goal') or ''), end_date=end, state=state)
        out.append(row)
    return out


def _source():
    """Who made this change: the X-Perkan-Source header (ui, mcp, ...) or 'api'."""
    return (request.headers.get('X-Perkan-Source') or 'api').strip()[:20] or 'api'


@app.errorhandler(db.SprintError)
def _sprint_error(err):
    return jsonify({'error': str(err)}), err.status


def _sprint_target(conn, ref):
    """Sprint id for a card's `sprint` argument: an id, "active", or ""/"backlog" (= None, the backlog)."""
    if ref in (None, '', 'backlog', 'none'):
        return None
    if ref == 'active':
        sid = db.active_sprint(conn)
        if sid is None:
            raise db.SprintError('there is no active sprint')
        return sid
    sid = db.resolve_sprint(conn, ref)
    if sid is None:
        raise db.SprintError('sprint not found', 404)
    return sid


def _finite_or_none(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _truthy(value):
    return str(value).strip().lower() in ('1', 'true', 'yes', 'on')


def _int_or_none(value):
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _generate_unique_color(conn, attempts=32):
    existing = {(r['color'] or '').lower() for r in conn.execute('SELECT color FROM projects') if r['color']}
    for _ in range(attempts):
        color = f"#{random.randint(0, 0xFFFFFF):06x}"
        if color.lower() not in existing:
            return color
    return f"#{random.randint(0, 0xFFFFFF):06x}"


def _find_project(conn, project_name):
    if not project_name:
        return None
    row = conn.execute('SELECT name, color FROM projects WHERE name = ?', (project_name,)).fetchone()
    return {'name': row['name'], 'color': row['color']} if row else None


def _ensure_project(conn, project_name):
    project_name = (project_name or '').strip()
    if not project_name:
        return None
    existing = _find_project(conn, project_name)
    if existing:
        return existing
    color = _generate_unique_color(conn)
    position = conn.execute('SELECT COALESCE(MAX(position), -1) + 1 FROM projects').fetchone()[0]
    conn.execute('INSERT INTO projects (name, color, position) VALUES (?,?,?)', (project_name, color, position))
    return {'name': project_name, 'color': color}


def _ensure_data_file():
    """Create the schema, importing a legacy kanban.json on first run."""
    db.init(_normalize_board, DEFAULT_BOARD)


def _clean_links(raw_links):
    """Return a list of {'text','url'} objects with minimal validation."""
    cleaned = []
    if isinstance(raw_links, list):
        for item in raw_links:
            if not isinstance(item, dict):
                continue
            text = str(item.get('text', '') or '').strip()
            url = str(item.get('url', '') or '').strip()
            if not url:
                continue
            cleaned.append({'text': text or url, 'url': url})
    return cleaned


def _merge_boards(existing, incoming):
    base = copy.deepcopy(_normalize_board(copy.deepcopy(existing)))
    incoming_board = _normalize_board(copy.deepcopy(incoming))

    columns_lookup = {col['id']: col for col in base.get('columns', [])}
    for inc_col in incoming_board.get('columns', []):
        col_id = inc_col['id']
        if col_id in columns_lookup:
            target = columns_lookup[col_id]
            target['title'] = inc_col.get('title', target['title'])
            target['color'] = inc_col.get('color', target.get('color'))
            target['hidden'] = bool(inc_col.get('hidden', target.get('hidden', False)))
            if 'is_done' in inc_col:
                target['is_done'] = _truthy(inc_col['is_done'])
            existing_ids = {card['id'] for card in target.get('cards', [])}
            for card in inc_col.get('cards', []):
                sanitized = _sanitize_card(card)
                if not sanitized:
                    continue
                if sanitized['id'] in existing_ids:
                    sanitized['id'] = str(uuid.uuid4())
                existing_ids.add(sanitized['id'])
                target.setdefault('cards', []).append(sanitized)
        else:
            base.setdefault('columns', []).append(copy.deepcopy(inc_col))
            columns_lookup[col_id] = base['columns'][-1]

    projects = base.setdefault('projects', [])
    project_lookup = {proj['name']: proj for proj in projects if proj.get('name')}
    for proj in incoming_board.get('projects', []):
        name = proj.get('name')
        if not name:
            continue
        if name in project_lookup:
            if proj.get('color'):
                project_lookup[name]['color'] = proj['color']
        else:
            projects.append({'name': name, 'color': proj.get('color') or DEFAULT_CARD_COLOR})
            project_lookup[name] = projects[-1]

    return _normalize_board(base)



# Make sure the database exists as soon as the app module loads
_ensure_data_file()


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/list')
def list_view():
    return render_template('list.html')


@app.route('/sprints')
def sprints_view():
    return render_template('sprints.html')


@app.route('/settings')
def settings_view():
    return render_template('settings.html')


@app.route('/api/board', methods=['GET'])
def get_board():
    # Hidden columns (and their cards) are left out unless ?include_hidden=true
    with db.read() as conn:
        return jsonify(db.load_board(conn, include_hidden=_truthy(request.args.get('include_hidden')),
                                     include_closed=_truthy(request.args.get('include_closed'))))


@app.route('/api/board/export', methods=['GET'])
def export_board():
    with db.read() as conn:
        board = db.load_board(conn, include_hidden=True, include_closed=True)   # a backup must contain everything
        board['sprints'] = db.list_sprints(conn, include_closed=True)
    return Response(json.dumps(board, indent=2), mimetype='application/json',
                    headers={'Content-Disposition': 'attachment; filename=kanban.json'})


@app.route('/api/board/import', methods=['POST'])
def import_board():
    upload = request.files.get('file')
    if not upload:
        return jsonify({'error': 'Import file is required'}), 400
    try:
        payload = json.load(upload)
    except json.JSONDecodeError:
        return jsonify({'error': 'Uploaded file is not valid JSON'}), 400

    mode = (request.form.get('mode') or 'merge').lower()
    if mode not in {'merge', 'replace'}:
        mode = 'merge'

    with db.write() as conn:
        if mode == 'replace':
            board = _normalize_board(payload)
        else:
            board = _merge_boards(db.load_board(conn, include_hidden=True), payload)
        db.replace_board(conn, board)
    return jsonify({'status': 'ok', 'mode': mode})


@app.route('/api/cards', methods=['GET'])
def get_cards():
    """Cards in global order. Filters: column, project, assignee, query; paging: limit, offset.
    Cards in hidden columns are skipped unless include_hidden=true or `column` names that column."""
    args = request.args
    limit = offset = None
    if args.get('limit') not in (None, ''):
        limit = _int_or_none(args.get('limit'))
        if limit is None or limit < 0:
            return jsonify({'error': 'limit must be a non-negative integer'}), 400
    if args.get('offset') not in (None, ''):
        offset = _int_or_none(args.get('offset'))
        if offset is None or offset < 0:
            return jsonify({'error': 'offset must be a non-negative integer'}), 400
    with db.read() as conn:
        cards, total = db.list_cards(
            conn, column=args.get('column') or None, project=args.get('project') or None,
            assignee=args.get('assignee') or None, query=args.get('query') or None,
            limit=limit, offset=offset or 0, include_hidden=_truthy(args.get('include_hidden')),
            sprint=args.get('sprint') or None, include_closed=_truthy(args.get('include_closed')))
    return jsonify({'cards': cards, 'total': total})


@app.route('/api/card', methods=['POST'])
def create_card():
    data = request.get_json() or {}
    title = data.get('title')
    description = data.get('description', '')
    column_id = data.get('column', 'todo')
    color = data.get('color')
    project_name = (data.get('project') or '').strip()
    assignee = (data.get('assignee') or '').strip()
    links = _clean_links(data.get('links'))
    position = _int_or_none(data.get('position'))  # optional index within the column
    if not title:
        return jsonify({'error': 'title required'}), 400

    with db.write() as conn:
        column_id = db.resolve_column(conn, column_id)
        if column_id is None:
            return jsonify({'error': 'column not found'}), 404
        if 'sprint' in data:
            sprint_id = _sprint_target(conn, data.get('sprint'))
        elif column_id != db.list_columns(conn, include_hidden=True)[0]['id']:
            # created past the first column (work already under way or done): it belongs to the active sprint
            sprint_id = db.active_sprint(conn)
        else:
            sprint_id = None
        card = {
            'id': str(uuid.uuid4()),
            'title': title,
            'description': description,
            'links': links
        }
        if assignee:
            card['assignee'] = assignee
        project_details = None
        if project_name:
            project_details = _ensure_project(conn, project_name)
            card['project'] = project_name
            if project_details and project_details.get('color'):
                card['color'] = project_details['color']

        if color and not project_details:
            card['color'] = color
        if 'color' not in card:
            card['color'] = DEFAULT_CARD_COLOR

        explicit = _finite_or_none(data.get('order'))
        if explicit is not None:
            card['order'] = db.order_for_explicit(conn, explicit)
        elif position is not None:
            card['order'] = db.order_for_position(conn, column_id, position)
        else:
            card['order'] = db.end_order(conn)   # default: after everything else
        card['created_at'] = db.now_iso()
        db.insert_card(conn, column_id, card)
        db.record_move(conn, card['id'], None, column_id, _source(), at=card['created_at'])
        if sprint_id:
            db.set_card_sprint(conn, card['id'], sprint_id, _source())
        card, _col = db.get_card(conn, card['id'])
        return jsonify(card), 201


@app.route('/api/card/<card_id>', methods=['PUT'])
def update_card(card_id):
    data = request.get_json() or {}
    target_col = data.get('column')
    position = _int_or_none(data.get('position'))  # optional integer
    order = data.get('order')
    title = data.get('title')
    description = data.get('description')
    color = data.get('color')
    links = data.get('links')
    project_payload = data.get('project') if 'project' in data else None
    assignee_payload = data.get('assignee') if 'assignee' in data else None

    with db.write() as conn:
        card_obj, original_column_id = db.get_card(conn, card_id)
        if card_obj is None:
            return jsonify({'error': 'card not found'}), 404
        sprint_change = 'sprint' in data
        sprint_id = _sprint_target(conn, data.get('sprint')) if sprint_change else None
        if target_col:
            target_col = db.resolve_column(conn, target_col)
            if target_col is None:
                return jsonify({'error': 'target column not found'}), 404

        destination_column_id = target_col or original_column_id
        if destination_column_id != original_column_id:
            todo_id = db.list_columns(conn, include_hidden=True)[0]['id']
            card_sprint = sprint_id if sprint_change else card_obj.get('sprint')
            if original_column_id == todo_id and (not card_sprint or card_sprint != db.active_sprint(conn)):
                return jsonify({'error': 'only cards in the active sprint can move out of the first column; '
                                         'add the card to the active sprint first'}), 409

        # update fields
        if title is not None:
            card_obj['title'] = title
        if description is not None:
            card_obj['description'] = description
        if color is not None:
            card_obj['color'] = color
        if project_payload is not None:
            normalized_project = (project_payload or '').strip()
            if normalized_project:
                project_details = _ensure_project(conn, normalized_project)
                card_obj['project'] = normalized_project
                if project_details and project_details.get('color'):
                    card_obj['color'] = project_details['color']
            else:
                card_obj.pop('project', None)
                if color is None:
                    card_obj['color'] = DEFAULT_CARD_COLOR
        if assignee_payload is not None:
            normalized_assignee = (assignee_payload or '').strip()
            if normalized_assignee:
                card_obj['assignee'] = normalized_assignee
            else:
                card_obj.pop('assignee', None)
        if links is not None:
            card_obj['links'] = _clean_links(links)
        if card_obj.get('project'):
            project_details = _find_project(conn, card_obj['project'])
            if project_details and project_details.get('color'):
                card_obj['color'] = project_details['color']
        elif 'color' not in card_obj:
            card_obj['color'] = DEFAULT_CARD_COLOR

        explicit = _finite_or_none(order)
        if explicit is not None:
            # an explicit order value wins (kept unique); position is ignored
            card_obj['order'] = db.order_for_explicit(conn, explicit, card_id)
        elif position is not None:
            # index among the destination column's other cards; rewrites only this card's order
            card_obj['order'] = db.order_for_position(conn, destination_column_id, position, card_id)
        # else: a plain column move keeps the card's order, so relative positions survive moves

        db.update_card_row(conn, card_obj, destination_column_id)
        if destination_column_id != original_column_id:
            db.record_move(conn, card_id, original_column_id, destination_column_id, _source())
        if sprint_change:
            db.set_card_sprint(conn, card_id, sprint_id, _source())
        card_obj, _col = db.get_card(conn, card_id)
        return jsonify(card_obj)


@app.route('/api/stats', methods=['GET'])
def stats():
    """Durations in days per card (backlog, work, lead, time in current column) with summaries.
    Filters: project, sprint (id, active or backlog), created_since, completed_since (yyyy-mm-dd or ISO). by_sprint has committed/completed per sprint. cards=false leaves out the per-card rows."""
    args = request.args

    def bound(name):
        value = (args.get(name) or '').strip()
        if not value:
            return None, None
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            value += 'T00:00:00Z'
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z', value):
            return None, f'{name} must be yyyy-mm-dd or an ISO UTC timestamp'
        return value, None

    created_since, err = bound('created_since')
    if err:
        return jsonify({'error': err}), 400
    completed_since, err = bound('completed_since')
    if err:
        return jsonify({'error': err}), 400
    with db.read() as conn:
        result = db.card_stats(conn, project=args.get('project') or None,
                               created_since=created_since, completed_since=completed_since,
                               sprint=args.get('sprint') or None)
    if args.get('cards') is not None and not _truthy(args.get('cards')):
        result.pop('cards')
    return jsonify(result)


@app.route('/api/card/<card_id>/history', methods=['GET'])
def card_history(card_id):
    """Column-change history, oldest first, plus the dates derived from it."""
    with db.read() as conn:
        card, column_id = db.get_card(conn, card_id)
        if card is None:
            return jsonify({'error': 'card not found'}), 404
        history = db.card_history(conn, card_id)
    return jsonify({'id': card_id, 'column': column_id, 'created_at': card.get('created_at'),
                    'started_at': card.get('started_at'), 'completed_at': card.get('completed_at'),
                    'column_since': card.get('column_since'), 'history': history})


@app.route('/api/card/<card_id>', methods=['DELETE'])
def delete_card(card_id):
    with db.write() as conn:
        deleted = conn.execute('DELETE FROM cards WHERE id = ?', (card_id,)).rowcount
    if deleted:
        return jsonify({'deleted': True})
    return jsonify({'error': 'card not found'}), 404


@app.route('/api/columns', methods=['GET'])
def get_columns():
    with db.read() as conn:
        cols = db.list_columns(conn, include_hidden=_truthy(request.args.get('include_hidden')))
        current = db.current_done_column(conn)
    return jsonify({'columns': cols, 'current_done': current})


@app.route('/api/column', methods=['POST'])
def create_column():
    data = request.get_json() or {}
    title = data.get('title')
    position = _int_or_none(data.get('position'))
    color = data.get('color') or '#9aa0a6'
    if not title:
        return jsonify({'error': 'title required'}), 400

    def _slug(s):
        s = s.lower()
        s = re.sub(r'[^a-z0-9]+', '-', s).strip('-')
        return s or str(uuid.uuid4())

    hidden = _truthy(data.get('hidden', False))
    is_current_done = _truthy(data.get('is_current_done', False))
    is_done = _truthy(data.get('is_done', False)) or is_current_done
    with db.write() as conn:
        ids = db.column_ids(conn)
        col_id = _slug(title)
        if col_id in ids:
            col_id = f"{col_id}-{str(uuid.uuid4())[:8]}"
        conn.execute('INSERT INTO columns (id, title, color, hidden, position, is_done) VALUES (?,?,?,?,?,?)',
                     (col_id, title, color, 1 if hidden else 0, len(ids), 1 if is_done else 0))
        if position is not None and position < len(ids):
            ids.insert(max(0, position), col_id)
        else:
            ids.append(col_id)
        db.place_columns(conn, ids)
        if is_current_done:
            db.set_current_done(conn, col_id)
    return jsonify({'id': col_id, 'title': title, 'cards': [], 'color': color, 'hidden': hidden,
                    'is_done': is_done, 'is_current_done': is_current_done}), 201


@app.route('/api/column/<col_id>', methods=['PUT'])
def update_column(col_id):
    data = request.get_json() or {}
    title = data.get('title')
    position = _int_or_none(data.get('position'))
    color = data.get('color')
    hidden = data.get('hidden')
    is_done = data.get('is_done')
    is_current_done = data.get('is_current_done')
    with db.write() as conn:
        ids = db.column_ids(conn)
        if col_id not in ids:
            return jsonify({'error': 'column not found'}), 404
        if title is not None:
            conn.execute('UPDATE columns SET title = ? WHERE id = ?', (title, col_id))
        if color is not None:
            conn.execute('UPDATE columns SET color = ? WHERE id = ?', (color, col_id))
        if hidden is not None:
            conn.execute('UPDATE columns SET hidden = ? WHERE id = ?', (1 if hidden else 0, col_id))
        if is_done is not None:
            conn.execute('UPDATE columns SET is_done = ? WHERE id = ?', (1 if _truthy(is_done) else 0, col_id))
            if not _truthy(is_done):  # a column that is not a done column cannot be the current one
                conn.execute('UPDATE columns SET is_current_done = 0 WHERE id = ?', (col_id,))
        if is_current_done is not None:
            if _truthy(is_current_done):
                db.set_current_done(conn, col_id)
            else:
                conn.execute('UPDATE columns SET is_current_done = 0 WHERE id = ?', (col_id,))
        if position is not None:
            ids.remove(col_id)
            ids.insert(min(max(0, position), len(ids)), col_id)
            db.place_columns(conn, ids)
        col = next(c for c in db.load_board(conn, include_hidden=True)['columns'] if c['id'] == col_id)
    return jsonify(col)


@app.route('/api/column/<col_id>', methods=['DELETE'])
def delete_column(col_id):
    data = request.get_json() or {}
    move_to = data.get('move_to')
    with db.write() as conn:
        ids = db.column_ids(conn)
        if col_id not in ids:
            return jsonify({'error': 'column not found'}), 404
        ids.remove(col_id)
        if move_to and move_to in ids:
            for (cid,) in conn.execute('SELECT id FROM cards WHERE column_id = ?', (col_id,)).fetchall():
                db.record_move(conn, cid, col_id, move_to, _source())
            conn.execute('UPDATE cards SET column_id = ? WHERE column_id = ?', (move_to, col_id))  # order values are kept
        conn.execute('DELETE FROM columns WHERE id = ?', (col_id,))  # cascades to any cards left behind
        db.place_columns(conn, ids)
    return jsonify({'deleted': True})


# ---------------------------------------------------------------- sprints

@app.route('/api/sprints', methods=['GET'])
def get_sprints():
    """Active sprint first, then planned by end date; closed sprints (newest first) only with include_closed=true."""
    with db.read() as conn:
        return jsonify({'sprints': db.list_sprints(conn, include_closed=_truthy(request.args.get('include_closed'))),
                        'today': db.today()})


@app.route('/api/sprint', methods=['POST'])
def create_sprint():
    data = request.get_json() or {}
    if not data.get('end_date'):
        return jsonify({'error': 'end_date required (yyyy-mm-dd; the sprint ends at the end of that day)'}), 400
    with db.write() as conn:
        sid = db.create_sprint(conn, data['end_date'], name=data.get('name'), goal=data.get('goal') or '',
                               start_date=data.get('start_date') or None)
        sprint = db.get_sprint(conn, sid)
        if _truthy(data.get('start')):
            db.start_sprint(conn, sid)
            sprint = db.get_sprint(conn, sid)
    return jsonify(sprint), 201


@app.route('/api/sprint/<sprint_id>', methods=['GET'])
def get_sprint(sprint_id):
    with db.read() as conn:
        sprint = db.get_sprint(conn, sprint_id)
    if sprint is None:
        return jsonify({'error': 'sprint not found'}), 404
    return jsonify(sprint)


@app.route('/api/sprint/<sprint_id>', methods=['PUT'])
def update_sprint(sprint_id):
    data = request.get_json() or {}
    with db.write() as conn:
        db.update_sprint(conn, sprint_id, name=data.get('name'), goal=data.get('goal'),
                         start_date=data.get('start_date'), end_date=data.get('end_date'))
        return jsonify(db.get_sprint(conn, sprint_id))


@app.route('/api/sprint/<sprint_id>', methods=['DELETE'])
def delete_sprint(sprint_id):
    with db.write() as conn:
        db.delete_sprint(conn, sprint_id)
    return jsonify({'deleted': True})


@app.route('/api/sprint/<sprint_id>/start', methods=['POST'])
def start_sprint(sprint_id):
    data = request.get_json(silent=True) or {}
    with db.write() as conn:
        db.start_sprint(conn, sprint_id, start_date=data.get('start_date') or None)
        return jsonify(db.get_sprint(conn, sprint_id))


@app.route('/api/sprint/<sprint_id>/complete', methods=['POST'])
def complete_sprint(sprint_id):
    data = request.get_json(silent=True) or {}
    with db.write() as conn:
        result = db.complete_sprint(
            conn, sprint_id, unfinished=data.get('unfinished') or 'carry_over',
            next_sprint=data.get('next_sprint') or None, next_end_date=data.get('next_end_date') or None,
            next_name=data.get('next_name') or None, start_next=_truthy(data.get('start_next', False)),
            source=_source())
    return jsonify(result)


@app.route('/api/card/<card_id>/sprint-history', methods=['GET'])
def card_sprint_history(card_id):
    with db.read() as conn:
        card, _col = db.get_card(conn, card_id)
        if card is None:
            return jsonify({'error': 'card not found'}), 404
        return jsonify({'id': card_id, 'sprint': card.get('sprint'), 'history': db.card_sprint_history(conn, card_id)})


@app.route('/api/projects', methods=['GET'])
def get_projects():
    with db.read() as conn:
        projects = [{'name': r['name'], 'color': r['color']}
                    for r in conn.execute('SELECT name, color FROM projects ORDER BY position')]
    return jsonify({'projects': projects})


@app.route('/api/project', methods=['POST'])
def create_project():
    data = request.get_json() or {}
    name = (data.get('name') or '').strip()
    color = data.get('color')
    position = _int_or_none(data.get('position'))
    if not name:
        return jsonify({'error': 'name required'}), 400

    with db.write() as conn:
        names = db.project_names(conn)
        if name in names:
            return jsonify({'error': 'project name must be unique'}), 400
        if not color:
            color = _generate_unique_color(conn)
        conn.execute('INSERT INTO projects (name, color, position) VALUES (?,?,?)', (name, color, len(names)))
        if position is None or position >= len(names):
            names.append(name)
        else:
            names.insert(max(0, position), name)
        db.place_projects(conn, names)
    return jsonify({'name': name, 'color': color}), 201


@app.route('/api/project/<int:project_idx>', methods=['PUT'])
def update_project(project_idx):
    data = request.get_json() or {}
    name = data.get('name')
    color = data.get('color')
    position = _int_or_none(data.get('position'))

    with db.write() as conn:
        names = db.project_names(conn)
        if project_idx < 0 or project_idx >= len(names):
            return jsonify({'error': 'project not found'}), 404
        current = names[project_idx]

        if name is not None:
            name = name.strip()
            if not name:
                return jsonify({'error': 'name required'}), 400
            if any(i != project_idx and n == name for i, n in enumerate(names)):
                return jsonify({'error': 'project name must be unique'}), 400
            if name != current:
                conn.execute('UPDATE projects SET name = ? WHERE name = ?', (name, current))
                conn.execute('UPDATE cards SET project = ?, color = (SELECT color FROM projects WHERE name = ?) WHERE project = ?',
                             (name, name, current))
                names[project_idx] = name
                current = name
        if color is not None:
            conn.execute('UPDATE projects SET color = ? WHERE name = ?', (color, current))
            conn.execute('UPDATE cards SET color = ? WHERE project = ?', (color or DEFAULT_CARD_COLOR, current))

        if position is not None:
            names.pop(project_idx)
            names.insert(min(max(0, position), len(names)), current)
            db.place_projects(conn, names)
        project = _find_project(conn, current)
    return jsonify(project)


@app.route('/api/project/<int:project_idx>', methods=['DELETE'])
def delete_project(project_idx):
    with db.write() as conn:
        names = db.project_names(conn)
        if project_idx < 0 or project_idx >= len(names):
            return jsonify({'error': 'project not found'}), 404
        removed = names.pop(project_idx)
        conn.execute('UPDATE cards SET project = NULL, color = ? WHERE project = ?', (DEFAULT_CARD_COLOR, removed))
        conn.execute('DELETE FROM projects WHERE name = ?', (removed,))
        db.place_projects(conn, names)
    return jsonify({'deleted': True})


from mcp_server import mcp_bp  # noqa: E402
app.register_blueprint(mcp_bp)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
