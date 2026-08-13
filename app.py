from flask import Flask, jsonify, request, render_template, abort, send_file, redirect, session
import json
import os
import secrets
import threading
import uuid
import re
import time
import datetime
import logging
import errno
import shutil
import random
import copy

import gcal
import users

app = Flask(__name__, static_folder='static', template_folder='templates')
# Basic logging for debugging slow I/O
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DATA_DIR = os.path.join(os.path.dirname(__file__), 'data')
DATA_FILE = os.path.join(DATA_DIR, 'kanban.json')
SECRET_KEY_FILE = os.path.join(DATA_DIR, 'secret_key.txt')
_lock = threading.Lock()


def _load_secret_key():
    """Session cookies need a stable key across restarts and across
    gunicorn worker processes, so persist one to disk instead of
    generating a fresh one per process (which would invalidate every
    session on every restart/reload)."""
    env_key = os.environ.get('SECRET_KEY')
    if env_key:
        return env_key
    if not os.path.exists(DATA_DIR):
        os.makedirs(DATA_DIR)
    if os.path.exists(SECRET_KEY_FILE):
        with open(SECRET_KEY_FILE, 'r', encoding='utf-8') as f:
            existing = f.read().strip()
            if existing:
                return existing
    new_key = secrets.token_hex(32)
    tmp = SECRET_KEY_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(new_key)
    os.replace(tmp, SECRET_KEY_FILE)
    return new_key


app.secret_key = _load_secret_key()

# Routes reachable without an authenticated session. gcal_sso/gcal_callback
# must be reachable while logged out too, since "Sign in with Google" IS
# the login for a not-yet-authenticated visitor.
PUBLIC_ENDPOINTS = {'login', 'static', 'gcal_sso', 'gcal_callback'}


@app.before_request
def _require_login():
    if request.endpoint in PUBLIC_ENDPOINTS or request.endpoint is None:
        return None
    user_id = session.get('user_id')
    if user_id and users.get_user(user_id):
        return None
    if request.path.startswith('/api/'):
        return jsonify({'error': 'login required'}), 401
    return redirect('/login')

DEFAULT_CARD_COLOR = '#5b2e8a'

DEFAULT_BOARD = {
    "columns": [
    {"id": "todo", "title": "To Do", "cards": [], "color": "#1f77b4", "hidden": False},
    {"id": "inprogress", "title": "In Progress", "cards": [], "color": "#ff8c00", "hidden": False},
    {"id": "blocked", "title": "Blocked", "cards": [], "color": "#d62728", "hidden": False},
    {"id": "done", "title": "Done", "cards": [], "color": "#2ca02c", "hidden": False}
    ],
    "projects": []
}


def _sanitize_due_date(value, all_day):
    if not value:
        return None
    value = str(value).strip()
    if not value:
        return None
    try:
        if all_day:
            datetime.date.fromisoformat(value)
        else:
            # Accept 'YYYY-MM-DDTHH:MM' or with seconds/offset
            datetime.datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    return value


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
    assigned_user_id = (card.get('assigned_user_id') or '').strip()
    if assigned_user_id and users.get_user(assigned_user_id):
        sanitized['assigned_user_id'] = assigned_user_id
    color = card.get('color')
    sanitized['color'] = color if color else DEFAULT_CARD_COLOR
    order = card.get('order')
    if order is not None:
        try:
            sanitized['order'] = float(order)
        except (TypeError, ValueError):
            pass

    all_day = bool(card.get('all_day', False))
    due_date = _sanitize_due_date(card.get('due_date'), all_day)
    if due_date:
        sanitized['due_date'] = due_date
        sanitized['all_day'] = all_day

    # Sync bookkeeping fields are only ever written by our own code paths,
    # but pass them through on import/merge so round-tripping an export
    # doesn't strand cards that are already linked to a calendar event.
    gcal_event_id = card.get('gcal_event_id')
    if gcal_event_id:
        sanitized['gcal_event_id'] = str(gcal_event_id)
    gcal_updated = card.get('gcal_updated')
    if gcal_updated:
        sanitized['gcal_updated'] = str(gcal_updated)
    gcal_user_id = card.get('gcal_user_id')
    if gcal_user_id:
        sanitized['gcal_user_id'] = str(gcal_user_id)
    updated_at = card.get('updated_at')
    if updated_at is not None:
        try:
            sanitized['updated_at'] = float(updated_at)
        except (TypeError, ValueError):
            pass

    return sanitized


