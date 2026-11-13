"""API tests: global card order, hidden columns, paging, MCP tools.

Run from the perkan directory:  python -m unittest discover -s tests -v
Uses a throwaway data directory; never touches ./data.
"""
import datetime
import io
import json
import os
import sys
import tempfile
import unittest

_TMP = tempfile.mkdtemp(prefix='perkan-test-')
os.environ['PERKAN_DATA_DIR'] = _TMP
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as perkan   # noqa: E402
import db              # noqa: E402


def board_fixture():
    """todo: t1 t2 t3 | doing: d1 d2 | done: (empty) | archive (hidden): h1 h2.
    Orders are one global sequence, deliberately interleaved across columns."""
    def card(cid, order):
        return {'id': cid, 'title': cid.upper(), 'description': f'about {cid}', 'order': order}
    return {
        'columns': [
            {'id': 'todo', 'title': 'To Do', 'cards': [card('t1', 1e6), card('t2', 3e6), card('t3', 5e6)]},
            {'id': 'doing', 'title': 'Doing', 'cards': [card('d1', 2e6), card('d2', 4e6)]},
            {'id': 'done', 'title': 'Done', 'is_current_done': True, 'cards': []},
            {'id': 'archive', 'title': 'Archive', 'hidden': True, 'cards': [card('h1', 6e6), card('h2', 7e6)]},
        ],
        'projects': [],
    }


class Base(unittest.TestCase):
    def setUp(self):
        with db.write() as conn:
            conn.execute('DELETE FROM card_history')   # replace_board keeps history of surviving card ids
            conn.execute('DELETE FROM card_sprint_history')
            conn.execute('DELETE FROM sprints')
            db.replace_board(conn, perkan._normalize_board(board_fixture()))
        self.c = perkan.app.test_client()

    # helpers
    def get(self, url):
        r = self.c.get(url)
        self.assertEqual(r.status_code, 200, r.data)
        return r.get_json()

    def put(self, card_id, **body):
        r = self.c.put(f'/api/card/{card_id}', json=body)
        self.assertEqual(r.status_code, 200, r.data)
        return r.get_json()

    def post(self, **body):
        r = self.c.post('/api/card', json=body)
        self.assertEqual(r.status_code, 201, r.data)
        return r.get_json()

    def ids(self, col):
        board = self.get('/api/board?include_hidden=true')
        return [c['id'] for c in next(x for x in board['columns'] if x['id'] == col)['cards']]

    def order(self, card_id):
        board = self.get('/api/board?include_hidden=true')
        return next(c['order'] for col in board['columns'] for c in col['cards'] if c['id'] == card_id)

    def global_ids(self):
        return [c['id'] for c in self.get('/api/cards?include_hidden=true')['cards']]

    def assert_invariants(self):
        board = self.get('/api/board?include_hidden=true')
        orders = [c['order'] for col in board['columns'] for c in col['cards']]
        self.assertEqual(len(orders), len(set(orders)), 'orders must be globally unique')
        for col in board['columns']:
            col_orders = [c['order'] for c in col['cards']]
            self.assertEqual(col_orders, sorted(col_orders), f"{col['id']} must be sorted by order")
        flat = self.get('/api/cards?include_hidden=true')['cards']
        self.assertEqual([c['order'] for c in flat], sorted(c['order'] for c in flat))


class InActiveSprint:
    """Mixin: every fixture card, and every card the post helper creates, is in a started sprint, so tests
    that move cards out of the first column are not stopped by the active-sprint rule."""
    def setUp(self):
        super().setUp()
        with db.write() as conn:
            sid = db.create_sprint(conn, '2999-01-01')
            db.start_sprint(conn, sid)
            for r in conn.execute('SELECT id FROM cards').fetchall():
                db.set_card_sprint(conn, r['id'], sid)

    def post(self, **body):
        body.setdefault('sprint', 'active')
        return super().post(**body)


