"""Regression checks for ported settings and mirrors, with isolated storage."""
import copy
from email.message import Message
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import jev_classify as jev
import locale_support as locale
import server


class SensitiveScopeTests(unittest.TestCase):
    def test_priority_and_independent_controls(self):
        conf = {"neverSend": ["Private"], "customSecrets": ["  confidential  "]}
        self.assertEqual(jev.looks_secret('password: confidential', 'Private', conf),
                         (True, 'Folder "Private" is on the never-send list'))
        self.assertEqual(jev.looks_secret('password: CONFIDENTIAL', '', conf),
                         (True, 'Contains your own term "confidential"'))
        conf['useBuiltinSecrets'] = False
        self.assertEqual(jev.looks_secret('password: plain', '', conf), (False, ''))
        self.assertTrue(jev.looks_secret('CONFIDENTIAL', '', conf)[0])
        self.assertTrue(jev.looks_secret('', 'Private', conf)[0])
        self.assertFalse(jev.looks_secret('ordinary note', '', {'customSecrets': [' ', '']} )[0])

    def test_custom_terms_are_literal_substrings(self):
        conf = {'useBuiltinSecrets': False, 'customSecrets': ['[a.b]']}
        self.assertTrue(jev.looks_secret('prefix [A.B] suffix', conf=conf)[0])
        self.assertFalse(jev.looks_secret('aXb', conf=conf)[0])

    def test_never_send_folders_are_not_read_and_bodies_keep_their_ids(self):
        with tempfile.TemporaryDirectory() as lib:
            jev.save_conf(lib, {'neverSend': ['Private'], 'sendSensitive': True,
                               'useBuiltinSecrets': False, 'keywords': {'Work': ['meeting']}})
            items = [{'id': 'private', 'name': 'Secret', 'folder': 'Private'},
                     {'id': 'public', 'name': 'Schedule', 'folder': 'Notes'}]
            with patch.object(jev, 'call_jev', side_effect=AssertionError('network called')), \
                    patch.dict(os.environ, {}, clear=True):
                def fetch(ids):
                    self.assertEqual(ids, ['public'])
                    return ['meeting tomorrow'], None
                rows, error = jev.classify(lib, items, [('Work', '')], fetch)
            self.assertIsNone(error)
            self.assertEqual(rows[0]['body'], '')
            self.assertEqual(rows[0]['verdict'], 'skipped')
            self.assertEqual(rows[1]['body'], 'meeting tomorrow')
            self.assertEqual(rows[1]['project'], 'Work')

    def test_entirely_excluded_batch_never_fetches_bodies(self):
        with tempfile.TemporaryDirectory() as lib:
            jev.save_conf(lib, {'neverSend': ['Private']})
            def fetch(ids):
                self.fail('excluded folder body was read')
            rows, _ = jev.classify(lib, [{'id': 'x', 'folder': 'Private'}], [('Work', '')], fetch)
            self.assertEqual(rows[0]['verdict'], 'skipped')

    def test_block_reasons_are_localized_without_translating_user_terms(self):
        with tempfile.TemporaryDirectory() as lib:
            jev.save_conf(lib, {'customSecrets': ['PRIVATE العربية']})
            rows, _ = jev.classify(lib, [{'id': 'x'}], [('Work', '')],
                                   lambda ids: (['private العربية'], None), 'zh')
            self.assertIn('含有自定义词「PRIVATE العربية」', rows[0]['error'])
            self.assertIn('已在本机拦截', rows[0]['error'])
            self.assertEqual(locale.translate('GitHub tokens', 'zh'), 'GitHub 密钥')


class ServerPortTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.paths = patch.multiple(server, LIB_DIR=str(root), DATA_FILE=str(root/'library.json'),
                                    BACKUP_FILE=str(root/'library.backup.json'),
                                    MIRROR_DIR=str(root/'Text Mirror'), SNAP_DIR=str(root/'Snapshots'),
                                    HISTORY_DIR=str(root/'Save History'))
        self.paths.start()
        self.lang = locale.REQUEST_LANGUAGE.set('en')

    def tearDown(self):
        locale.REQUEST_LANGUAGE.reset(self.lang)
        self.paths.stop()
        self.tmp.cleanup()

    def request(self, path, body=None, headers=None):
        h = object.__new__(server.Handler)
        h.path = path
        h.headers = Message()
        values = {'Host': f'localhost:{server.PORT}'}
        if body is not None:
            values.update({'Origin': f'http://localhost:{server.PORT}',
                           'X-Session-Token': server.SESSION_TOKEN})
        values.update(headers or {})
        for key, value in values.items():
            if value is not None:
                h.headers[key] = value
        h.wfile = io.BytesIO()
        h.send_response = lambda status: setattr(h, 'status', status)
        h.send_header = lambda *args: None
        h.end_headers = lambda: None
        h.send_error = lambda status, *args: setattr(h, 'status', status)
        if path in ('/api/similar/merge', '/api/similar/undo'):
            body = dict(body, rev=server.load_data()['rev'])
        h.read_body = lambda: body
        (h.do_GET if body is None else h.do_POST)()
        return h.status, json.loads(h.wfile.getvalue()) if h.wfile.getvalue() else None

    def test_local_host_origin_token_and_static_boundary(self):
        self.assertEqual(self.request('/Notes%20Library/library.json')[0], 404)
        self.assertEqual(self.request('/jev-config.json')[0], 404)
        self.assertEqual(self.request('/api/data', headers={'Host': 'attacker.example'})[0], 403)
        self.assertEqual(self.request('/api/session/token')[1]['token'], server.SESSION_TOKEN)
        for headers in ({'Host': 'attacker.example'}, {'Origin': 'https://attacker.example'},
                        {'Origin': None}, {'X-Session-Token': 'wrong'}):
            with patch.object(server, 'save_data') as save:
                self.assertEqual(self.request('/api/save', {'rev': 0}, headers)[0], 403)
                save.assert_not_called()
        with patch.object(server, 'save_data', return_value={'ok': True, 'rev': 1}):
            self.assertEqual(self.request('/api/save', {'rev': 0})[0], 200)
        with patch.object(server, 'move_notes', return_value=({'moved': 1, 'failed': 0}, None)):
            self.assertEqual(self.request('/api/notes/move', {'moves': []})[0], 200)

    def test_body_read_failure_is_per_note_and_never_becomes_empty_success(self):
        output = ('1' + server.US + 'first body' + server.RS +
                  '0' + server.US + 'Notes permission failed' + server.RS)
        with patch.object(server, 'run_osascript', return_value=(output, None)):
            status, result = self.request('/api/notes/body', {'ids': ['TEST_OK', 'TEST_FAIL']})
        self.assertEqual(status, 200)
        self.assertEqual(result['results'], [
            {'id': 'TEST_OK', 'ok': True, 'body': 'first body'},
            {'id': 'TEST_FAIL', 'ok': False, 'error': 'Notes permission failed'}])
        with patch.object(server, 'run_osascript', return_value=('1' + server.US + '' + server.RS, None)):
            self.assertEqual(self.request('/api/notes/body', {'ids': ['TEST_EMPTY']})[1]['results'][0],
                             {'id': 'TEST_EMPTY', 'ok': True, 'body': ''})
        with patch.object(server, 'run_osascript', return_value=('1' + server.US + 'body', None)):
            self.assertEqual(self.request('/api/notes/body', {'ids': ['TEST_TRUNCATED']})[0], 502)

    def test_partial_config_update_preserves_user_rules_key_and_thresholds(self):
        initial = {'apiKey': 'test-placeholder', 'keywords': {'Work': ['meeting']},
                   'neverSend': ['Private'], 'customSecrets': ['INTERNAL'],
                   'useBuiltinSecrets': False, 'sendSensitive': True, 'autoThreshold': .92}
        jev.save_conf(server.LIB_DIR, initial)
        self.assertEqual(self.request('/api/jev/config', {'descriptions': {'Work': 'Meeting notes'}})[0], 200)
        conf = jev.load_conf(server.LIB_DIR)
        for key, value in initial.items():
            self.assertEqual(conf[key], value, key)
        status, public = self.request('/api/jev/config')
        self.assertEqual(status, 200)
        self.assertNotIn('apiKey', public)
        self.assertNotIn('test-placeholder', json.dumps(public))
        self.assertEqual(public['customSecrets'], ['INTERNAL'])
        self.assertEqual(public['neverSend'], ['Private'])
        self.assertEqual(public['builtinLabels'], [label for label, _ in jev.BUILTIN_SECRETS])
        self.assertTrue(public['sendSensitive'])
        self.assertFalse(public['useBuiltinSecrets'])
        self.request('/api/jev/config', {'customSecrets': [], 'neverSend': [], 'useBuiltinSecrets': True})
        conf = jev.load_conf(server.LIB_DIR)
        self.assertEqual(conf['customSecrets'], [])
        self.assertEqual(conf['neverSend'], [])
        self.assertTrue(conf['useBuiltinSecrets'])
        self.assertEqual(conf['keywords'], initial['keywords'])

    def test_mirror_extension_round_trip_has_no_leftovers_and_preserves_bytes(self):
        body = '# Heading\n\n**bold**\n中文 العربية 👋\n\n  trailing spaces  \n'
        data = {'projects': ['Work'], 'prompts': [
            {'id': f'TEST_{i}', 'title': 'Same/title', 'project': 'Work', 'active': 2,
             'versions': [{'v': 1, 'content': 'old\n'}, {'v': 2, 'content': body}]}
            for i in range(2)]}
        for ext in ['.txt', '.md', '.txt']:
            data['mirrorExt'] = ext
            data['rev'] = server.load_data()['rev']
            data['rev'] = server.save_data(data)['rev']
            plan = server.mirror_plan(data)
            files = list(Path(server.MIRROR_DIR).rglob('*'))
            self.assertEqual(len([p for p in files if p.is_file()]), 5)
            self.assertTrue(all(p.suffix == ext for p in files if p.is_file()))
            for entry in data['prompts']:
                for v in entry['versions']:
                    path = Path(server.MIRROR_DIR)/plan[(entry['id'], v['v'])]
                    self.assertEqual(path.read_bytes(), v['content'].encode())
            self.assertEqual(server.load_data(), data)
        self.assertEqual(server.mirror_ext({'mirrorExt': '../invalid'}), '.txt')
        self.assertEqual(server.mirror_ext(None), '.txt')

    def test_similar_http_merge_and_undo_reload_disk_and_restore_content(self):
        data = {'projects': ['Work'], 'importedNoteIds': ['n1', 'n2'], 'prompts': [
            {'id': f'TEST_{i}', 'title': 'Checklist', 'project': 'Work', 'active': 1,
             'srcNoteId': f'n{i}', 'noteDate': f'2026-09-0{i} 12:00',
             'versions': [{'v': 1, 'content': 'Buy milk'}]} for i in (1, 2)]}
        data['rev'] = server.load_data()['rev']
        data['rev'] = server.save_data(data)['rev']
        status, scanned = self.request('/api/similar/scan', {})
        self.assertEqual(status, 200)
        self.assertEqual(len(scanned['groups']), 1)
        groups = [[m['id'] for m in g['members']] for g in scanned['groups']]
        status, merged = self.request('/api/similar/merge', {'groups': groups})
        self.assertEqual(status, 200)
        self.assertEqual(merged['removed'], 1)
        self.assertEqual(len(server.load_data()['prompts']), 1)
        self.assertEqual(self.request('/api/similar/scan', {})[1]['lastMerge'], {'groups': 1, 'notes': 2})
        self.assertEqual(self.request('/api/similar/undo', {})[0], 200)
        self.assertIsNone(self.request('/api/similar/scan', {})[1]['lastMerge'])
        restored = server.load_data()
        self.assertEqual(restored, dict(data, rev=data['rev'] + 2))
        locale.REQUEST_LANGUAGE.set('zh')
        status, result = self.request('/api/similar/undo', {})
        self.assertEqual(status, 400)
        self.assertEqual(result['error'], '没有可撤销的合并记录。')
        self.assertEqual(self.request('/api/similar/merge', {'groups': []})[0], 400)
        self.assertEqual(self.request('/api/similar/merge', {'groups': [['gone', 'missing']]})[0], 400)

    def test_failed_merge_and_undo_keep_the_previous_record_for_retry(self):
        import similar_groups
        data = {'projects': [], 'prompts': [
            {'id': str(i), 'title': 'Checklist', 'active': 1,
             'versions': [{'v': 1, 'content': 'buy milk'}]} for i in range(4)]}
        data['rev'] = server.load_data()['rev']
        data['rev'] = server.save_data(data)['rev']
        self.request('/api/similar/merge', {'groups': [['0', '1']]})
        record_path = Path(similar_groups.record_path(server.LIB_DIR))
        original_record = record_path.read_bytes()
        original_data = server.load_data()
        for endpoint, body in [('/api/similar/merge', {'groups': [['2', '3']]}),
                               ('/api/similar/undo', {})]:
            with patch.object(server, 'save_data', side_effect=OSError('disk unavailable')):
                status, result = self.request(endpoint, body)
            self.assertEqual(status, 500)
            self.assertIn('Save failed', result['error'])
            self.assertEqual(server.load_data(), original_data)
            self.assertEqual(record_path.read_bytes(), original_record)
        self.assertEqual(self.request('/api/similar/undo', {})[0], 200)
        self.assertEqual({p['id'] for p in server.load_data()['prompts']}, {'0', '1', '2', '3'})

    def test_mirror_failure_after_library_commit_does_not_restore_stale_log(self):
        import similar_groups
        data = {'projects': [], 'prompts': [
            {'id': str(i), 'title': 'Checklist', 'active': 1,
             'versions': [{'v': 1, 'content': 'buy milk'}]} for i in range(2)]}
        data['rev'] = server.load_data()['rev']
        data['rev'] = server.save_data(data)['rev']
        with patch.object(server, 'rebuild_mirror', side_effect=OSError('mirror unavailable')):
            status, result = self.request('/api/similar/merge', {'groups': [['0', '1']]})
            self.assertEqual(status, 200)
            self.assertIn('warning', result)
        self.assertEqual(len(server.load_data()['prompts']), 1)
        self.assertTrue(Path(similar_groups.record_path(server.LIB_DIR)).exists())
        with patch.object(server, 'rebuild_mirror', side_effect=OSError('mirror unavailable')):
            status, result = self.request('/api/similar/undo', {})
            self.assertEqual(status, 200)
            self.assertIn('warning', result)
        self.assertEqual(len(server.load_data()['prompts']), 2)
        self.assertFalse(Path(similar_groups.record_path(server.LIB_DIR)).exists())

    def test_folder_info_keeps_metadata_without_review_instructions(self):
        server.save_data({'rev': 0, 'projects': ['Work'], 'prompts': []})
        status, info = self.request('/api/diag?project=Work')
        self.assertEqual(status, 200)
        self.assertNotIn('ask', info)
        self.assertEqual(info['folder'], server.project_dir('Work'))