def _normalize_board(data):
    if not isinstance(data, dict):
        data = {}
    if 'columns' not in data or not isinstance(data['columns'], list):
        data['columns'] = []
    if 'projects' not in data or not isinstance(data['projects'], list):
        data['projects'] = []

    normalized_columns = []
    for col in data['columns']:
        if not isinstance(col, dict):
            continue
        col_id = col.get('id') or str(uuid.uuid4())
        title = str(col.get('title') or '').strip() or 'Untitled'
        color = col.get('color') or '#9aa0a6'
        hidden = bool(col.get('hidden', False))
        cards_payload = col.get('cards') if isinstance(col.get('cards'), list) else []
        normalized_cards = []
        seen_ids = set()
        for card in cards_payload:
            sanitized = _sanitize_card(card)
            if not sanitized:
                continue
            if sanitized['id'] in seen_ids:
                sanitized['id'] = str(uuid.uuid4())
            seen_ids.add(sanitized['id'])
            normalized_cards.append(sanitized)
        # assign order to any card that lacks one
        for _i, _card in enumerate(normalized_cards):
            if 'order' not in _card:
                _card['order'] = float((_i + 1) * 1_000_000)
        normalized_columns.append({'id': col_id, 'title': title, 'color': color, 'hidden': hidden, 'cards': normalized_cards})

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
    return data


def _get_projects(board):
    projects = board.get('projects')
    if not isinstance(projects, list):
        projects = []
        board['projects'] = projects
    return projects


def _generate_unique_color(board, attempts=32):
    existing = { (proj.get('color') or '').lower() for proj in _get_projects(board) if proj.get('color') }
    for _ in range(attempts):
        color = f"#{random.randint(0, 0xFFFFFF):06x}"
        if color.lower() not in existing:
            return color
    return f"#{random.randint(0, 0xFFFFFF):06x}"


def _ensure_project(board, project_name):
    project_name = (project_name or '').strip()
    if not project_name:
        return None
    projects = _get_projects(board)
    existing = next((p for p in projects if p.get('name') == project_name), None)
    if existing:
        if not existing.get('color'):
            existing['color'] = DEFAULT_CARD_COLOR
        return existing
    color = _generate_unique_color(board)
    project = {'name': project_name, 'color': color}
    projects.append(project)
    return project


def _find_project(board, project_name):
    if not project_name:
        return None
    projects = _get_projects(board)
    for proj in projects:
        if proj.get('name') == project_name:
            return proj
    return None


def _apply_project_color_to_cards(board, project_name, color):
    if not project_name:
        return
    for col in board.get('columns', []):
        for card in col.get('cards', []):
            if card.get('project') == project_name:
                if color:
                    card['color'] = color
                else:
                    card['color'] = DEFAULT_CARD_COLOR


def _update_project_references(board, old_name, new_name=None, project_color=None):
    for col in board.get('columns', []):
        for card in col.get('cards', []):
            if card.get('project') == old_name:
                if new_name:
                    card['project'] = new_name
                    if project_color:
                        card['color'] = project_color
                else:
                    card.pop('project', None)
                    card['color'] = DEFAULT_CARD_COLOR


def _ensure_data_file():
    if not os.path.exists(DATA_DIR):
        os.makedirs(DATA_DIR)
    if not os.path.exists(DATA_FILE):
        logger.info('Creating new blank kanban board at %s', DATA_FILE)
        _save_data(DEFAULT_BOARD)


def _load_data():
    _ensure_data_file()
    start = time.perf_counter()
    with open(DATA_FILE, 'r', encoding='utf-8') as f:
        data = json.load(f)
        data = _normalize_board(data)
    elapsed = time.perf_counter() - start
    if elapsed > 0.5:
        logger.warning('Slow _load_data: %.3fs', elapsed)
    return data


def _save_data(data):
    # Use lock to avoid concurrent writes from threads
    with _lock:
        start = time.perf_counter()
        tmp = DATA_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
        try:
            os.replace(tmp, DATA_FILE)
        except OSError as exc:
            recoverable = {errno.EXDEV, errno.EBUSY, errno.EACCES, errno.EPERM}
            if exc.errno in recoverable:
                shutil.copyfile(tmp, DATA_FILE)
                os.remove(tmp)
            else:
                os.remove(tmp)
                raise
        elapsed = time.perf_counter() - start
        if elapsed > 0.5:
            logger.warning('Slow _save_data: %.3fs', elapsed)