class OrderingTests(InActiveSprint, Base):
    def test_create_at_position_zero_gets_lowest_order_in_column(self):
        card = self.post(title='new top', column='todo', position=0)
        self.assertEqual(self.ids('todo')[0], card['id'])
        self.assertLess(card['order'], self.order('t1'))
        self.assert_invariants()

    def test_create_default_goes_after_everything(self):
        card = self.post(title='last', column='doing')
        self.assertEqual(self.ids('doing')[-1], card['id'])
        self.assertGreater(card['order'], self.order('h2'))
        self.assert_invariants()

    def test_create_in_the_middle(self):
        card = self.post(title='mid', column='todo', position=1)
        self.assertEqual(self.ids('todo'), ['t1', card['id'], 't2', 't3'])
        self.assert_invariants()

    def test_update_position_zero_moves_card_to_top_of_its_column(self):
        self.put('t3', position=0)
        self.assertEqual(self.ids('todo'), ['t3', 't1', 't2'])
        self.assertEqual(self.get('/api/cards?column=todo&limit=1')['cards'][0]['id'], 't3')
        self.assert_invariants()

    def test_position_past_the_end_means_bottom(self):
        self.put('t1', position=99)
        self.assertEqual(self.ids('todo'), ['t2', 't3', 't1'])
        self.assert_invariants()

    def test_reorder_changes_only_the_moved_card(self):
        before = {c: self.order(c) for c in ['t1', 't2', 'd1', 'd2', 'h1', 'h2']}
        self.put('t3', position=0)
        for cid, value in before.items():
            self.assertEqual(self.order(cid), value, f'{cid} must not change')

    def test_card_already_in_its_slot_keeps_its_order(self):
        before = self.order('t2')
        self.put('t2', position=1)
        self.assertEqual(self.order('t2'), before)

    def test_plain_column_move_keeps_order_value(self):
        before = {c: self.order(c) for c in ['t1', 't2', 't3']}
        for cid, col in [('t1', 'doing'), ('t2', 'done'), ('t3', 'doing')]:
            self.assertEqual(self.put(cid, column=col)['order'], before[cid])
        self.assertEqual(self.ids('doing'), ['t1', 'd1', 'd2', 't3'])   # slotted in by global order (t3=5e6 > d2=4e6)
        self.assertEqual(self.ids('done'), ['t2'])
        self.assert_invariants()

    def test_moving_cards_away_and_back_keeps_their_relative_order(self):
        """The scenario from the design: three cards leave a column for different columns,
        get reordered there, then return -- their relative order must not change."""
        original = [c for c in self.global_ids() if c in ('t1', 't2', 't3')]
        self.put('t1', column='doing', position=0)       # top of Doing: already above d1, so unchanged
        self.put('t2', column='done')
        self.put('t3', column='archive', position=0)     # top of the hidden column
        self.put('t3', column='todo')
        self.put('t2', column='todo')
        self.put('t1', column='todo')
        self.assertEqual([c for c in self.global_ids() if c in ('t1', 't2', 't3')], original)
        self.assertEqual(self.ids('todo'), ['t1', 't2', 't3'])
        self.assert_invariants()

    def test_explicit_reorder_may_change_relative_order(self):
        self.put('t1', position=99)   # explicit reorder: t1 now after t3
        self.assertEqual(self.ids('todo'), ['t2', 't3', 't1'])

    def test_move_to_column_with_position_lands_at_that_index(self):
        self.put('t1', column='doing', position=1)
        self.assertEqual(self.ids('doing'), ['d1', 't1', 'd2'])
        self.assert_invariants()

    def test_move_into_empty_column_keeps_order(self):
        before = self.order('d2')
        self.put('d2', column='done', position=0)
        self.assertEqual(self.order('d2'), before)

    def test_explicit_order_is_honoured_and_kept_unique(self):
        self.put('t1', order=4.5e6)
        self.assertEqual(self.order('t1'), 4.5e6)
        self.put('t2', order=4.5e6)      # collides with t1
        self.assertNotEqual(self.order('t2'), self.order('t1'))
        self.assertLess(self.order('t2') - 4.5e6, 1e6)
        self.assert_invariants()

    def test_non_finite_order_is_ignored(self):
        before = self.order('t1')
        r = self.c.put('/api/card/t1', data='{"order": NaN, "title": "x"}', content_type='application/json')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.order('t1'), before)

    def test_repeated_inserts_at_one_spot_never_collide(self):
        """Halving the gap 100 times exhausts float precision; the board must respace itself."""
        created = [self.post(title=f'n{i}', column='todo', position=1)['id'] for i in range(100)]
        self.assertEqual(self.ids('todo')[0], 't1')
        self.assertEqual(self.ids('todo')[1:101], created[::-1])      # newest first at index 1
        self.assertEqual(self.ids('todo')[-2:], ['t2', 't3'])
        self.assert_invariants()

    def test_deleting_a_column_with_move_to_keeps_order_values(self):
        before = self.order('d2')
        r = self.c.delete('/api/column/doing', json={'move_to': 'todo'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.order('d2'), before)
        self.assertEqual(self.ids('todo'), ['t1', 'd1', 't2', 'd2', 't3'])
        self.assert_invariants()


class HiddenColumnTests(Base):
    def test_board_excludes_hidden_columns_by_default(self):
        board = self.get('/api/board')
        self.assertEqual([c['id'] for c in board['columns']], ['todo', 'doing', 'done'])
        full = self.get('/api/board?include_hidden=true')
        self.assertEqual([c['id'] for c in full['columns']], ['todo', 'doing', 'done', 'archive'])
        self.assertEqual(self.get('/api/board?include_hidden=1')['columns'][-1]['id'], 'archive')

    def test_board_reports_the_active_sprint(self):
        self.assertIsNone(self.get('/api/board')['active_sprint'])
        r = self.c.post('/api/sprint', json={'end_date': '2999-01-01', 'start': True})
        self.assertEqual(r.status_code, 201, r.data)
        sid = r.get_json()['id']
        self.assertEqual(self.get('/api/board')['active_sprint'], sid)

    def test_columns_list_excludes_hidden_by_default(self):
        self.assertEqual([c['id'] for c in self.get('/api/columns')['columns']], ['todo', 'doing', 'done'])
        self.assertEqual(len(self.get('/api/columns?include_hidden=true')['columns']), 4)

    def test_cards_exclude_hidden_unless_asked(self):
        self.assertEqual([c['id'] for c in self.get('/api/cards')['cards']], ['t1', 'd1', 't2', 'd2', 't3'])
        self.assertEqual(self.get('/api/cards')['total'], 5)
        self.assertEqual(len(self.get('/api/cards?include_hidden=true')['cards']), 7)

    def test_naming_a_hidden_column_is_an_explicit_request(self):
        self.assertEqual([c['id'] for c in self.get('/api/cards?column=archive')['cards']], ['h1', 'h2'])

    def test_export_and_import_keep_hidden_columns(self):
        exported = json.loads(self.c.get('/api/board/export').data)
        self.assertIn('archive', [c['id'] for c in exported['columns']])
        extra = {'columns': [{'id': 'todo', 'title': 'To Do', 'cards': [{'id': 'x', 'title': 'X'}]}], 'projects': []}
        r = self.c.post('/api/board/import', data={'file': (io.BytesIO(json.dumps(extra).encode()), 'f.json'),
                                                   'mode': 'merge'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.ids('archive'), ['h1', 'h2'])            # merge must not drop hidden columns
        self.assertEqual(len(self.get('/api/cards?include_hidden=true')['cards']), 8)
        self.assert_invariants()

    def test_hiding_a_column_via_update_still_returns_it(self):
        r = self.c.put('/api/column/doing', json={'hidden': True})
        self.assertEqual(r.status_code, 200)
        self.assertEqual([c['id'] for c in r.get_json()['cards']], ['d1', 'd2'])
        self.assertNotIn('doing', [c['id'] for c in self.get('/api/board')['columns']])


class ListCardsTests(Base):
    def test_limit_returns_the_first_n_in_global_order(self):
        r = self.get('/api/cards?limit=2')
        self.assertEqual([c['id'] for c in r['cards']], ['t1', 'd1'])
        self.assertEqual(r['total'], 5)

    def test_top_of_a_column(self):
        self.assertEqual([c['id'] for c in self.get('/api/cards?column=todo&limit=1')['cards']], ['t1'])
        self.assertEqual(self.get('/api/cards?column=todo&limit=1')['cards'][0]['column'], 'todo')

    def test_offset_pages(self):
        self.assertEqual([c['id'] for c in self.get('/api/cards?limit=2&offset=2')['cards']], ['t2', 'd2'])
        self.assertEqual(self.get('/api/cards?offset=99')['cards'], [])

    def test_filters(self):
        self.post(title='tagged', column='todo', project='Proj', assignee='sam')
        self.assertEqual(len(self.get('/api/cards?project=Proj')['cards']), 1)
        self.assertEqual(len(self.get('/api/cards?assignee=sam')['cards']), 1)
        self.assertEqual([c['id'] for c in self.get('/api/cards?query=ABOUT%20D1')['cards']], ['d1'])

    def test_bad_paging_is_rejected(self):
        for q in ('limit=-1', 'limit=abc', 'offset=-3'):
            self.assertEqual(self.c.get('/api/cards?' + q).status_code, 400, q)


class McpTests(Base):
    def call(self, name, **arguments):
        r = self.c.post('/mcp', json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                                       'params': {'name': name, 'arguments': arguments}})
        result = r.get_json()['result']
        self.assertFalse(result['isError'], result)
        return result.get('structuredContent')

    def test_list_cards_limit_and_string_arguments(self):
        out = self.call('list_cards', column='todo', limit='1')
        self.assertEqual([c['id'] for c in out['cards']], ['t1'])
        self.assertEqual(out['total'], 3)

    def test_get_board_hidden_flag_accepts_strings(self):
        self.assertEqual(len(self.call('get_board')['columns']), 3)
        self.assertEqual(len(self.call('get_board', include_hidden='true')['columns']), 4)
        self.assertEqual(len(self.call('list_columns', include_hidden=True)['columns']), 4)

    def test_create_and_update_card_position(self):
        top = self.call('create_card', title='top', column='todo', position=0)
        self.assertEqual(self.ids('todo')[0], top['id'])
        self.call('update_card', id='t3', position=0)
        self.assertEqual(self.ids('todo')[0], 't3')
        self.assert_invariants()

    def test_sprint_tools(self):
        sp = self.call('create_sprint', end_date='2026-12-01', start='true')
        self.assertEqual(sp['state'], 'active')
        card = self.call('create_card', title='s', sprint='active')
        self.assertEqual(card['sprint'], sp['id'])
        self.assertEqual([c['id'] for c in self.call('list_cards', sprint='active')['cards']], [card['id']])
        self.call('update_card', id=card['id'], column='done')
        out = self.call('complete_sprint', id=sp['id'], start_next='true')
        self.assertEqual((out['committed'], out['completed'], out['unfinished']), (1, 1, 0))
        self.assertEqual(out['next_sprint']['state'], 'active')
        self.assertEqual(len(self.call('list_sprints')['sprints']), 1)
        self.assertEqual(self.call('get_card_sprint_history', id=card['id'])['sprint'], sp['id'])
        self.call('update_sprint', id=out['next_sprint']['id'], goal='Ship it')
        self.call('delete_sprint', id=out['next_sprint']['id'])


class HistoryTests(InActiveSprint, Base):
    def test_create_records_history_but_no_dates(self):
        card = self.post(title='x', column='todo')
        self.assertNotIn('started_at', card)
        self.assertNotIn('completed_at', card)
        h = self.get(f"/api/card/{card['id']}/history")
        self.assertEqual([(e['from'], e['to']) for e in h['history']], [(None, 'todo')])

    def test_first_move_starts_and_move_into_done_completes(self):
        self.put('t1', column='doing')
        c = self.get('/api/cards?query=t1')['cards'][0]
        self.assertIn('started_at', c)
        self.assertNotIn('completed_at', c)
        moved = self.put('t1', column='done')
        self.assertIn('completed_at', moved)
        h = self.get('/api/card/t1/history')
        self.assertEqual([(e['from'], e['to']) for e in h['history']], [('todo', 'doing'), ('doing', 'done')])

    def test_move_between_done_columns_keeps_completion(self):
        with db.write() as conn:
            conn.execute("INSERT INTO columns (id,title,color,hidden,position,is_done) VALUES ('done2','Done 1','#1',1,9,1)")
        done = self.put('t1', column='done')['completed_at']
        db_rows = self.get('/api/card/t1/history')['history']
        again = self.put('t1', column='done2')
        self.assertEqual(again['completed_at'], done)
        self.assertEqual(len(self.get('/api/card/t1/history')['history']), len(db_rows) + 1)

    def test_moving_out_of_done_clears_completion_and_reentry_resets_it(self):
        self.put('t1', column='done')
        out = self.put('t1', column='doing')
        self.assertNotIn('completed_at', out)
        with db.write() as conn:   # make the re-entry distinguishable from the first one
            conn.execute("UPDATE card_history SET at = '2020-01-01T00:00:00Z'")
        again = self.put('t1', column='done')
        self.assertGreater(again['completed_at'], '2020-01-01T00:00:00Z')

    def test_plain_edit_records_nothing(self):
        self.put('t1', title='renamed')
        self.assertEqual(self.get('/api/card/t1/history')['history'], [])

    def test_source_header(self):
        r = self.c.put('/api/card/t1', json={'column': 'doing'}, headers={'X-Perkan-Source': 'ui'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.get('/api/card/t1/history')['history'][0]['source'], 'ui')

    def test_delete_column_move_to_records_moves(self):
        r = self.c.delete('/api/column/doing', json={'move_to': 'done'})
        self.assertEqual(r.status_code, 200)
        c = self.get('/api/cards?query=d1')['cards'][0]
        self.assertIn('completed_at', c)

    def test_history_of_unknown_card_is_404(self):
        self.assertEqual(self.c.get('/api/card/nope/history').status_code, 404)

    def test_column_is_done_flag(self):
        cols = {c['id']: c for c in self.get('/api/columns?include_hidden=true')['columns']}
        self.assertTrue(cols['done']['is_done'])          # title starts with "Done"
        self.assertFalse(cols['todo']['is_done'])
        r = self.c.put('/api/column/doing', json={'is_done': True})
        self.assertTrue(r.get_json()['is_done'])
        r = self.c.post('/api/column', json={'title': 'Shipped', 'is_done': True})
        self.assertTrue(r.get_json()['is_done'])

    def test_current_done_column(self):
        j = self.get('/api/columns?include_hidden=true')
        self.assertEqual(j['current_done']['id'], 'done')
        # marking another column moves the marker and implies is_done
        r = self.c.put('/api/column/doing', json={'is_current_done': True})
        self.assertTrue(r.get_json()['is_current_done'])
        self.assertTrue(r.get_json()['is_done'])
        cols = {c['id']: c for c in self.get('/api/columns?include_hidden=true')['columns']}
        self.assertFalse(cols['done']['is_current_done'])
        self.assertEqual(sum(c['is_current_done'] for c in cols.values()), 1)
        # "done" / "current_done" are aliases for it
        cid = self.c.post('/api/card', json={'title': 'A', 'column': 'current_done'}).get_json()['id']
        self.assertEqual(self.get('/api/card/' + cid + '/history')['history'][0]['to'], 'doing')
        # the alias wins even over a column whose id is literally "done" (here: no longer the current one)
        cid2 = self.c.post('/api/card', json={'title': 'B', 'column': 'done'}).get_json()['id']
        self.assertEqual(self.get('/api/card/' + cid2 + '/history')['history'][0]['to'], 'doing')
        # un-marking a done column clears the marker; alias then fails
        self.c.put('/api/column/doing', json={'is_done': False})
        self.assertIsNone(self.get('/api/columns')['current_done'])
        self.assertEqual(self.c.put('/api/card/' + cid, json={'column': 'current_done'}).status_code, 404)

    def test_backfill_from_description(self):
        b = board_fixture()
        b['columns'][2]['cards'] = [
            {'id': 'x1', 'title': 'X', 'description': 'work\nDONE 2026-11-04', 'order': 9e6},
            {'id': 'x2', 'title': 'Y', 'description': 'no date', 'order': 9.5e6}]
        with db.write() as conn:
            db.replace_board(conn, perkan._normalize_board(b))
        cards = {c['id']: c for c in self.get('/api/cards')['cards']}
        self.assertEqual(cards['x1']['completed_at'], '2026-11-04T00:00:00Z')
        self.assertNotIn('started_at', cards['x1'])
        self.assertNotIn('completed_at', cards['x2'])

    def test_merge_import_keeps_history(self):
        self.put('t1', column='done')
        r = self.c.post('/api/board/import', data={'mode': 'merge', 'file': (io.BytesIO(b'{"columns": []}'), 'b.json')},
                        content_type='multipart/form-data')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertIn('completed_at', self.get('/api/cards?query=t1')['cards'][0])

    def test_mcp_tool_lists_history(self):
        self.put('t1', column='doing')
        r = self.c.post('/mcp', json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                                      'params': {'name': 'get_card_history', 'arguments': {'id': 't1'}}})
        hist = r.get_json()['result']['structuredContent']['history']
        self.assertEqual([(e['to'], e['source']) for e in hist], [('doing', 'api')])


    def test_created_at_and_column_since(self):
        card = self.post(title='n', column='todo')
        self.assertEqual(card['created_at'], card['column_since'])
        h = self.get(f"/api/card/{card['id']}/history")
        self.assertEqual(h['history'][0]['at'], card['created_at'])
        with db.write() as conn:
            conn.execute("UPDATE card_history SET at='2026-01-01T00:00:00Z' WHERE card_id=?", (card['id'],))
        self.put(card['id'], title='renamed')                    # a plain edit does not reset anything
        c = self.get(f"/api/cards?query=renamed")['cards'][0]
        self.assertEqual(c['column_since'], '2026-01-01T00:00:00Z')
        moved = self.put(card['id'], column='doing')
        self.assertGreater(moved['column_since'], '2026-01-01T00:00:00Z')
        self.assertEqual(moved['created_at'], card['created_at'])  # moves never touch created_at

    def test_cards_from_before_history_have_no_dates(self):
        c = self.get('/api/cards?query=t2')['cards'][0]
        for key in ('created_at', 'started_at', 'completed_at', 'column_since'):
            self.assertNotIn(key, c)

    def test_stats_durations(self):
        a = self.post(title='A', column='todo', project='P')['id']
        b = self.post(title='B', column='todo')['id']
        self.put(a, column='doing')
        self.put(a, column='done')
        with db.write() as conn:
            conn.execute("UPDATE cards SET created_at='2026-01-01T00:00:00Z' WHERE id=?", (a,))
            ids = [r['id'] for r in conn.execute('SELECT id FROM card_history WHERE card_id=? ORDER BY id', (a,))]
            for hid, at in zip(ids, ['2026-01-01T00:00:00Z', '2026-01-04T00:00:00Z', '2026-01-06T12:00:00Z']):
                conn.execute('UPDATE card_history SET at=? WHERE id=?', (at, hid))
            conn.execute("UPDATE cards SET created_at='2026-01-10T00:00:00Z' WHERE id=?", (b,))
            conn.execute("UPDATE card_history SET at='2026-01-10T00:00:00Z' WHERE card_id=?", (b,))
            st = db.card_stats(conn, now='2026-01-12T12:00:00Z')
        rows = {r['id']: r for r in st['cards']}
        self.assertEqual((rows[a]['backlog_days'], rows[a]['work_days'], rows[a]['lead_days']), (3.0, 2.5, 5.5))
        self.assertNotIn('in_column_days', rows[a])                 # done cards are not "waiting"
        self.assertEqual(rows[b]['in_column_days'], 2.5)
        self.assertNotIn('backlog_days', rows[b])                   # not started yet
        self.assertEqual(st['summary']['lead_days']['n'], 1)
        self.assertEqual(st['summary']['completed'], 1)
        self.assertEqual(st['by_project']['P']['work_days']['avg'], 2.5)
        self.assertEqual(st['open_by_column']['todo']['max'], 2.5)

    def test_stats_endpoint_filters(self):
        a = self.post(title='A', column='todo', project='P')['id']
        self.post(title='B', column='todo')
        self.put(a, column='done')
        everything = self.get('/api/stats')
        self.assertEqual(len(everything['cards']), 2)             # fixture cards predate history, so they are left out
        only = self.get('/api/stats?project=P&cards=false')
        self.assertNotIn('cards', only)
        self.assertEqual(only['summary']['cards'], 1)
        self.assertEqual(self.get('/api/stats?completed_since=2999-01-01')['summary']['cards'], 0)
        self.assertEqual(self.c.get('/api/stats?created_since=yesterday').status_code, 400)

    def test_merge_import_keeps_created_at(self):
        card = self.post(title='keep', column='todo')
        r = self.c.post('/api/board/import', data={'mode': 'merge', 'file': (io.BytesIO(b'{"columns": []}'), 'b.json')},
                        content_type='multipart/form-data')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(self.get('/api/cards?query=keep')['cards'][0]['created_at'], card['created_at'])

    def test_mcp_stats_tool(self):
        self.post(title='A', column='todo')
        r = self.c.post('/mcp', json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                                      'params': {'name': 'get_card_stats', 'arguments': {'cards': False}}})
        out = r.get_json()['result']['structuredContent']
        self.assertNotIn('cards', out)
        self.assertEqual(out['summary']['cards'], 1)


