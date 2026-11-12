"""Minimal stateless MCP server (Streamable HTTP transport) for PerKan.

Exposes the board through JSON-RPC 2.0 over POST /mcp. Every tool delegates to the
existing /api/* routes via Flask's in-process test client, so behavior and validation
are identical to the web UI. No authentication (the app has none).
"""
import json
import os
from urllib.parse import urlencode, urlparse

from flask import Blueprint, Response, current_app, jsonify, request

mcp_bp = Blueprint('mcp', __name__)

SERVER_INFO = {'name': 'perkan', 'version': '1.0.0'}
SUPPORTED_VERSIONS = ['2025-06-18', '2025-03-26', '2024-11-05']

_STR = {'type': 'string'}


def _obj(props, required=None):
    schema = {'type': 'object', 'properties': props}
    if required:
        schema['required'] = required
    return schema


_HIDDEN = {'include_hidden': {'type': 'boolean',
                              'description': 'Also include hidden columns (and their cards). Off by default.'}}

_DONE = {'type': 'boolean',
         'description': 'True for a done column: moving a card into it (from a non-done column) sets '
                        "the card's completed_at."}

_STACK = {'type': 'boolean',
          'description': 'True to stack the column: visible columns that are stacked and next to each other share '
                         'one horizontal slot on the board, top to bottom, splitting its height equally. A stacked '
                         'column with no stacked neighbour looks like a normal one. Cards do not move.'}

_WORK = {'type': 'boolean',
         'description': 'True if time a card spends in this column counts as time spent (work) on the sprint '
                        'burn-up page, e.g. In Progress. Leave off for waiting columns.'}

_SPRINT_ARG = {'type': 'string', 'description': 'Sprint id, "active" (the active sprint) or "backlog" (no sprint).'}
_DATE = {'type': 'string', 'description': 'yyyy-mm-dd; a sprint ends at the end of its end_date.'}

_LINKS = {
    'type': 'array',
    'description': 'Links attached to the card.',
    'items': _obj({'text': _STR, 'url': _STR}, ['url']),
}

_PROJECT_FIELDS = {
    'repo_path': dict(_STR, description='Local checkout where the project\'s work lives, e.g. /home/me/perkan.'),
    'repo_url': dict(_STR, description='Remote repository URL.'),
    'notes': dict(_STR, description='Free text about the project (test and deploy hints, portal, customer).'),
    'links': dict(_LINKS, description='Links for the project, e.g. a Confluence page or portal URL.'),
}


# ---------------------------------------------------------------------------
# Tool implementations. Each returns (status_code, json_payload).
# ---------------------------------------------------------------------------

def _api(method, path, body=None):
    client = current_app.test_client()
    resp = client.open(path, method=method, json=body if body is not None else None,
                       headers={'X-Perkan-Source': 'mcp'})
    try:
        payload = resp.get_json(silent=True)
    except Exception:
        payload = None
    return resp.status_code, payload


def _project_index(name):
    status, payload = _api('GET', '/api/projects')
    for i, proj in enumerate((payload or {}).get('projects', [])):
        if proj.get('name') == name:
            return i
    return None


def _with_query(path, args, names):
    params = {k: args[k] for k in names if args.get(k) not in (None, '')}
    return path + ('?' + urlencode(params) if params else '')


def _tool_get_board(args):
    return _api('GET', _with_query('/api/board', args, ['include_hidden', 'include_closed']))


def _tool_list_cards(args):
    return _api('GET', _with_query('/api/cards', args,
                                   ['column', 'project', 'assignee', 'query', 'limit', 'offset', 'include_hidden',
                                    'sprint', 'include_closed']))


def _tool_create_card(args):
    return _api('POST', '/api/card', args)


def _tool_update_card(args):
    args = dict(args)
    card_id = args.pop('id', None)
    if not card_id:
        return 400, {'error': 'id required'}
    return _api('PUT', f'/api/card/{card_id}', args)


def _sprint_id(args):
    args = dict(args)
    sid = args.pop('id', None)
    return sid, args


def _tool_list_sprints(args):
    return _api('GET', _with_query('/api/sprints', args, ['include_closed']))


def _tool_create_sprint(args):
    return _api('POST', '/api/sprint', args)