# Ensure the data file exists as soon as the app module loads
_ensure_data_file()


def _push_card_to_gcal(card):
    """Best-effort push of a card's due date to its assigned user's
    Google Calendar. A card only syncs when it has both a due_date and
    an assigned_user_id for a user who has connected their calendar;
    reassigning a card moves its event from the old user's calendar to
    the new one.

    Sync failures must never block saving the card locally, so errors
    are logged and swallowed. Returns True if the card was mutated
    (gcal_event_id/gcal_updated/gcal_user_id changed) and needs to be
    persisted again.
    """
    assigned_user_id = card.get('assigned_user_id')
    needs_event = bool(card.get('due_date')) and bool(assigned_user_id)
    stale_link = bool(card.get('gcal_event_id')) and (
        not needs_event or card.get('gcal_user_id') != assigned_user_id
    )
    changed = False
    try:
        if stale_link:
            gcal.delete_event_for_card(card)
            card.pop('gcal_event_id', None)
            card.pop('gcal_updated', None)
            card.pop('gcal_user_id', None)
            changed = True
        if needs_event:
            changed = gcal.upsert_event_for_card(card, assigned_user_id) or changed
    except gcal.GcalError as exc:
        logger.warning('Google Calendar push sync failed for card %s: %s', card.get('id'), exc)
    return changed


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



@app.route('/login', methods=['GET', 'POST'])
def login():
    bootstrap = users.count_users() == 0
    if session.get('user_id') and users.get_user(session['user_id']):
        return redirect('/')

    error = None
    gcal_error = request.args.get('gcal_error')
    if gcal_error:
        error = f'Google sign-in failed ({gcal_error}). You can try again or use a local account below.'
    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()
        password = request.form.get('password') or ''
        display_name = (request.form.get('display_name') or '').strip()

        if bootstrap:
            try:
                user = users.create_user(username, password, display_name)
                session['user_id'] = user['id']
                return redirect('/')
            except users.UserError as exc:
                error = str(exc)
        else:
            user = users.verify_login(username, password)
            if user:
                session['user_id'] = user['id']
                return redirect('/')
            error = 'invalid username or password'

    return render_template('login.html', bootstrap=bootstrap, error=error, google_configured=gcal.is_configured())


@app.route('/logout', methods=['POST'])
def logout():
    session.clear()
    return redirect('/login')


@app.route('/api/session', methods=['GET'])
def api_session():
    user = users.get_user(session.get('user_id'))
    return jsonify({'user': user})


@app.route('/api/users', methods=['GET'])
def list_users():
    return jsonify({'users': users.list_users()})


@app.route('/api/users', methods=['POST'])
def create_user():
    data = request.get_json() or {}
    try:
        user = users.create_user(data.get('username'), data.get('password'), data.get('display_name'))
    except users.UserError as exc:
        return jsonify({'error': str(exc)}), 400
    return jsonify(user), 201


@app.route('/api/users/<user_id>', methods=['PUT'])
def update_user(user_id):
    data = request.get_json() or {}
    password = data.get('password')
    if password is not None and session.get('user_id') != user_id:
        return jsonify({'error': 'you can only change your own password'}), 403
    try:
        user = users.update_user(user_id, display_name=data.get('display_name'), password=password)
    except users.UserError as exc:
        return jsonify({'error': str(exc)}), 400
    return jsonify(user)


@app.route('/api/users/<user_id>', methods=['DELETE'])
def delete_user(user_id):
    if session.get('user_id') == user_id:
        return jsonify({'error': "you can't delete the account you're logged in as"}), 400
    if not users.get_user(user_id):
        return jsonify({'error': 'user not found'}), 404

    board = _load_data()
    changed = False
    for col in board.get('columns', []):
        for card in col.get('cards', []):
            if card.get('assigned_user_id') == user_id:
                card.pop('assigned_user_id', None)
                changed = True
            if card.get('gcal_user_id') == user_id:
                try:
                    gcal.delete_event_for_card(card)
                except gcal.GcalError as exc:
                    logger.warning('Google Calendar cleanup failed while deleting user %s: %s', user_id, exc)
                card.pop('gcal_event_id', None)
                card.pop('gcal_updated', None)
                card.pop('gcal_user_id', None)
                changed = True
    if changed:
        _save_data(board)

    gcal.disconnect(user_id)
    users.delete_user(user_id)
    return jsonify({'deleted': True})