class MigrationTests(unittest.TestCase):
    def test_v1_database_gets_one_global_order(self):
        """A v1 database (per-column order + array position) is renumbered into the order the
        list view shows today: visible cards by (order, column, position), hidden cards last."""
        import sqlite3
        import subprocess
        d = tempfile.mkdtemp(prefix='perkan-v1-')
        conn = sqlite3.connect(os.path.join(d, 'perkan.db'))
        conn.executescript("""
            CREATE TABLE columns (id TEXT PRIMARY KEY, title TEXT NOT NULL, color TEXT NOT NULL,
                                  hidden INTEGER NOT NULL DEFAULT 0, position INTEGER NOT NULL);
            CREATE TABLE projects (name TEXT PRIMARY KEY, color TEXT NOT NULL, position INTEGER NOT NULL);
            CREATE TABLE cards (id TEXT PRIMARY KEY, column_id TEXT NOT NULL REFERENCES columns(id) ON DELETE CASCADE,
                pos INTEGER NOT NULL, title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', project TEXT,
                assignee TEXT, color TEXT NOT NULL, ord REAL NOT NULL);
            CREATE INDEX idx_cards_column ON cards(column_id, pos);
            CREATE TABLE card_links (card_id TEXT NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
                idx INTEGER NOT NULL, text TEXT NOT NULL, url TEXT NOT NULL, PRIMARY KEY (card_id, idx));
            INSERT INTO columns VALUES ('a','A','#111',0,0), ('b','B','#222',0,1), ('h','H','#333',1,2);
            -- ties at 1e6 (broken by column, then array position); array order differs from ord order
            INSERT INTO cards VALUES ('a1','a',0,'a1','',NULL,NULL,'#1',3000000),
                                     ('a2','a',1,'a2','',NULL,NULL,'#1',1000000),
                                     ('b1','b',0,'b1','',NULL,NULL,'#1',1000000),
                                     ('b2','b',1,'b2','',NULL,NULL,'#1',1000000),
                                     ('h1','h',0,'h1','',NULL,NULL,'#1',500000);
            INSERT INTO card_links VALUES ('a1',0,'x','http://x');
            PRAGMA user_version = 1;
        """)
        conn.commit()
        conn.close()
        code = ("import os; os.environ['PERKAN_DATA_DIR']=%r; import sys; sys.path.insert(0,%r); import app, db, json\n"
                "from app import app as a\n"
                "c=a.test_client(); r=c.get('/api/cards?include_hidden=true').get_json()['cards']\n"
                "print(json.dumps([[x['id'], x['order'], x['column']] for x in r]))\n"
                "print(json.dumps([k for col in c.get('/api/board?include_hidden=true').get_json()['columns'] for k in col['cards'] if k['id']=='a1'][0]['links']))\n"
                "print(db._connect().execute('PRAGMA user_version').fetchone()[0])\n"
                % (d, os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
        out = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, check=True).stdout.split('\n')
        cards = json.loads(out[0])
        self.assertEqual([c[0] for c in cards], ['a2', 'b1', 'b2', 'a1', 'h1'])
        self.assertEqual([c[1] for c in cards], [1e6, 2e6, 3e6, 4e6, 5e6])
        self.assertEqual(json.loads(out[1]), [{'text': 'x', 'url': 'http://x'}])   # links survive the schema change
        self.assertEqual(out[2], str(db.SCHEMA_VERSION))

class FirstColumnGateTests(Base):
    def start_sprint(self):
        with db.write() as conn:
            sid = db.create_sprint(conn, '2999-01-01')
            db.start_sprint(conn, sid)
        return sid

    def try_put(self, card_id, **body):
        return self.c.put(f'/api/card/{card_id}', json=body)

    def test_card_outside_the_active_sprint_cannot_leave_the_first_column(self):
        self.assertEqual(self.try_put('t1', column='doing').status_code, 409)   # no active sprint at all
        self.start_sprint()
        r = self.try_put('t1', column='doing')                                  # backlog card
        self.assertEqual(r.status_code, 409, r.data)
        self.assertIn('active sprint', r.get_json()['error'])
        self.assertEqual(self.ids('doing'), ['d1', 'd2'])

    def test_card_in_another_sprint_cannot_leave_the_first_column(self):
        self.start_sprint()
        with db.write() as conn:
            other = db.create_sprint(conn, '2999-02-01')
        self.put('t1', sprint=other)
        self.assertEqual(self.try_put('t1', column='doing').status_code, 409)

    def test_card_in_the_active_sprint_can_move(self):
        self.start_sprint()
        self.put('t1', sprint='active')
        self.put('t1', column='doing')
        self.assertIn('t1', self.ids('doing'))

    def test_joining_the_sprint_and_moving_in_one_call_is_allowed(self):
        self.start_sprint()
        self.put('t2', sprint='active', column='doing')
        self.assertIn('t2', self.ids('doing'))

    def test_card_created_past_the_first_column_joins_the_active_sprint_mid_sprint(self):
        sid = self.start_sprint()
        with db.write() as conn:                                  # started long ago, so a new card is "mid-sprint"
            conn.execute("UPDATE sprints SET started_at = '2000-01-01T00:00:00Z' WHERE id = ?", (sid,))
        for col in ('doing', 'done'):
            card = self.post(title='walk-in ' + col, column=col)
            self.assertEqual(card['sprint'], sid)
            self.assertTrue(card['added_after_start'])
        self.assertNotIn('sprint', self.post(title='plain', column='todo'))                     # first column: backlog
        self.assertNotIn('sprint', self.post(title='explicit', column='doing', sprint='backlog'))  # explicit wins
        self.assertEqual(self.get('/api/sprints')['sprints'][0]['added'], 2)

    def test_card_created_past_the_first_column_without_an_active_sprint_stays_in_the_backlog(self):
        self.assertNotIn('sprint', self.post(title='no sprint', column='doing'))

    def test_the_rule_only_applies_to_leaving_the_first_column(self):
        self.start_sprint()
        self.put('d1', column='done')                 # backlog card already past the first column
        self.put('d1', column='todo')                 # moving back is fine
        self.put('t3', order=1.5)                     # reorder within the column is fine
        self.put('t3', title='renamed')


class SprintTests(Base):
    def mk(self, end='2026-12-01', **kw):
        r = self.c.post('/api/sprint', json=dict(end_date=end, **kw))
        self.assertEqual(r.status_code, 201, r.get_json())
        return r.get_json()

    def card_sprint(self, cid):
        return self.get('/api/cards?include_hidden=true&include_closed=true&query=' + cid)['cards']

    def sprint_of(self, cid):
        return self.c.get('/api/card/' + cid + '/sprint-history').get_json()['sprint']

    def test_create_validates_and_names(self):
        self.assertEqual(self.c.post('/api/sprint', json={}).status_code, 400)
        self.assertEqual(self.c.post('/api/sprint', json={'end_date': '12/01'}).status_code, 400)
        self.assertEqual(self.c.post('/api/sprint', json={'end_date': '2026-02-30'}).status_code, 400)
        self.assertEqual(self.c.post('/api/sprint', json={'end_date': '2026-12-01', 'start_date': '2026-12-05'}).status_code, 400)
        sp = self.mk()
        self.assertEqual(sp['state'], 'planned')
        self.assertEqual(sp['name'], 'Sprint 12/01')
        self.assertEqual(self.mk(name='Mine')['name'], 'Mine')

    def test_only_one_active_sprint(self):
        a, b = self.mk('2026-12-01'), self.mk('2026-12-08')
        self.assertEqual(self.c.post('/api/sprint/%s/start' % a['id']).get_json()['state'], 'active')
        r = self.c.post('/api/sprint/%s/start' % b['id'])
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.c.post('/api/sprint/%s/start' % a['id']).status_code, 400)   # already active
        self.assertEqual([s['id'] for s in self.get('/api/sprints')['sprints']], [a['id'], b['id']])

    def test_card_sprint_argument(self):
        a = self.mk()
        self.assertEqual(self.c.post('/api/card', json={'title': 'x', 'sprint': 'active'}).status_code, 400)  # none active
        self.c.post('/api/sprint/%s/start' % a['id'])
        cid = self.c.post('/api/card', json={'title': 'x', 'sprint': 'active'}).get_json()['id']
        self.assertEqual(self.sprint_of(cid), a['id'])
        self.assertEqual(self.c.put('/api/card/t1', json={'sprint': a['id']}).get_json()['sprint'], a['id'])
        self.assertNotIn('sprint', self.c.put('/api/card/t1', json={'sprint': 'backlog'}).get_json())
        self.assertEqual(self.c.put('/api/card/t1', json={'sprint': 'nope'}).status_code, 404)
        hist = self.c.get('/api/card/t1/sprint-history').get_json()['history']
        self.assertEqual([(h['from'], h['to']) for h in hist], [(None, a['id']), (a['id'], None)])

    def test_complete_carry_over(self):
        a = self.mk('2026-12-01')
        self.c.post('/api/sprint/%s/start' % a['id'])
        for cid in ('t1', 'd1', 'd2'):
            self.c.put('/api/card/' + cid, json={'sprint': a['id']})
        self.c.put('/api/card/d2', json={'column': 'done'})
        r = self.c.post('/api/sprint/%s/complete' % a['id'], json={'next_end_date': '2026-12-08'})
        self.assertEqual(r.status_code, 200, r.get_json())
        j = r.get_json()
        self.assertEqual((j['committed'], j['completed'], j['unfinished']), (3, 1, 2))
        self.assertEqual(j['sprint']['state'], 'closed')
        self.assertEqual(j['sprint']['carried'], 2)
        nxt = j['next_sprint']
        self.assertEqual(nxt['state'], 'planned')
        self.assertEqual(nxt['end_date'], '2026-12-08')
        cards = {c['id']: c for c in self.get('/api/cards?sprint=%s' % nxt['id'])['cards']}
        self.assertEqual(set(cards), {'t1', 'd1'})
        self.assertEqual(cards['d1']['column'], 'doing')               # status is kept
        self.assertEqual(self.sprint_of('d2'), a['id'])                # done card stays in the closed sprint
        # closed sprint's done cards are hidden from the board and card lists ...
        board = self.get('/api/board')
        self.assertNotIn('d2', [c['id'] for col in board['columns'] for c in col['cards']])
        self.assertNotIn('d2', [c['id'] for c in self.get('/api/cards')['cards']])
        # ... unless asked for
        self.assertIn('d2', [c['id'] for c in self.get('/api/cards?include_closed=true')['cards']])
        self.assertIn('d2', [c['id'] for c in self.get('/api/cards?sprint=%s' % a['id'])['cards']])
        self.assertEqual([s['id'] for s in self.get('/api/sprints')['sprints']], [nxt['id']])
        self.assertEqual(len(self.get('/api/sprints?include_closed=true')['sprints']), 2)
        # cards cannot be moved into the closed sprint
        self.assertEqual(self.c.put('/api/card/t2', json={'sprint': a['id']}).status_code, 400)

    def test_mid_sprint_additions(self):
        a = self.mk('2026-12-01')
        self.c.put('/api/card/t1', json={'sprint': a['id']})            # planned before the start
        self.c.post('/api/sprint/%s/start' % a['id'])
        with db.write() as conn:                                        # pretend it started an hour ago
            conn.execute("UPDATE sprints SET started_at = '2000-01-01T00:00:00Z' WHERE id = ?", (a['id'],))
            conn.execute("UPDATE card_sprint_history SET at = '1999-12-31T00:00:00Z' WHERE card_id = 't1'")
        self.c.put('/api/card/d1', json={'sprint': a['id']})            # added after the start
        new = self.c.post('/api/card', json={'title': 'late', 'sprint': 'active'}).get_json()
        self.assertTrue(new['added_after_start'])
        cards = {c['id']: c for c in self.get('/api/cards?sprint=active')['cards']}
        self.assertNotIn('added_after_start', cards['t1'])
        self.assertTrue(cards['d1']['added_after_start'])
        board = {c['id']: c for col in self.get('/api/board')['columns'] for c in col['cards']}
        self.assertTrue(board['d1']['added_after_start'])
        self.assertNotIn('added_after_start', board['t1'])
        self.assertEqual(self.get('/api/sprints')['sprints'][0]['added'], 2)
        st = self.get('/api/stats')['by_sprint'][a['id']]
        self.assertEqual((st['committed'], st['added']), (1, 2))
        self.c.put('/api/card/d1', json={'sprint': 'backlog'})          # leaving drops the flag
        self.assertEqual(self.get('/api/sprints')['sprints'][0]['added'], 1)
        j = self.c.post('/api/sprint/%s/complete' % a['id'], json={'unfinished': 'backlog'}).get_json()
        self.assertEqual((j['committed'], j['added'], j['sprint']['added']), (1, 1, 1))

    def test_stats_by_sprint(self):
        a = self.mk('2026-12-01')
        self.c.post('/api/sprint/%s/start' % a['id'])
        for cid in ('t1', 'd1'):
            self.c.put('/api/card/' + cid, json={'sprint': a['id']})
        self.c.put('/api/card/d1', json={'column': 'done'})
        st = self.get('/api/stats')
        self.assertEqual((st['by_sprint'][a['id']]['committed'], st['by_sprint'][a['id']]['completed']), (2, 1))
        self.c.post('/api/sprint/%s/complete' % a['id'], json={'unfinished': 'backlog'})
        st = self.get('/api/stats?sprint=%s' % a['id'])
        self.assertEqual(st['by_sprint'][a['id']]['committed'], 2)
        self.assertEqual(st['by_sprint'][a['id']]['carried'], 0)
        self.assertEqual([c['id'] for c in st['cards']], ['d1'])        # t1 went back to the backlog

    def test_complete_default_next_week_and_start_next(self):
        a = self.mk('2026-12-01')
        self.c.post('/api/sprint/%s/start' % a['id'])
        self.c.put('/api/card/t1', json={'sprint': a['id']})
        j = self.c.post('/api/sprint/%s/complete' % a['id'], json={'start_next': True}).get_json()
        self.assertEqual(j['next_sprint']['end_date'], '2026-12-08')
        self.assertEqual(j['next_sprint']['state'], 'active')

    def test_complete_next_start_date_is_kept_when_started(self):
        a = self.mk('2026-12-01')
        self.c.post('/api/sprint/%s/start' % a['id'])
        self.c.put('/api/card/t1', json={'sprint': a['id']})
        j = self.c.post('/api/sprint/%s/complete' % a['id'],
                        json={'next_end_date': '2026-12-11', 'next_start_date': '2026-12-07', 'start_next': True}).get_json()
        self.assertEqual(j['next_sprint']['start_date'], '2026-12-07')
        self.assertEqual(j['next_sprint']['end_date'], '2026-12-11')
        self.assertEqual(j['next_sprint']['state'], 'active')

    def test_planned_sprint_start_date_survives_start(self):
        a = self.c.post('/api/sprint', json={'end_date': '2026-12-11', 'start_date': '2026-12-07'}).get_json()
        self.assertEqual(a['start_date'], '2026-12-07')
        self.c.put('/api/sprint/%s' % a['id'], json={'start_date': '2026-12-08'})
        j = self.c.post('/api/sprint/%s/start' % a['id']).get_json()
        self.assertEqual(j['start_date'], '2026-12-08')

    def test_active_sprint_cannot_lose_its_start_date(self):
        a = self.mk('2026-12-01')
        self.c.post('/api/sprint/%s/start' % a['id'])
        self.assertEqual(self.c.put('/api/sprint/%s' % a['id'], json={'start_date': ''}).status_code, 400)

    def test_complete_into_existing_planned_sprint(self):
        a, b = self.mk('2026-12-01'), self.mk('2026-12-08')
        self.c.post('/api/sprint/%s/start' % a['id'])
        self.c.put('/api/card/t1', json={'sprint': a['id']})
        j = self.c.post('/api/sprint/%s/complete' % a['id'], json={'next_sprint': b['id']}).get_json()
        self.assertEqual(j['next_sprint']['id'], b['id'])
        self.assertEqual(self.sprint_of('t1'), b['id'])
        self.assertEqual(len(self.get('/api/sprints?include_closed=true')['sprints']), 2)   # none created

    def test_complete_backlog_and_leave(self):
        for mode, expect in (('backlog', None), ('leave', 'A')):
            a = self.mk('2026-12-01')
            self.c.post('/api/sprint/%s/start' % a['id'])
            self.c.put('/api/card/t1', json={'sprint': a['id']})
            j = self.c.post('/api/sprint/%s/complete' % a['id'], json={'unfinished': mode}).get_json()
            self.assertIsNone(j['next_sprint'])
            self.assertEqual(self.sprint_of('t1'), None if expect is None else a['id'])
            self.c.put('/api/card/t1', json={'sprint': 'backlog'})
        bad = self.mk()
        self.c.post('/api/sprint/%s/start' % bad['id'])
        r = self.c.post('/api/sprint/%s/complete' % bad['id'], json={'unfinished': 'backlog', 'start_next': True})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.c.post('/api/sprint/%s/complete' % bad['id'], json={'unfinished': 'x'}).status_code, 400)

    def test_complete_requires_active_and_overdue_marker(self):
        a = self.mk('2000-01-01')
        self.assertEqual(self.c.post('/api/sprint/%s/complete' % a['id']).status_code, 400)   # still planned
        self.c.post('/api/sprint/%s/start' % a['id'], json={'start_date': '2000-01-01'})
        sp = self.get('/api/sprints')['sprints'][0]
        self.assertGreater(sp['overdue_days'], 0)
        self.assertEqual(sp['state'], 'active')                                              # no auto-close

    def test_update_and_delete(self):
        a = self.mk('2026-12-01')
        self.c.put('/api/card/t1', json={'sprint': a['id']})
        r = self.c.put('/api/sprint/%s' % a['id'], json={'name': 'Renamed', 'end_date': '2026-12-03'})
        self.assertEqual((r.get_json()['name'], r.get_json()['end_date']), ('Renamed', '2026-12-03'))
        self.assertEqual(self.c.put('/api/sprint/%s' % a['id'], json={'start_date': '2027-01-01'}).status_code, 400)
        self.assertEqual(self.c.delete('/api/sprint/%s' % a['id']).status_code, 200)
        self.assertIsNone(self.sprint_of('t1'))
        self.assertEqual(self.c.delete('/api/sprint/%s' % a['id']).status_code, 404)

    def test_export_import_keeps_sprints(self):
        a = self.mk('2026-12-01')
        self.c.put('/api/card/t1', json={'sprint': a['id']})
        exported = self.c.get('/api/board/export').get_json()
        import io
        r = self.c.post('/api/board/import', data={'mode': 'replace',
                        'file': (io.BytesIO(json.dumps(exported).encode()), 'kanban.json')},
                        content_type='multipart/form-data')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.sprint_of('t1'), a['id'])
        self.assertEqual([s['id'] for s in self.get('/api/sprints')['sprints']], [a['id']])