def _tool_update_sprint(args):
    sid, body = _sprint_id(args)
    if not sid:
        return 400, {'error': 'id required'}
    return _api('PUT', f'/api/sprint/{sid}', body)


def _tool_delete_sprint(args):
    sid, _ = _sprint_id(args)
    if not sid:
        return 400, {'error': 'id required'}
    return _api('DELETE', f'/api/sprint/{sid}')


def _tool_start_sprint(args):
    sid, body = _sprint_id(args)
    if not sid:
        return 400, {'error': 'id required'}
    return _api('POST', f'/api/sprint/{sid}/start', body)


def _tool_complete_sprint(args):
    sid, body = _sprint_id(args)
    if not sid:
        return 400, {'error': 'id required'}
    return _api('POST', f'/api/sprint/{sid}/complete', body)


def _tool_get_card_sprint_history(args):
    return _api('GET', f"/api/card/{args.get('id', '')}/sprint-history")


def _tool_get_card_history(args):
    return _api('GET', f"/api/card/{args.get('id', '')}/history")


def _tool_get_card_stats(args):
    return _api('GET', _with_query('/api/stats', args, ['project', 'sprint', 'created_since', 'completed_since', 'cards']))


def _tool_delete_card(args):
    return _api('DELETE', f"/api/card/{args.get('id', '')}")


def _tool_list_columns(args):
    return _api('GET', _with_query('/api/columns', args, ['include_hidden']))


def _tool_create_column(args):
    return _api('POST', '/api/column', args)


def _tool_update_column(args):
    args = dict(args)
    col_id = args.pop('id', None)
    if not col_id:
        return 400, {'error': 'id required'}
    return _api('PUT', f'/api/column/{col_id}', args)


def _tool_delete_column(args):
    args = dict(args)
    col_id = args.pop('id', None)
    if not col_id:
        return 400, {'error': 'id required'}
    return _api('DELETE', f'/api/column/{col_id}', args)


def _tool_list_projects(args):
    return _api('GET', '/api/projects')


def _tool_create_project(args):
    return _api('POST', '/api/project', args)


def _tool_update_project(args):
    args = dict(args)
    name = args.pop('current_name', None)
    idx = _project_index(name)
    if idx is None:
        return 404, {'error': 'project not found'}
    return _api('PUT', f'/api/project/{idx}', args)


def _tool_delete_project(args):
    idx = _project_index(args.get('name'))
    if idx is None:
        return 404, {'error': 'project not found'}
    return _api('DELETE', f'/api/project/{idx}')