class PingWatchdogTests(unittest.TestCase):
    def test_first_launch_and_closed_window_grace(self):
        self.assertFalse(server.ping_expired(100, 190, None))
        self.assertTrue(server.ping_expired(100, 190.01, None))
        self.assertFalse(server.ping_expired(100, 112, 100))
        self.assertTrue(server.ping_expired(100, 112.01, 100))
        # A fresh ping after a page reload resets the short grace period.
        self.assertFalse(server.ping_expired(100, 113, 110))

    def test_ping_endpoint_updates_last_contact(self):
        handler = object.__new__(server.Handler)
        handler.path = '/api/ping'
        handler.headers = Message()
        handler.headers['Host'] = f'localhost:{server.PORT}'
        replies = []
        handler.send_json = replies.append
        with patch.object(server.time, 'monotonic', return_value=321), \
                patch.object(server, '_last_ping', None):
            handler.do_GET()
            self.assertEqual(server._last_ping, 321)
        self.assertEqual(replies, [{'ok': True}])


class SessionLogTests(unittest.TestCase):
    def test_session_report_counts_three_edits_two_categories_and_one_move(self):
        baseline = {'prompts': [
            {'id': f'TEST_{i}', 'title': f'Note {i}', 'project': 'Inbox',
             'versions': [{'v': 1, 'content': 'before'}]} for i in range(3)]}
        current = copy.deepcopy(baseline)
        for prompt in current['prompts']:
            prompt['versions'][0]['content'] = 'after'
        for prompt in current['prompts'][:2]:
            prompt['project'] = 'Work'
        moves = [{'noteId': 'N1', 'title': 'Note 0', 'from': 'Inbox', 'to': 'Work',
                  'accountId': 'A1', 'folderId': 'F2', 'time': '2026-09-23T12:00:00'}]
        groups = server.session_changes(baseline, current, moves)
        self.assertEqual({key: len(rows) for key, rows in groups.items()},
                         {'created': 0, 'body': 3, 'category': 2, 'deleted': 0, 'moved': 1})

    def test_session_baseline_and_committed_changes_are_written_to_txt(self):
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root)
            paths = {"LIB_DIR": root, "DATA_FILE": str(folder / 'library.json'),
                     "BACKUP_FILE": str(folder / 'library.backup.json'),
                     "MIRROR_DIR": str(folder / 'Text Mirror'),
                     "HISTORY_DIR": str(folder / 'Save History'),
                     "SNAP_DIR": str(folder / 'Snapshots'),
                     "MOVELOG_DIR": str(folder / 'Move Logs')}
            with patch.multiple(server, **paths), patch.object(server, 'SESSION_STATE', None), \
                    patch.object(server, 'system_locale', return_value={'language': 'en'}):
                server.start_session()
                initial = server.session_report()
                self.assertTrue(Path(initial['path']).is_file())
                server.save_data({'rev': 0, 'projects': ['Work'], 'prompts': [
                    {'id': 'TEST_1', 'title': 'Draft', 'project': 'Work', 'active': 1,
                     'versions': [{'v': 1, 'content': 'first'}]}]})
                server.record_session_moves([{'id': 'N1', '笔记标题': 'Draft',
                    '原文件夹': 'Inbox', '目标文件夹': 'Work', '原账户ID': 'A1'}],
                    {'N1': 'F2'}, 'Sync back to phone')
                report = server.session_report()
                self.assertEqual(len(report['groups']['created']), 1)
                self.assertEqual(len(report['groups']['moved']), 1)
                self.assertEqual(report['groups']['moved'][0]['accountId'], 'A1')
                text = Path(report['path']).read_text(encoding='utf-8')
                self.assertIn('Draft', text)
                self.assertIn('Inbox → Work', text)

    def test_manual_move_uses_selected_folder_id_in_source_account(self):
        note = {'id': 'N1', 'name': 'Draft', 'folder': 'Inbox', 'folderId': 'SOURCE',
                'accountId': 'A1', 'accountName': 'Main'}
        folders = [{'id': 'TARGET_A', 'name': 'Work', 'accountId': 'A1'},
                   {'id': 'TARGET_B', 'name': 'Work', 'accountId': 'B2'}]
        receipt = '1/0' + server.RS + 'M' + server.US + 'N1' + server.US + 'TARGET_A'
        with patch.object(server, 'list_notes', return_value=([note], None)), \
                patch.object(server, 'list_folder_details', return_value=(folders, None)), \
                patch.object(server, 'load_restore_book', return_value={'记录': []}), \
                patch.object(server, 'save_restore_book'), \
                patch.object(server, 'write_move_log', return_value='/tmp/move-log.md'), \
                patch.object(server, 'record_session_moves'), \
                patch.object(server, 'run_osascript', return_value=(receipt, None)) as script:
            result, error = server.move_notes([{'noteId': 'N1', 'folder': 'Work',
                'folderId': 'TARGET_A', 'accountId': 'A1'}], 'Manual move')
            self.assertIsNone(error)
            self.assertEqual(result['moved'], 1)
            self.assertEqual(script.call_args.args[1], ['Work', 'A1', 'TARGET_A', 'N1'])
            script.reset_mock()
            result, error = server.move_notes([{'noteId': 'N1', 'folder': 'Work',
                'folderId': 'TARGET_B', 'accountId': 'A1'}], 'Manual move')
            self.assertIsNone(error)
            self.assertEqual(result['failed'], 1)
            script.assert_not_called()


if __name__ == '__main__':
    unittest.main()