@app.route('/')
def index():
    current_user = users.get_user(session.get('user_id'))
    return render_template('index.html', current_user=current_user)


@app.route('/list')
def list_view():
    current_user = users.get_user(session.get('user_id'))
    return render_template('list.html', current_user=current_user)


@app.route('/api/board', methods=['GET'])
def get_board():
    return jsonify(_load_data())


@app.route('/api/board/export', methods=['GET'])
def export_board():
    _ensure_data_file()
    return send_file(DATA_FILE, mimetype='application/json', as_attachment=True, download_name='kanban.json')


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

    if mode == 'replace':
        board = _normalize_board(payload)
    else:
        current = _load_data()
        board = _merge_boards(current, payload)

    _save_data(board)
    return jsonify({'status': 'ok', 'mode': mode})


@app.route('/api/card', methods=['POST'])
def create_card():
    data = request.get_json() or {}
    title = data.get('title')
    description = data.get('description', '')
    column_id = data.get('column', 'todo')
    color = data.get('color')
    hidden = data.get('hidden')
    project_name = (data.get('project') or '').strip()
    assignee = (data.get('assignee') or '').strip()
    assigned_user_id = (data.get('assigned_user_id') or '').strip()
    links = _clean_links(data.get('links'))
    all_day = bool(data.get('all_day', False))
    due_date = _sanitize_due_date(data.get('due_date'), all_day)
    if not title:
        return jsonify({'error': 'title required'}), 400
    if assigned_user_id and not users.get_user(assigned_user_id):
        return jsonify({'error': 'assigned user not found'}), 400

    board = _load_data()
    card = {
        'id': str(uuid.uuid4()),
        'title': title,
        'description': description,
        'links': links,
        'updated_at': time.time(),
    }
    if due_date:
        card['due_date'] = due_date
        card['all_day'] = all_day
    if assignee:
        card['assignee'] = assignee
    if assigned_user_id:
        card['assigned_user_id'] = assigned_user_id
    project_details = None
    if project_name:
        project_details = _ensure_project(board, project_name)
        card['project'] = project_name
        if project_details and project_details.get('color'):
            card['color'] = project_details['color']

    if color and not project_details:
        card['color'] = color
    if 'color' not in card:
        card['color'] = DEFAULT_CARD_COLOR

    for col in board['columns']:
        if col['id'] == column_id:
            existing_orders = [c['order'] for c in col['cards'] if isinstance(c.get('order'), (int, float))]
            card['order'] = float(max(existing_orders, default=0) + 1_000_000)
            col['cards'].append(card)
            _save_data(board)
            if _push_card_to_gcal(card):
                _save_data(board)
            return jsonify(card), 201
    return jsonify({'error': 'column not found'}), 404


