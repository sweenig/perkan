"""Two-way sync between PerKan cards and each user's own Google Calendar.

Every local user (see users.py) can connect their own Google account —
personal or Workspace, no code-level difference between the two, both
just do a standard OAuth2 flow. Each connection's tokens live in their
own file under data/gcal_tokens/<user_id>.json (that whole directory is
gitignored alongside kanban.json). A card only syncs once it has both a
due_date and an assigned_user_id pointing at a user who has connected
their calendar; unassigned cards never sync anywhere.

Changes made on the Google Calendar side (including brand new events)
are pulled back in by a background polling loop that walks every
connected user's calendar in turn, since a self-hosted/local instance
generally has no public HTTPS endpoint for Google's push-notification
webhooks.
"""
import os
import json
import time
import uuid
import random
import string
import logging
import threading
import datetime

import requests

logger = logging.getLogger(__name__)

DATA_DIR = os.path.join(os.path.dirname(__file__), 'data')
TOKEN_DIR = os.path.join(DATA_DIR, 'gcal_tokens')
_token_lock = threading.Lock()

AUTH_ENDPOINT = 'https://accounts.google.com/o/oauth2/v2/auth'
TOKEN_ENDPOINT = 'https://oauth2.googleapis.com/token'
USERINFO_ENDPOINT = 'https://www.googleapis.com/oauth2/v2/userinfo'
API_BASE = 'https://www.googleapis.com/calendar/v3'
# identity scopes let the same OAuth grant double as "Sign in with
# Google" (see app.py's /auth/google/sso), not just calendar access.
SCOPE = 'openid email profile https://www.googleapis.com/auth/calendar'
EXTENDED_PROP_KEY = 'perkan_card_id'
DEFAULT_EVENT_DURATION_MINUTES = 30


def _config():
    return {
        'client_id': os.environ.get('GOOGLE_CLIENT_ID', ''),
        'client_secret': os.environ.get('GOOGLE_CLIENT_SECRET', ''),
        'redirect_uri': os.environ.get('GOOGLE_REDIRECT_URI', 'http://127.0.0.1:5000/auth/google/callback'),
        # "primary" resolves to whichever account the request is authenticated
        # as, so this one setting works for every connected user's own calendar.
        'calendar_id': os.environ.get('GOOGLE_CALENDAR_ID', 'primary'),
        'poll_interval': int(os.environ.get('GCAL_SYNC_INTERVAL_SECONDS', '60')),
        'import_new_events': os.environ.get('GCAL_IMPORT_NEW_EVENTS', 'true').lower() not in ('0', 'false', 'no'),
        'default_column': os.environ.get('GCAL_DEFAULT_COLUMN', 'todo'),
        'sync_window_days': int(os.environ.get('GCAL_SYNC_WINDOW_DAYS', '90')),
        # Cards store naive "wall clock" datetimes (no offset). Google needs
        # to know which IANA zone that wall clock is in, or it assumes UTC.
        'timezone': os.environ.get('GCAL_TIMEZONE', 'UTC'),
    }


def is_configured():
    cfg = _config()
    return bool(cfg['client_id'] and cfg['client_secret'])


def _ensure_token_dir():
    if not os.path.exists(TOKEN_DIR):
        os.makedirs(TOKEN_DIR)


def _token_path(user_id):
    # user ids are uuid4 strings (see users.py); safe to use directly as a filename.
    return os.path.join(TOKEN_DIR, f'{user_id}.json')


def _load_token(user_id):
    _ensure_token_dir()
    path = _token_path(user_id)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_token(user_id, token):
    _ensure_token_dir()
    with _token_lock:
        path = _token_path(user_id)
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(token, f, indent=2)
        os.replace(tmp, path)


def connected_user_ids():
    """User ids with a live (refresh_token-holding) calendar connection."""
    _ensure_token_dir()
    result = []
    for name in os.listdir(TOKEN_DIR):
        if not name.endswith('.json'):
            continue
        user_id = name[:-len('.json')]
        if _load_token(user_id).get('refresh_token'):
            result.append(user_id)
    return result