class SprintMigrationTests(Base):
    def test_weekly_done_columns_become_closed_sprints(self):
        b = board_fixture()
        b['columns'].insert(3, {'id': 'w1', 'title': 'Done 10/30', 'hidden': True, 'is_done': True,
                                'cards': [{'id': 'w1a', 'title': 'W', 'description': 'DONE 2026-10-29', 'order': 8e6}]})
        with db.write() as conn:
            db.replace_board(conn, perkan._normalize_board(b))
        with db.write() as conn:
            before = db.card_dates(conn, ['w1a'])['w1a']
            report = db.migrate_to_sprints(conn, today_str='2026-11-06')
            after = db.card_dates(conn, ['w1a'])['w1a']
        self.assertEqual(before[1], after[1])                          # completed_at untouched
        self.assertEqual(after[0], None)                               # and the move does not count as started
        self.assertEqual([x['from_column'] for x in report['closed_sprints']], ['Done 10/30'])
        self.assertIsNone(report['active_sprint'])                     # "Doing" is not an In Progress / Burn-In column
        card = self.get('/api/cards?include_closed=true&query=W')['cards'][0]
        self.assertEqual(card['column'], 'done')
        self.assertEqual(self.get('/api/sprints?include_closed=true')['sprints'][-1]['end_date'], '2026-10-30')
        self.assertNotIn('w1a', [c['id'] for col in self.get('/api/board')['columns'] for c in col['cards']])
        with db.write() as conn:
            with self.assertRaises(db.SprintError):
                db.migrate_to_sprints(conn)                            # refuses to run twice


