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
            {'id': 'done', 'title': 'Done', 'cards': []},
            {'id': 'archive', 'title': 'Archive', 'hidden': True, 'cards': [card('h1', 6e6), card('h2', 7e6)]},
        ],
        'projects': [],
    }


class Base(unittest.TestCase):
    def setUp(self):
        with db.write() as conn:
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
        self.assertEqual(out[2], '2')


if __name__ == '__main__':
    unittest.main()