def is_connected(user_id):
    return bool(_load_token(user_id).get('refresh_token'))


def disconnect(user_id):
    with _token_lock:
        path = _token_path(user_id)
        if os.path.exists(path):
            os.remove(path)


def status(user_id):
    token = _load_token(user_id)
    return {
        'configured': is_configured(),
        'connected': bool(token.get('refresh_token')),
        'calendar_id': _config()['calendar_id'],
        'connected_email': token.get('connected_email'),
        'last_sync': token.get('last_sync'),
        'last_sync_error': token.get('last_sync_error'),
    }


def _build_auth_url(state):
    cfg = _config()
    params = {
        'client_id': cfg['client_id'],
        'redirect_uri': cfg['redirect_uri'],
        'response_type': 'code',
        'scope': SCOPE,
        'access_type': 'offline',
        'prompt': 'consent',
        'state': state,
        'include_granted_scopes': 'true',
    }
    query = '&'.join(f'{k}={requests.utils.quote(str(v), safe="")}' for k, v in params.items())
    return f'{AUTH_ENDPOINT}?{query}'


def get_auth_url(user_id):
    """'Connect my calendar' flow for an already-logged-in user. The
    pending state is stashed in that user's own token file since we
    already know who they are."""
    state = ''.join(random.choices(string.ascii_letters + string.digits, k=24))
    token = _load_token(user_id)
    token['oauth_state'] = state
    token['oauth_state_created'] = time.time()
    _save_token(user_id, token)
    return _build_auth_url(state)


def build_sso_auth_url():
    """'Sign in with Google' flow for a not-yet-logged-in visitor —
    there's no user (and thus no token file) to stash state in yet, so
    the caller (app.py) is responsible for holding onto the returned
    state itself (in the Flask session) until the callback."""
    state = ''.join(random.choices(string.ascii_letters + string.digits, k=24))
    return _build_auth_url(state), state


def verify_state(user_id, state):
    token = _load_token(user_id)
    saved = token.get('oauth_state')
    created = token.get('oauth_state_created') or 0
    if not saved or not state or saved != state:
        return False
    return (time.time() - created) < 600  # 10 minute window


def _fetch_identity(access_token):
    """Returns {'id', 'email', 'name'} — 'id' is Google's stable
    per-account identifier (the OIDC 'sub' claim), used to match a
    Google account back to a PerKan user across sign-ins even if their
    email later changes."""
    try:
        resp = requests.get(
            USERINFO_ENDPOINT,
            headers={'Authorization': f'Bearer {access_token}'}, timeout=10)
        if resp.ok:
            data = resp.json()
            return {'id': data.get('id'), 'email': data.get('email'), 'name': data.get('name')}
    except requests.RequestException:
        pass
    return {'id': None, 'email': None, 'name': None}


def _exchange_code_raw(code):
    """POST the authorization code to Google. Returns (token_payload,
    identity) without persisting anything — callers decide where the
    resulting token belongs."""
    cfg = _config()
    resp = requests.post(TOKEN_ENDPOINT, data={
        'code': code,
        'client_id': cfg['client_id'],
        'client_secret': cfg['client_secret'],
        'redirect_uri': cfg['redirect_uri'],
        'grant_type': 'authorization_code',
    }, timeout=15)
    resp.raise_for_status()
    payload = resp.json()
    identity = _fetch_identity(payload['access_token'])
    return payload, identity


def _token_from_payload(existing_token, payload, identity):
    token = dict(existing_token)
    token.pop('oauth_state', None)
    token.pop('oauth_state_created', None)
    token['access_token'] = payload['access_token']
    token['expires_at'] = time.time() + payload.get('expires_in', 3600) - 60
    if payload.get('refresh_token'):
        token['refresh_token'] = payload['refresh_token']
    token['connected_email'] = identity.get('email')
    token.pop('sync_token', None)  # force a fresh full sync with the new grant
    return token


def exchange_code(user_id, code):
    """'Connect my calendar' flow: exchange + persist in one step for a
    known, already-logged-in user. Returns (token, identity) — the
    caller uses identity to backfill google_id/google_email onto that
    user's account so 'Sign in with Google' works for them too later."""
    payload, identity = _exchange_code_raw(code)
    token = _token_from_payload(_load_token(user_id), payload, identity)
    _save_token(user_id, token)
    return token, identity