class ProjectFieldTests(Base):
    def call(self, tool, **arguments):
        r = self.c.post('/mcp', json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                                       'params': {'name': tool, 'arguments': arguments}})
        result = r.get_json()['result']
        self.assertFalse(result['isError'], result)
        return result.get('structuredContent')

    def test_project_without_fields_shows_only_name_and_color(self):
        self.c.post('/api/project', json={'name': 'Plain', 'color': '#111111'})
        self.assertEqual(self.get('/api/projects')['projects'], [{'name': 'Plain', 'color': '#111111'}])

    def test_create_update_and_clear_fields(self):
        r = self.c.post('/api/project', json={'name': 'P', 'color': '#222222', 'repo_path': ' /src/p ',
                                              'links': [{'text': 'Docs', 'url': 'http://d'}, {'url': 'http://x'}, {'text': 'no url'}]})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.get_json()['repo_path'], '/src/p')
        self.assertEqual(r.get_json()['links'], [{'text': 'Docs', 'url': 'http://d'}, {'text': 'http://x', 'url': 'http://x'}])
        # a partial update leaves the other fields alone
        r = self.c.put('/api/project/0', json={'notes': 'run tests in a container', 'repo_url': 'https://g/p'})
        proj = r.get_json()
        self.assertEqual((proj['repo_path'], proj['repo_url'], proj['notes']), ('/src/p', 'https://g/p', 'run tests in a container'))
        self.assertEqual(len(proj['links']), 2)
        # shown by list_projects and get_board
        self.assertEqual(self.get('/api/projects')['projects'][0], proj)
        self.assertEqual(self.get('/api/board')['projects'][0], proj)
        # empty values clear
        proj = self.c.put('/api/project/0', json={'repo_path': '', 'links': []}).get_json()
        self.assertNotIn('repo_path', proj)
        self.assertNotIn('links', proj)
        self.assertEqual(proj['notes'], 'run tests in a container')

    def test_rename_and_recolor_keep_fields(self):
        self.c.post('/api/project', json={'name': 'Old', 'repo_path': '/old'})
        proj = self.c.put('/api/project/0', json={'name': 'New', 'color': '#333333'}).get_json()
        self.assertEqual((proj['name'], proj['repo_path']), ('New', '/old'))

    def test_export_import_round_trip_and_merge_never_blanks(self):
        self.c.post('/api/project', json={'name': 'P', 'repo_path': '/p', 'notes': 'n', 'links': [{'url': 'http://l'}]})
        exported = json.loads(self.c.get('/api/board/export').data)
        self.assertEqual(exported['projects'][0]['repo_path'], '/p')
        self.c.put('/api/project/0', json={'repo_path': '', 'notes': '', 'links': []})
        r = self.c.post('/api/board/import', data={'mode': 'merge', 'file': (io.BytesIO(json.dumps(exported).encode()), 'f.json')},
                        content_type='multipart/form-data')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(self.get('/api/projects')['projects'][0]['repo_path'], '/p')
        # an old export (no extras) merged over a project keeps what is there
        old = {'columns': [], 'projects': [{'name': 'P', 'color': '#444444'}]}
        self.c.post('/api/board/import', data={'mode': 'merge', 'file': (io.BytesIO(json.dumps(old).encode()), 'f.json')},
                    content_type='multipart/form-data')
        proj = self.get('/api/projects')['projects'][0]
        self.assertEqual((proj['color'], proj['repo_path'], proj['notes']), ('#444444', '/p', 'n'))
        # replace restores them from the export
        self.c.put('/api/project/0', json={'repo_path': ''})
        self.c.post('/api/board/import', data={'mode': 'replace', 'file': (io.BytesIO(json.dumps(exported).encode()), 'f.json')},
                    content_type='multipart/form-data')
        self.assertEqual(self.get('/api/projects')['projects'][0]['links'], [{'text': 'http://l', 'url': 'http://l'}])

    def test_mcp_tools_carry_the_fields(self):
        made = self.call('create_project', name='M', repo_path='/m', links=[{'text': 'T', 'url': 'http://t'}])
        self.assertEqual(made['repo_path'], '/m')
        upd = self.call('update_project', current_name='M', repo_url='https://g/m', notes='hello')
        self.assertEqual((upd['repo_path'], upd['repo_url'], upd['notes']), ('/m', 'https://g/m', 'hello'))
        listed = self.call('list_projects')['projects'][0]
        self.assertEqual(listed['links'], [{'text': 'T', 'url': 'http://t'}])
        self.assertEqual(self.call('get_board')['projects'][0]['repo_path'], '/m')
        self.assertNotIn('notes', self.call('update_project', current_name='M', notes=''))
        tools = {t['name']: t for t in self.c.post('/mcp', json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}).get_json()['result']['tools']}
        for name in ('create_project', 'update_project'):
            self.assertTrue({'repo_path', 'repo_url', 'notes', 'links'} <= set(tools[name]['inputSchema']['properties']))

    def test_projects_are_alphabetical_and_have_no_position(self):
        for name in ('beta', 'Alpha', 'gamma', 'alpha2'):
            self.c.post('/api/project', json={'name': name, 'position': 0})      # position is ignored
        names = [p['name'] for p in self.get('/api/projects')['projects']]
        self.assertEqual(names, ['Alpha', 'alpha2', 'beta', 'gamma'])
        self.assertEqual([p['name'] for p in self.get('/api/board')['projects']], names)
        self.c.put('/api/project/1', json={'name': 'zeta', 'position': 0})       # renamed: sorts to the end
        self.assertEqual([p['name'] for p in self.get('/api/projects')['projects']], ['Alpha', 'beta', 'gamma', 'zeta'])
        self.assertEqual(self.c.delete('/api/project/0').status_code, 200)      # index = alphabetical position
        self.assertEqual([p['name'] for p in self.get('/api/projects')['projects']], ['beta', 'gamma', 'zeta'])
        with db.read() as conn:
            self.assertNotIn('position', [r['name'] for r in conn.execute('PRAGMA table_info(projects)')])
        tools = {t['name']: t for t in self.c.post('/mcp', json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}).get_json()['result']['tools']}
        for name in ('create_project', 'update_project'):
            self.assertNotIn('position', tools[name]['inputSchema']['properties'])

    def test_v7_database_gains_the_columns(self):
        import sqlite3
        import subprocess
        d = tempfile.mkdtemp(prefix='perkan-v7-')
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        prefix = "import os; os.environ['PERKAN_DATA_DIR']=%r; import sys; sys.path.insert(0,%r); import app, db, json\n" % (d, root)
        subprocess.run([sys.executable, '-c', prefix + "app.app.test_client().post('/api/project', json={'name': 'Old'})"], check=True)
        conn = sqlite3.connect(os.path.join(d, 'perkan.db'))
        for col in ('repo_path', 'repo_url', 'notes', 'links'):
            conn.execute('ALTER TABLE projects DROP COLUMN %s' % col)
        conn.execute('ALTER TABLE projects ADD COLUMN position INTEGER NOT NULL DEFAULT 0')   # what v7 had
        conn.execute('ALTER TABLE columns DROP COLUMN stacked')                   # added in v10
        conn.execute('ALTER TABLE columns DROP COLUMN counts_as_work')            # added in v12
        conn.execute('PRAGMA user_version = 7')
        conn.commit()
        conn.close()
        out = subprocess.run([sys.executable, '-c', prefix + "c=app.app.test_client()\n"
                              "c.put('/api/project/0', json={'repo_path': '/after'})\n"
                              "print(json.dumps(c.get('/api/projects').get_json()['projects']))\n"
                              "print(db._connect().execute('PRAGMA user_version').fetchone()[0])"],
                             capture_output=True, text=True, check=True).stdout.split('\n')
        self.assertEqual(json.loads(out[0])[0]['repo_path'], '/after')
        self.assertEqual(out[1], str(db.SCHEMA_VERSION))


