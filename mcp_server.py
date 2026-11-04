"""Minimal stateless MCP server (Streamable HTTP transport) for PerKan.

Exposes the board through JSON-RPC 2.0 over POST /mcp. Every tool delegates to the
existing /api/* routes via Flask's in-process test client, so behavior and validation
are identical to the web UI. No authentication (the app has none).
"""
import json
import os
from urllib.parse import urlparse

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


_LINKS = {
    'type': 'array',
    'description': 'Links attached to the card.',
    'items': _obj({'text': _STR, 'url': _STR}, ['url']),
}


# ---------------------------------------------------------------------------
# Tool implementations. Each returns (status_code, json_payload).
# ---------------------------------------------------------------------------

def _api(method, path, body=None):
    client = current_app.test_client()
    resp = client.open(path, method=method, json=body if body is not None else None)
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


def _tool_get_board(args):
    return _api('GET', '/api/board')


def _tool_list_cards(args):
    status, board = _api('GET', '/api/board')
    if status != 200:
        return status, board
    column = args.get('column')
    project = args.get('project')
    assignee = args.get('assignee')
    query = (args.get('query') or '').lower()
    cards = []
    for col in board.get('columns', []):
        if column and col['id'] != column:
            continue
        for card in col.get('cards', []):
            if project and card.get('project') != project:
                continue
            if assignee and card.get('assignee') != assignee:
                continue
            if query and query not in (card.get('title', '') + ' ' + card.get('description', '')).lower():
                continue
            cards.append(dict(card, column=col['id']))
    return 200, {'cards': cards}


def _tool_create_card(args):
    return _api('POST', '/api/card', args)


def _tool_update_card(args):
    args = dict(args)
    card_id = args.pop('id', None)
    if not card_id:
        return 400, {'error': 'id required'}
    return _api('PUT', f'/api/card/{card_id}', args)


def _tool_delete_card(args):
    return _api('DELETE', f"/api/card/{args.get('id', '')}")


def _tool_list_columns(args):
    return _api('GET', '/api/columns')


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
    ('get_board', 'Get the entire board: all columns (with their cards) and projects.',
     _obj({}), _tool_get_board),
    ('list_cards', 'List cards, optionally filtered. Each card includes its column id.',
     _obj({'column': dict(_STR, description='Column id.'),
           'project': dict(_STR, description='Project name.'),
           'assignee': _STR,
           'query': dict(_STR, description='Case-insensitive text to find in title or description.')}),
     _tool_list_cards),
    ('create_card', 'Create a card. Setting a project creates it if missing and applies its color.',
     _obj({'title': _STR, 'description': _STR,
           'column': dict(_STR, description="Column id (default 'todo')."),
           'project': _STR, 'assignee': _STR, 'color': dict(_STR, description='Hex color; ignored if project set.'),
           'links': _LINKS}, ['title']),
     _tool_create_card),
    ('update_card', "Update a card's fields and/or move it. Set 'column' (and optional 'position') to move. "
                    "Pass an empty string for project/assignee to clear them.",
     _obj({'id': _STR, 'title': _STR, 'description': _STR, 'column': dict(_STR, description='Destination column id.'),
           'position': {'type': 'integer', 'description': 'Index within destination column (when moving).'},
           'project': _STR, 'assignee': _STR, 'color': _STR, 'links': _LINKS}, ['id']),
     _tool_update_card),
    ('delete_card', 'Delete a card by id.', _obj({'id': _STR}, ['id']), _tool_delete_card),
    ('list_columns', 'List columns (id, title, color, hidden) without cards.', _obj({}), _tool_list_columns),
    ('create_column', 'Create a column.',
     _obj({'title': _STR, 'color': _STR, 'hidden': {'type': 'boolean'},
           'position': {'type': 'integer'}}, ['title']),
     _tool_create_column),
    ('update_column', 'Rename, recolor, hide/show, or reposition a column.',
     _obj({'id': _STR, 'title': _STR, 'color': _STR, 'hidden': {'type': 'boolean'},
           'position': {'type': 'integer'}}, ['id']),
     _tool_update_column),
    ('delete_column', 'Delete a column. Optionally move its cards to another column first; otherwise they are deleted.',
     _obj({'id': _STR, 'move_to': dict(_STR, description='Column id to receive the cards.')}, ['id']),
     _tool_delete_column),
    ('list_projects', 'List projects (name, color) in order.', _obj({}), _tool_list_projects),
    ('create_project', 'Create a project. A color is generated if omitted.',
     _obj({'name': _STR, 'color': _STR, 'position': {'type': 'integer'}}, ['name']),
     _tool_create_project),
    ('update_project', 'Rename, recolor, or reposition a project (identified by its current name). '
                       'Cards in the project are updated to match.',
     _obj({'current_name': _STR, 'name': dict(_STR, description='New name.'), 'color': _STR,
           'position': {'type': 'integer'}}, ['current_name']),
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