def exchange_code_for_sso(code):
    """'Sign in with Google' flow: exchange the code but don't persist
    a token file yet, since the PerKan user isn't known until the
    caller matches or creates one by identity['id']. Call
    save_token_for_user() once that's decided."""
    return _exchange_code_raw(code)


def save_token_for_user(user_id, payload, identity):
    token = _token_from_payload(_load_token(user_id), payload, identity)
    _save_token(user_id, token)
    return token


def _refresh_access_token(user_id, token):
    cfg = _config()
    resp = requests.post(TOKEN_ENDPOINT, data={
        'refresh_token': token['refresh_token'],
        'client_id': cfg['client_id'],
        'client_secret': cfg['client_secret'],
        'grant_type': 'refresh_token',
    }, timeout=15)
    resp.raise_for_status()
    payload = resp.json()
    token['access_token'] = payload['access_token']
    token['expires_at'] = time.time() + payload.get('expires_in', 3600) - 60
    _save_token(user_id, token)
    return token


def _get_access_token(user_id):
    token = _load_token(user_id)
    if not token.get('refresh_token'):
        return None
    if token.get('access_token') and token.get('expires_at', 0) > time.time():
        return token['access_token']
    token = _refresh_access_token(user_id, token)
    return token.get('access_token')


class GcalError(Exception):
    pass


def _request(user_id, method, path, **kwargs):
    access_token = _get_access_token(user_id)
    if not access_token:
        raise GcalError('not connected')
    headers = kwargs.pop('headers', {})
    headers['Authorization'] = f'Bearer {access_token}'
    resp = requests.request(method, f'{API_BASE}{path}', headers=headers, timeout=15, **kwargs)
    return resp


# ---------------------------------------------------------------------------
# Card <-> event conversion
# ---------------------------------------------------------------------------

def _event_body_from_card(card, cfg):
    due_date = card.get('due_date')
    all_day = bool(card.get('all_day'))
    body = {
        'summary': card.get('title') or 'Untitled',
        'description': card.get('description') or '',
        'extendedProperties': {'private': {EXTENDED_PROP_KEY: card['id']}},
    }
    if due_date:
        if all_day:
            start_date = due_date[:10]
            end_date = (datetime.date.fromisoformat(start_date) + datetime.timedelta(days=1)).isoformat()
            body['start'] = {'date': start_date}
            body['end'] = {'date': end_date}
        else:
            start_dt = datetime.datetime.fromisoformat(due_date)
            end_dt = start_dt + datetime.timedelta(minutes=DEFAULT_EVENT_DURATION_MINUTES)
            # dateTime here is a naive wall-clock string; timeZone tells
            # Google how to interpret it instead of assuming UTC.
            body['start'] = {'dateTime': start_dt.isoformat(), 'timeZone': cfg['timezone']}
            body['end'] = {'dateTime': end_dt.isoformat(), 'timeZone': cfg['timezone']}
    return body


def _card_fields_from_event(event):
    start = event.get('start', {})
    if 'date' in start:
        return {'due_date': start['date'], 'all_day': True}
    if 'dateTime' in start:
        # Strip timezone offset for the simple local-time input the UI uses.
        dt = start['dateTime']
        return {'due_date': dt[:19], 'all_day': False}
    return {'due_date': None, 'all_day': False}


def upsert_event_for_card(card, user_id):
    """Create or update the event for a card on a specific user's calendar.

    Mutates the card dict in place with gcal_event_id/gcal_updated/
    gcal_user_id on success. No-ops (returns False) if that user isn't
    connected or the card has no due_date. Raises GcalError on API
    failure so callers can decide whether to surface it (a sync failure
    should never block saving the card locally).
    """
    if not is_connected(user_id) or not card.get('due_date'):
        return False
    cfg = _config()
    body = _event_body_from_card(card, cfg)
    event_id = card.get('gcal_event_id') if card.get('gcal_user_id') == user_id else None
    if event_id:
        resp = _request(user_id, 'PATCH', f'/calendars/{cfg["calendar_id"]}/events/{event_id}', json=body)
        if resp.status_code == 404:
            event_id = None  # event was deleted on the Google side; recreate it
    if not event_id:
        resp = _request(user_id, 'POST', f'/calendars/{cfg["calendar_id"]}/events', json=body)
    if not resp.ok:
        raise GcalError(f'calendar API error {resp.status_code}: {resp.text[:200]}')
    event = resp.json()
    card['gcal_event_id'] = event['id']
    card['gcal_updated'] = event.get('updated')
    card['gcal_user_id'] = user_id
    return True


