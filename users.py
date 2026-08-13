"""Local user accounts for PerKan.

A lightweight username/password store backed by a JSON file (same
pattern as kanban.json), used to gate the board behind a login and to
give each person their own Google Calendar connection. There is no
notion of roles/admins: any logged-in user can manage any other user,
matching the trusted-small-team spirit of the rest of the app.
"""
import os
import json
import re
import time
import uuid
import threading

from werkzeug.security import generate_password_hash, check_password_hash

DATA_DIR = os.path.join(os.path.dirname(__file__), 'data')
USERS_FILE = os.path.join(DATA_DIR, 'users.json')
# Reentrant: create/update/delete hold the lock while calling _save(),
# which also acquires it.
_lock = threading.RLock()

USERNAME_RE = re.compile(r'^[a-zA-Z0-9_.-]{2,32}$')


def _ensure_data_dir():
    if not os.path.exists(DATA_DIR):
        os.makedirs(DATA_DIR)


def _load():
    _ensure_data_dir()
    if not os.path.exists(USERS_FILE):
        return []
    try:
        with open(USERS_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _save(users):
    _ensure_data_dir()
    with _lock:
        tmp = USERS_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(users, f, indent=2)
        os.replace(tmp, USERS_FILE)


def _public(user):
    return {
        'id': user['id'],
        'username': user['username'],
        'display_name': user.get('display_name') or user['username'],
        'created_at': user.get('created_at'),
        'has_password': bool(user.get('password_hash')),
        'google_linked': bool(user.get('google_id')),
    }


class UserError(Exception):
    pass


def count_users():
    return len(_load())


def list_users():
    return [_public(u) for u in _load()]


def get_user(user_id):
    if not user_id:
        return None
    for u in _load():
        if u['id'] == user_id:
            return _public(u)
    return None


def _find_by_username(users, username):
    username = (username or '').strip().lower()
    return next((u for u in users if u['username'].lower() == username), None)


def create_user(username, password, display_name=None, google_id=None, google_email=None):
    """password may be None for a Google-only account (created via Sign
    in with Google) that has no local password to fall back on — such a
    user can only log in through Google until they set one themselves."""
    username = (username or '').strip()
    if not USERNAME_RE.match(username or ''):
        raise UserError('username must be 2-32 characters: letters, numbers, "_", "." or "-"')
    if password is not None and len(password) < 8:
        raise UserError('password must be at least 8 characters')

    with _lock:
        users = _load()
        if _find_by_username(users, username):
            raise UserError('username already taken')
        user = {
            'id': str(uuid.uuid4()),
            'username': username,
            'display_name': (display_name or '').strip() or username,
            'password_hash': generate_password_hash(password) if password else None,
            'google_id': google_id,
            'google_email': google_email,
            'created_at': time.time(),
        }
        users.append(user)
        _save(users)
    return _public(user)


def verify_login(username, password):
    users = _load()
    user = _find_by_username(users, username)
    if not user or not user.get('password_hash'):
        return None
    if not check_password_hash(user['password_hash'], password or ''):
        return None
    return _public(user)


def find_by_google_id(google_id):
    if not google_id:
        return None
    for u in _load():
        if u.get('google_id') == google_id:
            return _public(u)
    return None


def link_google_account(user_id, google_id, google_email):
    with _lock:
        users = _load()
        user = next((u for u in users if u['id'] == user_id), None)
        if not user:
            raise UserError('user not found')
        user['google_id'] = google_id
        user['google_email'] = google_email
        _save(users)
        return _public(user)


def generate_unique_username(base):
    """Turn an email local-part (or any free text) into an available
    username, appending a numeric suffix on collision."""
    cleaned = re.sub(r'[^a-zA-Z0-9_.-]', '', base or '')[:28]
    if len(cleaned) < 2:
        cleaned = (cleaned + 'user')[:28]
    with _lock:
        existing = {u['username'].lower() for u in _load()}
        candidate = cleaned
        suffix = 1
        while candidate.lower() in existing:
            suffix += 1
            candidate = f'{cleaned}{suffix}'
        return candidate


def update_user(user_id, display_name=None, password=None):
    with _lock:
        users = _load()
        user = next((u for u in users if u['id'] == user_id), None)
        if not user:
            raise UserError('user not found')
        if display_name is not None:
            display_name = display_name.strip()
            user['display_name'] = display_name or user['username']
        if password is not None:
            if len(password) < 8:
                raise UserError('password must be at least 8 characters')
            user['password_hash'] = generate_password_hash(password)
        _save(users)
        return _public(user)


def delete_user(user_id):
    with _lock:
        users = _load()
        remaining = [u for u in users if u['id'] != user_id]
        if len(remaining) == len(users):
            return False
        _save(remaining)
        return True
