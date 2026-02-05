from flask import Flask, jsonify, request, render_template, abort
import json
import os
import threading
import uuid
import re
import time
import logging
import errno
import shutil
import random

app = Flask(__name__, static_folder='static', template_folder='templates')
# Basic logging for debugging slow I/O
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DATA_DIR = os.path.join(os.path.dirname(__file__), 'data')
DATA_FILE = os.path.join(DATA_DIR, 'kanban.json')
_lock = threading.Lock()

DEFAULT_CARD_COLOR = '#5b2e8a'

DEFAULT_BOARD = {
    "columns": [
        {"id": "todo", "title": "To Do", "cards": [], "color": "#1f77b4"},
        {"id": "inprogress", "title": "In Progress", "cards": [], "color": "#ff8c00"},
        {"id": "blocked", "title": "Blocked", "cards": [], "color": "#d62728"},
        {"id": "done", "title": "Done", "cards": [], "color": "#2ca02c"}
    ],
    "projects": []
}


def _normalize_board(data):
        if 'columns' not in data or not isinstance(data['columns'], list):
                data['columns'] = []
        if 'projects' not in data or not isinstance(data['projects'], list):
                data['projects'] = []
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



@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/board', methods=['GET'])
def get_board():
    return jsonify(_load_data())


@app.route('/api/card', methods=['POST'])
def create_card():
    data = request.get_json() or {}
    title = data.get('title')
    description = data.get('description', '')
    column_id = data.get('column', 'todo')
    color = data.get('color')
    project_name = (data.get('project') or '').strip()
    links = _clean_links(data.get('links'))
    if not title:
        return jsonify({'error': 'title required'}), 400

    board = _load_data()
    card = {
        'id': str(uuid.uuid4()),
        'title': title,
        'description': description,
        'links': links
    }
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
            col['cards'].append(card)
            _save_data(board)
            return jsonify(card), 201
    return jsonify({'error': 'column not found'}), 404


@app.route('/api/card/<card_id>', methods=['PUT'])
def update_card(card_id):
    data = request.get_json() or {}
    target_col = data.get('column')
    position = data.get('position')  # optional integer
    title = data.get('title')
    description = data.get('description')
    color = data.get('color')
    links = data.get('links')
    project_payload = data.get('project') if 'project' in data else None

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
    if links is not None:
        card_obj['links'] = _clean_links(links)

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
    return jsonify(card_obj)


@app.route('/api/card/<card_id>', methods=['DELETE'])
def delete_card(card_id):
    board = _load_data()
    for col in board['columns']:
        for i, c in enumerate(col['cards']):
            if c['id'] == card_id:
                del col['cards'][i]
                _save_data(board)
                return jsonify({'deleted': True})
    return jsonify({'error': 'card not found'}), 404


@app.route('/api/columns', methods=['GET'])
def get_columns():
    board = _load_data()
    cols = [{'id': c['id'], 'title': c['title'], 'color': c.get('color')} for c in board['columns']]
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

    col = {'id': col_id, 'title': title, 'cards': [], 'color': color}
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
    board = _load_data()
    idx = next((i for i, c in enumerate(board['columns']) if c['id'] == col_id), None)
    if idx is None:
        return jsonify({'error': 'column not found'}), 404
    col = board['columns'][idx]
    if title is not None:
        col['title'] = title
    if color is not None:
        col['color'] = color
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


if __name__ == '__main__':
    _ensure_data_file()
    app.run(host='0.0.0.0', port=5000, debug=True)