def delete_event_for_card(card):
    """Delete the calendar event linked to a card, using whichever user's
    calendar it's recorded as living on (card['gcal_user_id'])."""
    event_id = card.get('gcal_event_id')
    user_id = card.get('gcal_user_id')
    if not event_id or not user_id or not is_connected(user_id):
        return False
    cfg = _config()
    resp = _request(user_id, 'DELETE', f'/calendars/{cfg["calendar_id"]}/events/{event_id}')
    if resp.status_code not in (200, 204, 404, 410, 412):
        raise GcalError(f'calendar API error {resp.status_code}: {resp.text[:200]}')
    return True


# ---------------------------------------------------------------------------
# Pull sync
# ---------------------------------------------------------------------------

def _parse_rfc3339(value):
    if not value:
        return 0.0
    try:
        return datetime.datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
    except ValueError:
        return 0.0


def pull_changes_for_user(user_id, board):
    """Apply one user's Google Calendar changes to the in-memory board.

    Returns True if the board was modified (caller persists it). Cards
    already linked to this user's calendar are matched by gcal_event_id;
    the perkan_card_id extended property is a fallback for cards that
    exist on the board but temporarily lost that link (e.g. a failed
    write). New, unlinked events become new cards assigned to this user.
    """
    if not is_connected(user_id):
        return False
    cfg = _config()
    token = _load_token(user_id)
    sync_token = token.get('sync_token')

    cards_by_event = {}
    cards_by_id = {}
    for col in board.get('columns', []):
        for card in col.get('cards', []):
            cards_by_id[card['id']] = card
            if card.get('gcal_event_id') and card.get('gcal_user_id') == user_id:
                cards_by_event[card['gcal_event_id']] = card

    return _pull_changes_impl(user_id, board, cfg, token, sync_token, cards_by_event, cards_by_id)