class BacklogAddCardTests(Base):
    """The sprints page adds backlog cards at the top or bottom: sprint='backlog' plus, for the top,
    an explicit order just below the first backlog card."""
    def backlog_ids(self):
        return [c['id'] for c in self.get('/api/cards?sprint=backlog')['cards']]

    def test_new_card_goes_to_top_or_bottom_of_backlog(self):
        before = self.backlog_ids()
        first = self.get('/api/cards?sprint=backlog')['cards'][0]['order']
        top = self.post(title='top', column='todo', sprint='backlog', order=first - 1, project='P', assignee='sam')
        bottom = self.post(title='bottom', column='todo', sprint='backlog')
        self.assertEqual(self.backlog_ids(), [top['id']] + before + [bottom['id']])
        self.assertEqual((top['project'], top['assignee']), ('P', 'sam'))
        self.assertIsNone(top.get('sprint'))
        self.assert_invariants()


class StackedColumnTests(Base):
    def call(self, tool, **arguments):
        r = self.c.post('/mcp', json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                                       'params': {'name': tool, 'arguments': arguments}})
        result = r.get_json()['result']
        self.assertFalse(result['isError'], result)
        return result.get('structuredContent')

    def stacked(self, url='/api/columns'):
        return {c['id']: c['stacked'] for c in self.get(url)['columns']}

    def test_default_off_set_and_clear_without_moving_cards(self):
        self.assertEqual(set(self.stacked().values()), {False})
        before = self.global_ids()
        r = self.c.put('/api/column/doing', json={'stacked': True})
        self.assertTrue(r.get_json()['stacked'])
        self.assertTrue(self.stacked()['doing'])
        self.assertTrue(next(c for c in self.get('/api/board')['columns'] if c['id'] == 'doing')['stacked'])
        self.c.put('/api/column/doing', json={'stacked': 'false'})     # clients send string booleans
        self.assertFalse(self.stacked()['doing'])
        self.assertEqual(self.global_ids(), before)
        self.assertEqual(self.ids('doing'), ['d1', 'd2'])

    def test_create_with_the_setting_and_through_mcp(self):
        r = self.c.post('/api/column', json={'title': 'Parked', 'stacked': True})
        self.assertTrue(r.get_json()['stacked'])
        self.assertTrue(self.stacked()['parked'])
        made = self.call('create_column', title='Waiting', stacked='true')
        self.assertEqual(self.call('list_columns')['columns'][-1]['stacked'], True)
        self.call('update_column', id=made['id'], stacked=False)
        self.assertFalse(self.stacked()[made['id']])
        tools = {t['name']: t for t in self.c.post('/mcp', json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}).get_json()['result']['tools']}
        for name in ('create_column', 'update_column'):
            self.assertIn('stacked', tools[name]['inputSchema']['properties'])

    def test_setting_survives_export_import_and_hidden_columns_keep_it(self):
        self.c.put('/api/column/doing', json={'stacked': True})
        self.c.put('/api/column/archive', json={'stacked': True})
        exported = json.loads(self.c.get('/api/board/export').data)
        self.c.put('/api/column/doing', json={'stacked': False})
        r = self.c.post('/api/board/import', data={'mode': 'replace', 'file': (io.BytesIO(json.dumps(exported).encode()), 'f.json')},
                        content_type='multipart/form-data')
        self.assertEqual(r.status_code, 200, r.data)
        got = self.stacked('/api/columns?include_hidden=true')
        self.assertTrue(got['doing'] and got['archive'] and not got['todo'])

    def test_v9_database_gains_the_column(self):
        import sqlite3
        import subprocess
        d = tempfile.mkdtemp(prefix='perkan-v9-')
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        prefix = "import os; os.environ['PERKAN_DATA_DIR']=%r; import sys; sys.path.insert(0,%r); import app, db, json\n" % (d, root)
        subprocess.run([sys.executable, '-c', prefix + "pass"], check=True)
        conn = sqlite3.connect(os.path.join(d, 'perkan.db'))
        conn.execute('ALTER TABLE columns DROP COLUMN stacked')
        conn.execute('ALTER TABLE columns DROP COLUMN counts_as_work')
        conn.execute('PRAGMA user_version = 9')
        conn.commit()
        conn.close()
        out = subprocess.run([sys.executable, '-c', prefix + "c=app.app.test_client()\n"
                              "c.put('/api/column/inprogress', json={'stacked': True})\n"
                              "print(json.dumps({x['id']: x['stacked'] for x in c.get('/api/columns').get_json()['columns']}))\n"
                              "print(db._connect().execute('PRAGMA user_version').fetchone()[0])"],
                             capture_output=True, text=True, check=True).stdout.split('\n')
        self.assertEqual(json.loads(out[0]), {'todo': False, 'inprogress': True, 'blocked': False, 'done': False})
        self.assertEqual(out[1], str(db.SCHEMA_VERSION))

    def test_v10_database_renames_the_column(self):
        import sqlite3
        import subprocess
        d = tempfile.mkdtemp(prefix='perkan-v10-')
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        prefix = "import os; os.environ['PERKAN_DATA_DIR']=%r; import sys; sys.path.insert(0,%r); import app, db, json\n" % (d, root)
        subprocess.run([sys.executable, '-c', prefix + "app.app.test_client().put('/api/column/inprogress', json={'stacked': True})"], check=True)
        conn = sqlite3.connect(os.path.join(d, 'perkan.db'))
        conn.execute('ALTER TABLE columns RENAME COLUMN stacked TO stack_with_previous')    # what v10 called it
        conn.execute('ALTER TABLE columns DROP COLUMN counts_as_work')
        conn.execute('PRAGMA user_version = 10')
        conn.commit()
        conn.close()
        out = subprocess.run([sys.executable, '-c', prefix + "c=app.app.test_client()\n"
                              "print(json.dumps({x['id']: x['stacked'] for x in c.get('/api/columns').get_json()['columns']}))\n"
                              "print(db._connect().execute('PRAGMA user_version').fetchone()[0])"],
                             capture_output=True, text=True, check=True).stdout.split('\n')
        self.assertEqual(json.loads(out[0]), {'todo': False, 'inprogress': True, 'blocked': False, 'done': False})   # kept
        self.assertEqual(out[1], str(db.SCHEMA_VERSION))

    def test_board_and_settings_pages_wire_the_stack(self):
        js = self.c.get('/static/app.js').data
        self.assertIn(b'column-stack', js)
        self.assertIn(b'stacked', js)
        self.assertIn(b'lastStacked', js)               # neighbouring stacked columns share a slot
        self.assertIn(b'.column-stack', self.c.get('/static/style.css').data)
        self.assertIn(b'col-stack', self.c.get('/static/settings.js').data)