TOOLS = [
    ('get_board', 'Get the board: visible columns (with their cards, sorted by order) and projects. '
                  'Hidden columns are left out unless include_hidden is true. Large; prefer list_cards '
                  'with a limit when you only need a few cards.',
     _obj(dict(_HIDDEN, include_closed={'type': 'boolean', 'description': 'Also include done cards of closed '
                                                                         'sprints (hidden by default).'})),
     _tool_get_board),
    ('list_cards', 'List cards in global order (the "order" field, lowest first), optionally filtered and paged. '
                   'Each card includes its column id; "total" is the number of matches before limit/offset. '
                   'Use column + limit to get the top N cards of a column. Cards in hidden columns are skipped '
                   'unless include_hidden is true or column names that hidden column.',
     _obj({'column': dict(_STR, description='Column id.'),
           'project': dict(_STR, description='Project name.'),
           'assignee': _STR,
           'sprint': dict(_SPRINT_ARG, description='Only cards in this sprint: an id, "active" or "backlog". '
                                                    'Includes done cards of closed sprints.'),
           'include_closed': {'type': 'boolean', 'description': 'Also include done cards of closed sprints '
                                                                  '(hidden by default).'},
           'query': dict(_STR, description='Case-insensitive text to find in title or description.'),
           'limit': {'type': 'integer', 'minimum': 0, 'description': 'Return at most this many cards.'},
           'offset': {'type': 'integer', 'minimum': 0, 'description': 'Skip this many matching cards first.'},
           **_HIDDEN}),
     _tool_list_cards),
    ('create_card', 'Create a card. Setting a project creates it if missing and applies its color. '
                    'It goes after all other cards unless position is given. A card created in any column but the first joins the active sprint (as added mid-sprint) unless sprint is given.',
     _obj({'title': _STR, 'description': _STR,
           'column': dict(_STR, description="Column id (default 'todo')."),
           'position': {'type': 'integer', 'description': 'Index within the column (0 = top).'},
           'project': _STR, 'assignee': _STR, 'color': dict(_STR, description='Hex color; ignored if project set.'),
           'sprint': _SPRINT_ARG, 'links': _LINKS}, ['title']),
     _tool_create_card),
    ('update_card', "Update a card's fields, move it to another column, and/or reorder it. "
                    "Order is one global value per card. Moving with only 'column' keeps the card's order "
                    "value, so it lands where that value falls in the destination column. 'position' (0 = top) "
                    "places it at that index among the column's other cards, changing only this card's order "
                    "value; it works within the current column too. Pass an empty string for project/assignee to clear them. A card outside the active sprint cannot leave the first column (add it to the sprint first, or in the same call).",
     _obj({'id': _STR, 'title': _STR, 'description': _STR, 'column': dict(_STR, description='Destination column id, or "done" for the current done column.'),
           'position': {'type': 'integer', 'description': 'Index among the destination column\'s other cards (0 = top).'},
           'project': _STR, 'assignee': _STR, 'color': _STR, 'sprint': _SPRINT_ARG, 'links': _LINKS}, ['id']),
     _tool_update_card),
    ('get_card_history', "A card's column-change history, oldest first (from/to column ids, UTC timestamp, "
                         "source), plus created_at, started_at, completed_at and column_since. started_at is the first move to another "
                         "column; completed_at is when the card last entered a done column (absent while it "
                         "is outside every done column). Cards also carry started_at/completed_at in "
                         "list_cards and get_board.", _obj({'id': _STR}, ['id']), _tool_get_card_history),
    ('get_card_stats', 'Durations in days per card plus summaries (n, avg, median, min, max). backlog_days = '
                       'created to started; work_days = started to completed; lead_days = created to completed; '
                       'in_column_days = time in the current column for cards not in a done column '
                       '(e.g. how long cards have been in Burn-In). Also returns by_project summaries and '
                       'open_by_column. A metric is missing for a card when a timestamp it needs was never '
                       'recorded (cards from before history). Cards carry created_at, started_at, completed_at '
                       'and column_since in list_cards and get_board too.',
     _obj({'project': _STR,
           'sprint': dict(_SPRINT_ARG, description='Only cards in this sprint (id, "active" or "backlog"). '
                                                    'by_sprint always summarises sprints: committed (the set at the start), '
                                                    'added (cards added after it started), completed/carried '
                                                    'and the lead/work time summaries of completed cards.'),
           'created_since': dict(_STR, description='Only cards created on/after this date (yyyy-mm-dd).'),
           'completed_since': dict(_STR, description='Only cards completed on/after this date (yyyy-mm-dd).'),
           'cards': {'type': 'boolean', 'description': 'false returns summaries only (default true).'}}),
     _tool_get_card_stats),
    ('delete_card', 'Delete a card by id.', _obj({'id': _STR}, ['id']), _tool_delete_card),
    ('list_columns', 'List columns (id, title, color, hidden, is_done, is_current_done, stacked, counts_as_work) without cards. Hidden '
                     'columns are left out unless include_hidden is true. The result also has "current_done" '
                     '({id, title} of the current done column, hidden or not, or null).', _obj(_HIDDEN), _tool_list_columns),
    ('create_column', 'Create a column. Set is_done for a column that holds finished cards; is_current_done makes it '
                      'the one current done column (clearing the marker elsewhere). stacked puts it in a stack with the '
                      'stacked columns next to it (one shared slot on the board). counts_as_work marks a column whose '
                      'time counts as time spent on the sprint burn-up.',
     _obj({'title': _STR, 'color': _STR, 'hidden': {'type': 'boolean'}, 'is_done': _DONE, 'is_current_done': _DONE,
           'stacked': _STACK, 'counts_as_work': _WORK, 'position': {'type': 'integer'}}, ['title']),
     _tool_create_column),
    ('update_column', 'Rename, recolor, hide/show, mark as a done column, mark as the current done column '
                      '(is_current_done; only one column holds it, so use this at weekly rollover), stack it with the stacked '
                      'columns next to it (stacked), mark it as a column whose time counts as work (counts_as_work), or reposition '
                      'a column.',
     _obj({'id': _STR, 'title': _STR, 'color': _STR, 'hidden': {'type': 'boolean'}, 'is_done': _DONE, 'is_current_done': _DONE,
           'stacked': _STACK, 'counts_as_work': _WORK, 'position': {'type': 'integer'}}, ['id']),
     _tool_update_column),
    ('delete_column', 'Delete a column. Optionally move its cards to another column first; otherwise they are deleted.',
     _obj({'id': _STR, 'move_to': dict(_STR, description='Column id to receive the cards.')}, ['id']),
     _tool_delete_column),
    ('list_sprints', 'List sprints: the active one first, then planned ones by end date. Closed sprints (newest '
                     'first) only with include_closed. Each has id, name, goal, state (planned|active|closed), '
                     'start_date, end_date, card counts (cards, done), overdue_days for an active sprint past its '
                     'end, added (cards added after it started), and committed/completed/carried for closed ones. Also returns today\'s date.',
     _obj({'include_closed': {'type': 'boolean'}}), _tool_list_sprints),
    ('create_sprint', 'Create a planned sprint ending at the END of end_date. name defaults to "Sprint mm/dd". '
                      'start:true starts it right away (fails if another sprint is active). A sprint is a time box '
                      'that cards belong to (card.sprint); columns stay the workflow status. No sprint = backlog.',
     _obj({'end_date': _DATE, 'name': _STR, 'goal': _STR, 'start_date': _DATE, 'start': {'type': 'boolean'}},
          ['end_date']),
     _tool_create_sprint),
    ('update_sprint', 'Rename a planned or active sprint, set its goal, or change its dates.',
     _obj({'id': _STR, 'name': _STR, 'goal': _STR, 'start_date': _DATE, 'end_date': _DATE}, ['id']),
     _tool_update_sprint),
    ('delete_sprint', 'Delete a sprint; its cards go back to the backlog.', _obj({'id': _STR}, ['id']),
     _tool_delete_sprint),
    ('start_sprint', 'Start a planned sprint (start_date defaults to today). Only one sprint can be active.',
     _obj({'id': _STR, 'start_date': _DATE}, ['id']), _tool_start_sprint),
    ('complete_sprint', 'Close the active sprint. unfinished (cards not in a done column): carry_over (default) '
                        'moves them to the next sprint keeping their column; backlog takes them out of any sprint; '
                        'leave keeps them in the closed sprint. For carry_over the next sprint is next_sprint (a '
                        'planned sprint id) or a new one made from next_end_date (default: this sprint\'s end + 7 '
                        'days) and next_name; start_next starts it. Done cards stay in the closed sprint and '
                        'disappear from the board. Returns committed (the set at the start) / added (cards added mid-sprint) / completed / unfinished counts.',
     _obj({'id': _STR, 'unfinished': {'type': 'string', 'enum': ['carry_over', 'backlog', 'leave']},
           'next_sprint': dict(_STR, description='Id of a planned sprint to receive the unfinished cards.'),
           'next_end_date': _DATE, 'next_name': _STR, 'start_next': {'type': 'boolean'}}, ['id']),
     _tool_complete_sprint),
    ('get_card_sprint_history', "A card's sprint changes, oldest first (from/to sprint ids, UTC time, source), "
                                "plus its current sprint.", _obj({'id': _STR}, ['id']),
     _tool_get_card_sprint_history),
    ('list_projects', 'List projects alphabetically: name, color and, when set, repo_path, repo_url, notes and links.',
     _obj({}), _tool_list_projects),
    ('create_project', 'Create a project. A color is generated if omitted. repo_path, repo_url, notes and links '
                       'say where the project\'s work lives.',
     _obj(dict({'name': _STR, 'color': _STR}, **_PROJECT_FIELDS), ['name']),
     _tool_create_project),
    ('update_project', 'Rename, recolor or describe a project (identified by its current name). '
                       'Cards in the project are updated to match. Pass an empty string (or an empty links list) '
                       'to clear repo_path, repo_url, notes or links; fields you leave out are unchanged. '
                       'links replaces the whole list.',
     _obj(dict({'current_name': _STR, 'name': dict(_STR, description='New name.'), 'color': _STR},
               **_PROJECT_FIELDS), ['current_name']),
     _tool_update_project),
    ('delete_project', 'Delete a project by name. Its cards are kept but un-assigned from the project.',
     _obj({'name': _STR}, ['name']), _tool_delete_project),
]