@app.route('/api/card/<card_id>', methods=['PUT'])
def update_card(card_id):
    data = request.get_json() or {}
    target_col = data.get('column')
    position = data.get('position')  # optional integer
    order = data.get('order')
    title = data.get('title')
    description = data.get('description')
    color = data.get('color')
    links = data.get('links')
    project_payload = data.get('project') if 'project' in data else None
    assignee_payload = data.get('assignee') if 'assignee' in data else None
    assigned_user_payload = data.get('assigned_user_id') if 'assigned_user_id' in data else None
    clear_assigned_user = 'assigned_user_id' in data and not str(data.get('assigned_user_id') or '').strip()
    due_date_payload = data.get('due_date') if 'due_date' in data else None
    all_day_payload = bool(data.get('all_day', False))
    clear_due_date = 'due_date' in data and not str(data.get('due_date') or '').strip()

    board = _load_data()
    # find and remove card from any column
    card_obj = None
    original_column_id = None
    original_position = None
    for col in board['columns']:
        for i, c in enumerate(col['cards']):
            if c['id'] == card_id:
                card_obj = c
                original_column_id = col['id']
                original_position = i
                del col['cards'][i]
                break
        if card_obj:
            break

    if not card_obj:
        return jsonify({'error': 'card not found'}), 404

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
            project_details = _ensure_project(board, normalized_project)
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
    if clear_assigned_user:
        card_obj.pop('assigned_user_id', None)
    elif assigned_user_payload is not None:
        normalized_user_id = (assigned_user_payload or '').strip()
        if normalized_user_id and users.get_user(normalized_user_id):
            card_obj['assigned_user_id'] = normalized_user_id
        elif normalized_user_id:
            return jsonify({'error': 'assigned user not found'}), 400
    if links is not None:
        card_obj['links'] = _clean_links(links)
    if order is not None:
        try:
            card_obj['order'] = float(order)
        except (TypeError, ValueError):
            pass
    if clear_due_date:
        card_obj.pop('due_date', None)
        card_obj.pop('all_day', None)
    elif due_date_payload is not None:
        due_date = _sanitize_due_date(due_date_payload, all_day_payload)
        if due_date:
            card_obj['due_date'] = due_date
            card_obj['all_day'] = all_day_payload
    card_obj['updated_at'] = time.time()

    if card_obj.get('project'):
        project_details = _find_project(board, card_obj['project'])
        if project_details and project_details.get('color'):
            card_obj['color'] = project_details['color']
    elif 'color' not in card_obj:
        card_obj['color'] = DEFAULT_CARD_COLOR

    # place into target column
    destination_column_id = target_col or original_column_id
    destination_column = None
    if destination_column_id:
        destination_column = next((c for c in board['columns'] if c['id'] == destination_column_id), None)

    if target_col and destination_column is None:
        return jsonify({'error': 'target column not found'}), 404

    if destination_column is None:
        destination_column = board['columns'][0]

    if target_col:
        # honor supplied position when explicitly moving columns
        if position is None or position >= len(destination_column['cards']):
            destination_column['cards'].append(card_obj)
        else:
            destination_column['cards'].insert(max(0, int(position)), card_obj)
    else:
        # keep original relative order when staying in same column
        insert_idx = original_position if original_position is not None else len(destination_column['cards'])
        insert_idx = min(insert_idx, len(destination_column['cards']))
        destination_column['cards'].insert(insert_idx, card_obj)

    _save_data(board)
    if _push_card_to_gcal(card_obj):
        _save_data(board)
    return jsonify(card_obj)


@app.route('/api/card/<card_id>', methods=['DELETE'])
def delete_card(card_id):
    board = _load_data()
    for col in board['columns']:
        for i, c in enumerate(col['cards']):
            if c['id'] == card_id:
                try:
                    gcal.delete_event_for_card(c)
                except gcal.GcalError as exc:
                    logger.warning('Google Calendar delete sync failed for card %s: %s', card_id, exc)
                del col['cards'][i]
                _save_data(board)
                return jsonify({'deleted': True})
    return jsonify({'error': 'card not found'}), 404


@app.route('/api/columns', methods=['GET'])
def get_columns():
    board = _load_data()
    cols = [{'id': c['id'], 'title': c['title'], 'color': c.get('color'), 'hidden': bool(c.get('hidden', False))} for c in board['columns']]
    return jsonify({'columns': cols})


@app.route('/api/column', methods=['POST'])
def create_column():
    data = request.get_json() or {}
    title = data.get('title')
    position = data.get('position')
    color = data.get('color') or '#9aa0a6'
    if not title:
        return jsonify({'error': 'title required'}), 400
    board = _load_data()

    def _slug(s):
        s = s.lower()
        s = re.sub(r'[^a-z0-9]+', '-', s).strip('-')
        return s or str(uuid.uuid4())

    col_id = _slug(title)
    ids = {c['id'] for c in board['columns']}
    if col_id in ids:
        col_id = f"{col_id}-{str(uuid.uuid4())[:8]}"

    hidden = bool(data.get('hidden', False))
    col = {'id': col_id, 'title': title, 'cards': [], 'color': color, 'hidden': hidden}
    if position is None or position >= len(board['columns']):
        board['columns'].append(col)
    else:
        board['columns'].insert(max(0, int(position)), col)
    _save_data(board)
    return jsonify(col), 201