class BurnupTests(Base):
    """Cumulative minutes spent per day, checked against a hand calculation (times are UTC; PERKAN_TZ is unset).

    Sprint S: 2026-11-02 .. 2026-11-06, "now" = 2026-11-04 12:00. Only the column 'doing' counts as work.
      A  in S from 11-01. doing 11-02 09:00-12:00 (180), back in doing 11-03 10:00 until now (1560)  -> 1740
      B  in S, sits in 'archive' (waiting, not work) since 11-02 09:00                                -> 0
      C  carried over: in doing since 11-02 20:00, joined S 11-03 08:00 -> counted from 08:00 (1680)  -> 1680
      D  only a backfill history row                                                                  -> unknown
      E  in S, no sprint history at all; doing 11-02 12:00-18:00, then done                           -> 360, flagged
      F  in S 11-02 00:00 to 11-03 00:00 (then moved to another sprint), doing since 11-02 12:00      -> 720 (12h)
    Cumulative at the end of 11-02 (11-03 00:00): A 180, C 0, E 360, F 720            -> 1260
    Cumulative at the end of 11-03 (11-04 00:00): A 1020, C 960, E 360, F 720          -> 3060
    Now (11-04 12:00):                            A 1740, C 1680, E 360, F 720         -> 4500
    """
    NOW = datetime.datetime(2026, 11, 4, 12, 0, tzinfo=datetime.timezone.utc)

    def make(self, title):
        return self.post(title=title, column='todo')['id']

    def history(self, cid, rows):
        """rows: [(column, 'YYYY-MM-DD HH:MM', source)] replaces the card's column history."""
        with db.write() as conn:
            conn.execute('DELETE FROM card_history WHERE card_id = ?', (cid,))
            prev = None
            for col, at, source in rows:
                conn.execute('INSERT INTO card_history (card_id, from_column, to_column, at, source) VALUES (?,?,?,?,?)',
                             (cid, prev, col, at.replace(' ', 'T') + ':00Z', source))
                prev = col

    def in_sprint(self, cid, sid, entered=None, left=None, other=None):
        with db.write() as conn:
            conn.execute('DELETE FROM card_sprint_history WHERE card_id = ?', (cid,))
            if entered:
                conn.execute('INSERT INTO card_sprint_history (card_id, from_sprint, to_sprint, at, source) VALUES (?,?,?,?,?)',
                             (cid, None, sid, entered.replace(' ', 'T') + ':00Z', 'api'))
            if left:
                conn.execute('INSERT INTO card_sprint_history (card_id, from_sprint, to_sprint, at, source) VALUES (?,?,?,?,?)',
                             (cid, sid, other, left.replace(' ', 'T') + ':00Z', 'api'))
            conn.execute('UPDATE cards SET sprint_id = ? WHERE id = ?', (other if left else sid, cid))

    def setUp(self):
        super().setUp()
        self.c.put('/api/column/doing', json={'counts_as_work': True})
        with db.write() as conn:
            self.sid = db.create_sprint(conn, '2026-11-06', name='S', start_date='2026-11-02')
            db.start_sprint(conn, self.sid, start_date='2026-11-02')
            self.other = db.create_sprint(conn, '2026-11-13', name='Next')
        ids = {n: self.make(n) for n in 'ABCDEF'}
        self.ids = ids
        P = 'api'
        self.history(ids['A'], [('todo', '2026-11-01 08:00', P), ('doing', '2026-11-02 09:00', P),
                                ('todo', '2026-11-02 12:00', P), ('doing', '2026-11-03 10:00', P)])
        self.in_sprint(ids['A'], self.sid, entered='2026-11-01 08:00')
        self.history(ids['B'], [('todo', '2026-11-01 08:00', P), ('archive', '2026-11-02 09:00', P)])
        self.in_sprint(ids['B'], self.sid, entered='2026-11-01 08:00')
        self.history(ids['C'], [('todo', '2026-11-01 08:00', P), ('doing', '2026-11-02 20:00', P)])
        self.in_sprint(ids['C'], self.sid, entered='2026-11-03 08:00')
        self.history(ids['D'], [('done', '2026-10-30 00:00', 'backfill')])
        self.in_sprint(ids['D'], self.sid, entered='2026-11-01 08:00')
        self.history(ids['E'], [('todo', '2026-11-01 08:00', P), ('doing', '2026-11-02 12:00', P), ('done', '2026-11-02 18:00', P)])
        self.in_sprint(ids['E'], self.sid)                      # no sprint history row at all
        with db.write() as conn:
            conn.execute('UPDATE cards SET sprint_id = ? WHERE id = ?', (self.sid, ids['E']))
        self.history(ids['F'], [('todo', '2026-11-01 08:00', P), ('doing', '2026-11-02 12:00', P)])
        self.in_sprint(ids['F'], self.sid, entered='2026-11-02 00:00', left='2026-11-03 00:00', other=self.other)

    def burnup(self):
        with db.read() as conn:
            return db.sprint_burnup(conn, self.sid, now=self.NOW)

    def test_points_match_the_hand_calculation(self):
        out = self.burnup()
        self.assertEqual(out['days'], ['2026-11-02', '2026-11-03', '2026-11-04', '2026-11-05', '2026-11-06'])
        self.assertEqual([(p['kind'], p['minutes']) for p in out['points']],
                         [('start', 0), ('day_end', 180 + 360 + 720),
                          ('day_end', (180 + 840) + 960 + 360 + 720),
                          ('now', 1740 + 1680 + 360 + 720)])
        self.assertEqual(out['points'][-1]['at'], '2026-11-04T12:00:00Z')      # future days are not plotted
        self.assertEqual(out['total_minutes'], 4500)
        self.assertEqual([w['id'] for w in out['work_columns']], ['doing'])

    def test_hourly_points_match_the_hand_calculation(self):
        with db.read() as conn:
            out = db.sprint_burnup(conn, self.sid, now=self.NOW, step='hour')
        pts = out['points']
        self.assertEqual(out['step'], 'hour')
        self.assertEqual(len(pts), 1 + 59 + 1)          # origin, 11-02 01:00 .. 11-04 11:00, and now
        by_at = {p['at']: p for p in pts}
        self.assertEqual(pts[0]['minutes'], 0)
        self.assertEqual(by_at['2026-11-02T08:00:00Z']['minutes'], 0)
        self.assertEqual(by_at['2026-11-02T12:00:00Z']['minutes'], 180)                   # A's first stay, ended 12:00
        self.assertEqual(by_at['2026-11-02T18:00:00Z']['minutes'], 180 + 360 + 360)       # + E 12-18, F 12-18
        self.assertEqual(by_at['2026-11-03T00:00:00Z']['minutes'], 1260)                  # = the daily point
        self.assertEqual(by_at['2026-11-03T09:00:00Z']['minutes'], 1260 + 60)             # C joins at 08:00: one hour
        self.assertEqual(by_at['2026-11-04T00:00:00Z']['minutes'], 3060)                  # = the daily point
        self.assertEqual((pts[-1]['kind'], pts[-1]['minutes']), ('now', 4500))
        mins = [p['minutes'] for p in pts]
        self.assertEqual(mins, sorted(mins))                                              # cumulative: never falls
        self.assertEqual(pts[1]['local'], '2026-11-02T01:00')
        self.assertEqual(out['tz'], 'UTC')
        self.assertEqual(out['day_edges'][0], '2026-11-02T00:00:00Z')
        self.assertEqual(len(out['day_edges']), 6)

    def test_hourly_points_follow_the_local_time_zone(self):
        os.environ['PERKAN_TZ'] = 'America/Denver'
        try:
            with db.read() as conn:
                out = db.sprint_burnup(conn, self.sid, now=self.NOW, step='hour')
        finally:
            del os.environ['PERKAN_TZ']
        self.assertEqual(out['tz'], 'America/Denver')
        self.assertEqual(out['points'][0]['at'], '2026-11-02T07:00:00Z')      # 11-02 00:00 local (MST, UTC-7: DST ended 11-01)
        self.assertEqual(out['points'][0]['local'], '2026-11-02T00:00')

    def test_per_card_minutes_unknown_cards_and_flags(self):
        out = self.burnup()
        mins = {c['title']: c['minutes'] for c in out['cards']}
        self.assertEqual(mins, {'A': 1740, 'C': 1680, 'E': 360, 'F': 720, 'B': 0})   # waiting time is not work
        self.assertEqual([c['title'] for c in out['cards']][:2], ['A', 'C'])           # biggest first
        self.assertEqual([u['title'] for u in out['unknown']], ['D'])                  # unknown, never 0
        e = next(c for c in out['cards'] if c['title'] == 'E')
        self.assertIn('no sprint history', e['note'])

    def test_no_work_column_and_unstarted_sprint_say_so(self):
        self.c.put('/api/column/doing', json={'counts_as_work': False})
        out = self.burnup()
        self.assertEqual(out['total_minutes'], 0)
        self.assertTrue(any('Counts as work' in n for n in out['notes']))
        with db.read() as conn:
            planned = db.sprint_burnup(conn, self.other, now=self.NOW)
        self.assertEqual(planned['points'], [])
        self.assertTrue(planned['notes'])

    def test_closed_sprint_stops_at_its_close_and_window_end(self):
        with db.write() as conn:
            conn.execute("UPDATE sprints SET state = 'closed', closed_at = '2026-11-03T12:00:00Z' WHERE id = ?", (self.sid,))
        with db.read() as conn:
            out = db.sprint_burnup(conn, self.sid, now=self.NOW)
        self.assertEqual(out['points'][-1]['kind'], 'closed')
        self.assertEqual(out['points'][-1]['at'], '2026-11-03T12:00:00Z')
        # A: 180 + 2h (10:00-12:00 on 11-03), C: 4h (08:00-12:00), E 360, F 720
        self.assertEqual(out['total_minutes'], 180 + 120 + 240 + 360 + 720)

    def test_time_zone_moves_the_day_boundaries(self):
        os.environ['PERKAN_TZ'] = 'America/Denver'          # UTC-7 in November: days end at 07:00 UTC
        try:
            out = self.burnup()
        finally:
            del os.environ['PERKAN_TZ']
        self.assertEqual(out['points'][1]['at'], '2026-11-03T07:00:00Z')

    def test_api_page_and_links(self):
        out = self.get(f'/api/sprint/{self.sid}/burnup')
        self.assertEqual(out['sprint']['name'], 'S')
        self.assertEqual(self.get('/api/sprint/active/burnup')['sprint']['id'], self.sid)
        self.assertEqual(self.c.get('/api/sprint/nope/burnup').status_code, 404)
        self.assertEqual(self.c.get(f'/api/sprint/{self.sid}/burnup?step=week').status_code, 400)
        self.assertEqual(self.get(f'/api/sprint/{self.sid}/burnup?step=hour')['step'], 'hour')
        with db.write() as conn:
            conn.execute("UPDATE sprints SET state = 'closed' WHERE id = ?", (self.sid,))
        r = self.c.get('/api/sprint/active/burnup')
        self.assertEqual(r.status_code, 404)
        self.assertIn('no active sprint', r.get_json()['error'])
        page = self.c.get('/burnup')
        self.assertEqual(page.status_code, 200)
        for needle in (b'id="buChart"', b'burnup.js', b'burnup.css', b'<select id="buTitle"', b'id="buSteps"'):
            self.assertIn(needle, page.data)
        for url in ('/', '/list', '/sprints', '/settings', '/burnup'):
            self.assertIn(b'href="/burnup"' if url != '/burnup' else b'Burn-up', self.c.get(url).data, url)
        self.assertIn(b'/api/sprint/', self.c.get('/static/burnup.js').data)