_TOOL_MAP = {name: fn for name, _d, _s, fn in TOOLS}


def _tool_list():
    return [{'name': n, 'description': d, 'inputSchema': s} for n, d, s, _f in TOOLS]


# ---------------------------------------------------------------------------
# JSON-RPC handling
# ---------------------------------------------------------------------------

def _rpc_error(req_id, code, message):
    return {'jsonrpc': '2.0', 'id': req_id, 'error': {'code': code, 'message': message}}


def _handle(msg):
    """Handle one JSON-RPC message. Returns a response dict, or None for notifications."""
    if not isinstance(msg, dict) or msg.get('jsonrpc') != '2.0':
        return _rpc_error(None, -32600, 'Invalid Request')
    method = msg.get('method')
    req_id = msg.get('id')
    params = msg.get('params') or {}
    is_notification = 'id' not in msg

    if method is None:
        return None  # a client response; nothing to do
    if is_notification:
        return None  # e.g. notifications/initialized, notifications/cancelled

    if method == 'initialize':
        requested = params.get('protocolVersion')
        version = requested if requested in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[0]
        return {'jsonrpc': '2.0', 'id': req_id, 'result': {
            'protocolVersion': version,
            'capabilities': {'tools': {'listChanged': False}},
            'serverInfo': SERVER_INFO,
        }}
    if method == 'ping':
        return {'jsonrpc': '2.0', 'id': req_id, 'result': {}}
    if method == 'tools/list':
        return {'jsonrpc': '2.0', 'id': req_id, 'result': {'tools': _tool_list()}}
    if method == 'tools/call':
        fn = _TOOL_MAP.get(params.get('name'))
        if fn is None:
            return _rpc_error(req_id, -32602, f"Unknown tool: {params.get('name')}")
        args = params.get('arguments') or {}
        if not isinstance(args, dict):
            return _rpc_error(req_id, -32602, 'arguments must be an object')
        try:
            status, payload = fn(args)
        except Exception as exc:  # surface as a tool error, not a protocol error
            current_app.logger.exception('MCP tool %s failed', params.get('name'))
            return {'jsonrpc': '2.0', 'id': req_id, 'result': {
                'content': [{'type': 'text', 'text': f'Error: {exc}'}], 'isError': True}}
        is_error = status >= 400
        text = json.dumps(payload, indent=2) if payload is not None else ''
        if is_error:
            text = (payload or {}).get('error', f'HTTP {status}') if isinstance(payload, dict) else f'HTTP {status}'
        result = {'content': [{'type': 'text', 'text': text}], 'isError': is_error}
        if not is_error and isinstance(payload, dict):
            result['structuredContent'] = payload
        return {'jsonrpc': '2.0', 'id': req_id, 'result': result}
    return _rpc_error(req_id, -32601, f'Method not found: {method}')