@app.route('/api/column/<col_id>', methods=['PUT'])
def update_column(col_id):
    data = request.get_json() or {}
    title = data.get('title')
    position = data.get('position')
    color = data.get('color')
    hidden = data.get('hidden')
    board = _load_data()
    idx = next((i for i, c in enumerate(board['columns']) if c['id'] == col_id), None)
    if idx is None:
        return jsonify({'error': 'column not found'}), 404
    col = board['columns'][idx]
    if title is not None:
        col['title'] = title
    if color is not None:
        col['color'] = color
    if hidden is not None:
        col['hidden'] = bool(hidden)
    if position is not None:
        pos = max(0, int(position))
        board['columns'].pop(idx)
        board['columns'].insert(min(pos, len(board['columns'])), col)
    _save_data(board)
    return jsonify(col)


@app.route('/api/column/<col_id>', methods=['DELETE'])
def delete_column(col_id):
    data = request.get_json() or {}
    move_to = data.get('move_to')
    board = _load_data()
    idx = next((i for i, c in enumerate(board['columns']) if c['id'] == col_id), None)
    if idx is None:
        return jsonify({'error': 'column not found'}), 404
    col = board['columns'].pop(idx)
    if move_to:
        target = next((c for c in board['columns'] if c['id'] == move_to), None)
        if target is not None:
            target['cards'].extend(col.get('cards', []))
    _save_data(board)
    return jsonify({'deleted': True})


@app.route('/api/projects', methods=['GET'])
def get_projects():
    board = _load_data()
    projects = _get_projects(board)
    return jsonify({'projects': projects})


@app.route('/api/project', methods=['POST'])
def create_project():
    data = request.get_json() or {}
    name = (data.get('name') or '').strip()
    color = data.get('color')
    position = data.get('position')
    if not name:
        return jsonify({'error': 'name required'}), 400

    board = _load_data()
    projects = _get_projects(board)
    if any(proj.get('name') == name for proj in projects):
        return jsonify({'error': 'project name must be unique'}), 400
    if not color:
        color = _generate_unique_color(board)
    project = {'name': name, 'color': color}

    if position is None or position >= len(projects):
        projects.append(project)
    else:
        insert_idx = max(0, int(position))
        projects.insert(insert_idx, project)

    _save_data(board)
    return jsonify(project), 201


@app.route('/api/project/<int:project_idx>', methods=['PUT'])
def update_project(project_idx):
    data = request.get_json() or {}
    board = _load_data()
    projects = _get_projects(board)

    if project_idx < 0 or project_idx >= len(projects):
        return jsonify({'error': 'project not found'}), 404

    project = projects[project_idx]
    name = data.get('name')
    color = data.get('color')
    position = data.get('position')

    if name is not None:
        name = name.strip()
        if not name:
            return jsonify({'error': 'name required'}), 400
        if any(i != project_idx and p.get('name') == name for i, p in enumerate(projects)):
            return jsonify({'error': 'project name must be unique'}), 400
        old_name = project.get('name')
        if name != old_name:
            project['name'] = name
            _update_project_references(board, old_name, name, project.get('color'))
    if color is not None:
        project['color'] = color
        _apply_project_color_to_cards(board, project['name'], color)

    if position is not None:
        mover = projects.pop(project_idx)
        insert_idx = max(0, int(position))
        if insert_idx > len(projects):
            insert_idx = len(projects)
        projects.insert(insert_idx, mover)

    _save_data(board)
    return jsonify(project)


@app.route('/api/project/<int:project_idx>', methods=['DELETE'])
def delete_project(project_idx):
    board = _load_data()
    projects = _get_projects(board)

    if project_idx < 0 or project_idx >= len(projects):
        return jsonify({'error': 'project not found'}), 404

    removed = projects.pop(project_idx)
    _update_project_references(board, removed.get('name'), None)
    _save_data(board)
    return jsonify({'deleted': True})


def _push_pending_cards_for_user(board, user_id):
    """After a user connects their calendar, catch up any cards already
    assigned to them (with a due date) that never got synced because
    they weren't connected yet."""
    changed = False
    for col in board.get('columns', []):
        for card in col.get('cards', []):
            if card.get('assigned_user_id') != user_id or not card.get('due_date'):
                continue
            if card.get('gcal_event_id') and card.get('gcal_user_id') == user_id:
                continue
            if _push_card_to_gcal(card):
                changed = True
    return changed