def _pull_changes_impl(user_id, board, cfg, token, sync_token, cards_by_event, cards_by_id):
    changed = False
    params = {'showDeleted': 'true', 'singleEvents': 'true'}
    if sync_token:
        params['syncToken'] = sync_token
    else:
        now = datetime.datetime.now(datetime.timezone.utc)
        params['timeMin'] = now.isoformat()
        params['timeMax'] = (now + datetime.timedelta(days=cfg['sync_window_days'])).isoformat()

    default_col = next((c for c in board.get('columns', []) if c['id'] == cfg['default_column']), None) \
        or (board['columns'][0] if board.get('columns') else None)

    page_token = None
    next_sync_token = sync_token
    while True:
        query = dict(params)
        if page_token:
            query['pageToken'] = page_token
        resp = _request(user_id, 'GET', f'/calendars/{cfg["calendar_id"]}/events', params=query)
        if resp.status_code == 410:
            token.pop('sync_token', None)
            _save_token(user_id, token)
            return pull_changes_for_user(user_id, board)
        if not resp.ok:
            raise GcalError(f'calendar API error {resp.status_code}: {resp.text[:200]}')
        payload = resp.json()

        for event in payload.get('items', []):
            card = cards_by_event.get(event['id'])
            if not card:
                ext_id = event.get('extendedProperties', {}).get('private', {}).get(EXTENDED_PROP_KEY)
                candidate = cards_by_id.get(ext_id)
                # Only adopt the fallback match if this card isn't already
                # linked to a *different* user's calendar event.
                if candidate and (not candidate.get('gcal_event_id') or candidate.get('gcal_user_id') == user_id):
                    card = candidate

            if event.get('status') == 'cancelled':
                if card and card.get('gcal_event_id') == event['id']:
                    card.pop('gcal_event_id', None)
                    card.pop('gcal_updated', None)
                    card.pop('gcal_user_id', None)
                    changed = True
                continue

            event_epoch = _parse_rfc3339(event.get('updated'))

            if card:
                # Last-write-wins: only apply the remote change if it's newer
                # than the local edit we haven't pushed yet.
                if event_epoch <= card.get('updated_at', 0) and card.get('gcal_updated') == event.get('updated'):
                    continue
                if event_epoch > card.get('updated_at', 0):
                    fields = _card_fields_from_event(event)
                    card['title'] = event.get('summary') or card.get('title') or 'Untitled'
                    card['description'] = event.get('description') or card.get('description') or ''
                    if fields['due_date']:
                        card['due_date'] = fields['due_date']
                        card['all_day'] = fields['all_day']
                    card['gcal_event_id'] = event['id']
                    card['gcal_updated'] = event.get('updated')
                    card['gcal_user_id'] = user_id
                    card.setdefault('assigned_user_id', user_id)
                    card['updated_at'] = event_epoch
                    changed = True
            elif cfg['import_new_events'] and default_col is not None:
                ext_id = event.get('extendedProperties', {}).get('private', {}).get(EXTENDED_PROP_KEY)
                if ext_id:
                    # Created by a PerKan instance whose card no longer exists here; skip.
                    continue
                fields = _card_fields_from_event(event)
                new_card = {
                    'id': str(uuid.uuid4()),
                    'title': event.get('summary') or 'Untitled',
                    'description': event.get('description') or '',
                    'links': [],
                    'color': '#5b2e8a',
                    'assigned_user_id': user_id,
                    'gcal_event_id': event['id'],
                    'gcal_updated': event.get('updated'),
                    'gcal_user_id': user_id,
                    'updated_at': event_epoch,
                }
                if fields['due_date']:
                    new_card['due_date'] = fields['due_date']
                    new_card['all_day'] = fields['all_day']
                existing_orders = [c['order'] for c in default_col['cards'] if isinstance(c.get('order'), (int, float))]
                new_card['order'] = float(max(existing_orders, default=0) + 1_000_000)
                default_col['cards'].append(new_card)
                cards_by_event[event['id']] = new_card
                cards_by_id[new_card['id']] = new_card
                changed = True

        next_sync_token = payload.get('nextSyncToken', next_sync_token)
        page_token = payload.get('nextPageToken')
        if not page_token:
            break

    token['sync_token'] = next_sync_token
    token['last_sync'] = time.time()
    token['last_sync_error'] = None
    _save_token(user_id, token)
    return changed


# ---------------------------------------------------------------------------
# Background polling loop
# ---------------------------------------------------------------------------

def start_background_sync(load_fn, save_fn):
    cfg = _config()
    if not is_configured():
        logger.info('Google Calendar sync not configured (GOOGLE_CLIENT_ID/SECRET unset); skipping background sync')
        return

    def _loop():
        while True:
            time.sleep(cfg['poll_interval'])
            for user_id in connected_user_ids():
                # Cheap cross-process throttle: if gunicorn is running multiple
                # workers, each starts its own copy of this loop per user. Skip
                # the tick if another worker already synced this user very
                # recently so we don't hammer the API or race on sync_token.
                token = _load_token(user_id)
                if token.get('last_sync') and (time.time() - token['last_sync']) < cfg['poll_interval'] * 0.5:
                    continue
                try:
                    board = load_fn()
                    if pull_changes_for_user(user_id, board):
                        save_fn(board)
                except GcalError as exc:
                    logger.warning('Google Calendar pull sync failed for user %s: %s', user_id, exc)
                    token = _load_token(user_id)
                    token['last_sync_error'] = str(exc)
                    _save_token(user_id, token)
                except requests.RequestException as exc:
                    logger.warning('Google Calendar pull sync network error for user %s: %s', user_id, exc)

    thread = threading.Thread(target=_loop, daemon=True, name='gcal-sync')
    thread.start()