def _origin_ok():
    """Reject browser requests from foreign origins (DNS-rebinding protection)."""
    origin = request.headers.get('Origin')
    if not origin:
        return True  # non-browser clients don't send Origin
    allowed = [o.strip() for o in os.environ.get('MCP_ALLOWED_ORIGINS', '').split(',') if o.strip()]
    if origin in allowed or '*' in allowed:
        return True
    return urlparse(origin).netloc == request.host


@mcp_bp.route('/mcp', methods=['POST'])
def mcp_post():
    if not _origin_ok():
        return jsonify(_rpc_error(None, -32600, 'Origin not allowed')), 403
    payload = request.get_json(silent=True)
    if payload is None:
        return jsonify(_rpc_error(None, -32700, 'Parse error')), 400

    if isinstance(payload, list):
        if not payload:
            return jsonify(_rpc_error(None, -32600, 'Invalid Request')), 400
        responses = [r for r in (_handle(m) for m in payload) if r is not None]
        if not responses:
            return Response(status=202)
        return jsonify(responses)

    response = _handle(payload)
    if response is None:
        return Response(status=202)
    return jsonify(response)


@mcp_bp.route('/mcp', methods=['GET', 'DELETE'])
def mcp_unsupported():
    # Stateless server: no SSE stream and no sessions to terminate.
    return Response(status=405, headers={'Allow': 'POST'})