@app.route('/api/gcal/status', methods=['GET'])
def gcal_status():
    if not session.get('user_id'):
        return jsonify({'error': 'login required'}), 401
    return jsonify(gcal.status(session['user_id']))


@app.route('/auth/google/login', methods=['GET'])
def gcal_login():
    if not gcal.is_configured():
        abort(400, description='GOOGLE_CLIENT_ID/GOOGLE_CLIENT_SECRET are not configured on the server')
    return redirect(gcal.get_auth_url(session['user_id']))


@app.route('/auth/google/sso', methods=['GET'])
def gcal_sso():
    """Start of 'Sign in with Google' for a not-yet-logged-in visitor.
    If they're already logged in, there's nothing to sign into — send
    them to the ordinary connect-my-calendar flow instead."""
    if session.get('user_id') and users.get_user(session['user_id']):
        return redirect('/auth/google/login')
    if not gcal.is_configured():
        abort(400, description='GOOGLE_CLIENT_ID/GOOGLE_CLIENT_SECRET are not configured on the server')
    url, state = gcal.build_sso_auth_url()
    session['sso_state'] = state
    return redirect(url)


@app.route('/auth/google/callback', methods=['GET'])
def gcal_callback():
    error = request.args.get('error')
    if error:
        was_logged_in = bool(session.get('user_id') and users.get_user(session['user_id']))
        session.pop('sso_state', None)
        destination = '/' if was_logged_in else '/login'
        return redirect(f'{destination}?gcal_error={error}')

    code = request.args.get('code')
    state = request.args.get('state')
    if not code:
        abort(400, description='missing authorization code')

    existing_user_id = session.get('user_id')
    if existing_user_id and users.get_user(existing_user_id):
        # "Connect my calendar" — the visitor was already logged in via a
        # local account (or a previous Google sign-in) before starting this.
        if not gcal.verify_state(existing_user_id, state):
            abort(400, description='invalid or expired OAuth state')
        try:
            token, identity = gcal.exchange_code(existing_user_id, code)
        except Exception as exc:
            logger.warning('Google Calendar OAuth exchange failed for user %s: %s', existing_user_id, exc)
            return redirect('/?gcal_error=exchange_failed')
        if identity.get('id'):
            users.link_google_account(existing_user_id, identity['id'], identity.get('email'))

        board = _load_data()
        if _push_pending_cards_for_user(board, existing_user_id):
            _save_data(board)
        return redirect('/?gcal_connected=1')

    # "Sign in with Google" — no one was logged in yet.
    pending_state = session.pop('sso_state', None)
    if not pending_state or pending_state != state:
        abort(400, description='invalid or expired OAuth state')
    try:
        payload, identity = gcal.exchange_code_for_sso(code)
    except Exception as exc:
        logger.warning('Google sign-in exchange failed: %s', exc)
        return redirect('/login?gcal_error=exchange_failed')
    if not identity.get('id'):
        return redirect('/login?gcal_error=no_identity')

    user = users.find_by_google_id(identity['id'])
    if not user:
        base_username = (identity.get('email') or 'user').split('@')[0]
        username = users.generate_unique_username(base_username)
        user = users.create_user(
            username, password=None,
            display_name=identity.get('name') or username,
            google_id=identity['id'], google_email=identity.get('email'))

    gcal.save_token_for_user(user['id'], payload, identity)
    session['user_id'] = user['id']

    board = _load_data()
    if _push_pending_cards_for_user(board, user['id']):
        _save_data(board)
    return redirect('/?gcal_connected=1')


@app.route('/auth/google/disconnect', methods=['POST'])
def gcal_disconnect():
    gcal.disconnect(session['user_id'])
    return jsonify({'disconnected': True})


@app.route('/api/gcal/sync', methods=['POST'])
def gcal_sync_now():
    user_id = session['user_id']
    if not gcal.is_connected(user_id):
        return jsonify({'error': 'not connected'}), 400
    board = _load_data()
    try:
        changed = gcal.pull_changes_for_user(user_id, board)
    except gcal.GcalError as exc:
        return jsonify({'error': str(exc)}), 502
    if changed:
        _save_data(board)
    return jsonify({'status': 'ok', 'changed': changed})


gcal.start_background_sync(_load_data, _save_data)


if __name__ == '__main__':
    _ensure_data_file()
    app.run(host='0.0.0.0', port=5000, debug=True)
