"""API tests: global card order, hidden columns, paging, MCP tools.

Run from the perkan directory:  python -m unittest discover -s tests -v
Uses a throwaway data directory; never touches ./data.
"""
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


class OrderingTests(Base):
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


class HistoryTests(Base):
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


if __name__ == '__main__':
    unittest.main()