class CardTimeTests(Base):
    """Total minutes per column from a card's history, checked by hand. now = 2026-11-03 18:00 UTC.
    Card A: created in todo 07:00; doing 07:46; archive (waiting) 12:14; doing again 13:00; done 15:30.
      todo 07:00-07:46 = 46, doing 07:46-12:14 = 268 and 13:00-15:30 = 150 -> 418 over 2 stays,
      archive 12:14-13:00 = 46, done 15:30-18:00 = 150 (the current column counts up to now).
    Only 'doing' counts as work, so work_minutes = 418 and none of the waiting time is in it."""
    NOW = datetime.datetime(2026, 11, 3, 18, 0, tzinfo=datetime.timezone.utc)

    def history(self, cid, rows):
        with db.write() as conn:
            conn.execute('DELETE FROM card_history WHERE card_id = ?', (cid,))
            prev = None
            for col, at, source in rows:
                conn.execute('INSERT INTO card_history (card_id, from_column, to_column, at, source) VALUES (?,?,?,?,?)',
                             (cid, prev, col, '2026-11-03T%s:00Z' % at, source))
                prev = col

    def t(self, cid, now=None):
        with db.read() as conn:
            return db.card_time_in_columns(conn, cid, now=now or self.NOW)

    def test_hand_calculation_repeated_stays_and_waiting_time(self):
        self.c.put('/api/column/doing', json={'counts_as_work': True})
        self.history('t1', [('todo', '07:00', 'api'), ('doing', '07:46', 'api'), ('archive', '12:14', 'api'),
                            ('doing', '13:00', 'api'), ('done', '15:30', 'api')])
        out = self.t('t1')
        self.assertTrue(out['known'])
        self.assertEqual([(c['id'], c['minutes'], c['stays'], c['counts_as_work']) for c in out['columns']],
                         [('todo', 46, 1, False), ('doing', 418, 2, True), ('done', 150, 1, False), ('archive', 46, 1, False)])
        self.assertEqual([c['title'] for c in out['columns']], ['To Do', 'Doing', 'Done', 'Archive'])   # board order
        self.assertEqual(out['work_minutes'], 418)
        # the card's own example: In Progress 07:46 -> Waiting 12:14 is 268 minutes and none of the waiting counts
        self.history('t2', [('todo', '07:00', 'api'), ('doing', '07:46', 'api'), ('archive', '12:14', 'api')])
        out = self.t('t2', now=datetime.datetime(2026, 11, 3, 12, 14, tzinfo=datetime.timezone.utc))
        self.assertEqual(next(c['minutes'] for c in out['columns'] if c['id'] == 'doing'), 268)
        self.assertEqual(out['work_minutes'], 268)
        later = self.t('t2', now=datetime.datetime(2026, 11, 3, 20, 0, tzinfo=datetime.timezone.utc))
        self.assertEqual(later['work_minutes'], 268)                         # waiting time keeps growing elsewhere
        self.assertEqual(next(c['minutes'] for c in later['columns'] if c['id'] == 'archive'), 466)

    def test_no_work_column_ticked_means_zero_work_minutes_but_columns_still_listed(self):
        self.history('t1', [('todo', '07:00', 'api'), ('doing', '08:00', 'api')])
        out = self.t('t1')
        self.assertEqual(out['work_minutes'], 0)
        self.assertEqual(next(c['minutes'] for c in out['columns'] if c['id'] == 'doing'), 600)

    def test_unknown_when_history_was_not_observed(self):
        self.history('t1', [('done', '00:00', 'backfill')])
        self.assertEqual(self.t('t1'), {'known': False, 'reason': 'no recorded column history'})
        self.history('t2', [])
        self.assertFalse(self.t('t2')['known'])                              # unknown, never zeros

    def test_deleted_column_is_listed_last_under_its_id(self):
        self.history('t1', [('todo', '07:00', 'api'), ('gone', '08:00', 'api')])
        out = self.t('t1')
        self.assertEqual([(c['id'], c['title'], c['counts_as_work']) for c in out['columns']],
                         [('todo', 'To Do', False), ('gone', 'gone', False)])

    def test_api_mcp_and_modal_carry_it(self):
        self.c.put('/api/column/doing', json={'counts_as_work': True})
        made = self.post(title='fresh', column='todo')
        h = self.get('/api/card/%s/history' % made['id'])
        self.assertTrue(h['time_in_columns']['known'])
        self.assertEqual(h['time_in_columns']['columns'][0]['id'], 'todo')
        r = self.c.post('/mcp', json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                                       'params': {'name': 'get_card_history', 'arguments': {'id': made['id']}}})
        self.assertIn('time_in_columns', r.get_json()['result']['structuredContent'])
        tools = {t['name']: t for t in self.c.post('/mcp', json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}).get_json()['result']['tools']}
        self.assertIn('time_in_columns', tools['get_card_history']['description'])
        js = self.c.get('/static/app.js').data
        self.assertIn(b'timeInColumnsHtml', js)
        self.assertIn(b'fmtDuration', js)


class WorkColumnTests(Base):
    def call(self, tool, **arguments):
        r = self.c.post('/mcp', json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                                       'params': {'name': tool, 'arguments': arguments}})
        result = r.get_json()['result']
        self.assertFalse(result['isError'], result)
        return result.get('structuredContent')

    def flags(self, url='/api/columns'):
        return {c['id']: c['counts_as_work'] for c in self.get(url)['columns']}

    def test_default_off_set_clear_create_mcp_and_export(self):
        self.assertEqual(set(self.flags().values()), {False})
        self.assertTrue(self.c.put('/api/column/doing', json={'counts_as_work': True}).get_json()['counts_as_work'])
        self.assertTrue(self.flags()['doing'])
        self.c.put('/api/column/doing', json={'counts_as_work': 'false'})
        self.assertFalse(self.flags()['doing'])
        self.assertTrue(self.c.post('/api/column', json={'title': 'Working', 'counts_as_work': True}).get_json()['counts_as_work'])
        made = self.call('create_column', title='Reviewing', counts_as_work='true')
        self.assertTrue(self.flags()[made['id']])
        self.call('update_column', id=made['id'], counts_as_work=False)
        self.assertFalse(self.flags()[made['id']])
        self.assertIn('counts_as_work', self.call('list_columns')['columns'][0])
        tools = {t['name']: t for t in self.c.post('/mcp', json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}).get_json()['result']['tools']}
        for name in ('create_column', 'update_column'):
            self.assertIn('counts_as_work', tools[name]['inputSchema']['properties'])
        self.c.put('/api/column/doing', json={'counts_as_work': True})
        exported = json.loads(self.c.get('/api/board/export').data)
        self.c.put('/api/column/doing', json={'counts_as_work': False})
        r = self.c.post('/api/board/import', data={'mode': 'replace', 'file': (io.BytesIO(json.dumps(exported).encode()), 'f.json')},
                        content_type='multipart/form-data')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertTrue(self.flags()['doing'])
        self.assertIn(b'col-work', self.c.get('/static/settings.js').data)

    def test_v11_database_gains_the_column(self):
        import sqlite3
        import subprocess
        d = tempfile.mkdtemp(prefix='perkan-v11-')
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        prefix = "import os; os.environ['PERKAN_DATA_DIR']=%r; import sys; sys.path.insert(0,%r); import app, db, json\n" % (d, root)
        subprocess.run([sys.executable, '-c', prefix + "pass"], check=True)
        conn = sqlite3.connect(os.path.join(d, 'perkan.db'))
        conn.execute('ALTER TABLE columns DROP COLUMN counts_as_work')
        conn.execute('PRAGMA user_version = 11')
        conn.commit()
        conn.close()
        out = subprocess.run([sys.executable, '-c', prefix + "c=app.app.test_client()\n"
                              "c.put('/api/column/inprogress', json={'counts_as_work': True})\n"
                              "print(json.dumps({x['id']: x['counts_as_work'] for x in c.get('/api/columns').get_json()['columns']}))\n"
                              "print(db._connect().execute('PRAGMA user_version').fetchone()[0])"],
                             capture_output=True, text=True, check=True).stdout.split('\n')
        self.assertEqual(json.loads(out[0]), {'todo': False, 'inprogress': True, 'blocked': False, 'done': False})
        self.assertEqual(out[1], str(db.SCHEMA_VERSION))


class PageTests(Base):
    def test_pages_render_and_settings_is_a_page_not_a_modal(self):
        for url in ('/', '/list', '/sprints', '/settings'):
            r = self.c.get(url)
            self.assertEqual(r.status_code, 200, url)
            self.assertIn(b'href="/settings"', r.data if url != '/settings' else b'href="/settings"')
        page = self.c.get('/settings').data
        self.assertIn(b'settings.js', page)
        self.assertIn(b'data-tab="projects"', page)
        self.assertIn(b'id="projectModal"', page)          # projects are added and edited in one modal
        self.assertIn(b'colors.js', page)
        self.assertNotIn(b'settingsModal', self.c.get('/static/app.js').data)

    def test_board_has_per_card_expand_toggle_and_header_icon(self):
        js = self.c.get('/static/app.js').data
        for needle in (b'class="card-toggle"', b'flippedCards', b'flippedCards.clear()', b'setTruncateBtnLabel'):
            self.assertIn(needle, js)
        self.assertIn(b'.card-toggle', self.c.get('/static/style.css').data)
        self.assertIn(b'id="truncateBtn"', self.c.get('/').data)

    def test_sprints_page_has_the_backlog_add_card_dialog(self):
        page = self.c.get('/sprints').data
        for ident in (b'id="cardDialog"', b'id="ncTitle"', b'id="ncProject"', b'id="ncAssignee"', b'id="ncDescription"'):
            self.assertIn(ident, page)
        js = self.c.get('/static/sprints.js').data
        self.assertIn(b"openCardDialog('top')", js)
        self.assertIn(b"openCardDialog('bottom')", js)


if __name__ == '__main__':
    unittest.main()
