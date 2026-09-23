"""Isolated language/Unicode checks. Never accesses real Notes or user data."""
import copy
import importlib.util
import io
from email.message import Message
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import locale_support as locale
import server


class LocaleTests(unittest.TestCase):
    def setUp(self):
        locale.system_locale.cache_clear()
        self.language_token = locale.REQUEST_LANGUAGE.set("en")
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        paths = {"LIB_DIR": root, "DATA_FILE": root / "库.json",
                 "BACKUP_FILE": root / "库.backup.json", "MIRROR_DIR": root / "txt镜像",
                 "HISTORY_DIR": root / "Save History", "SNAP_DIR": root / "快照", "MOVELOG_DIR": root / "移动记录",
                 "RESTORE_FILE": root / "归位表.json"}
        self.path_patch = patch.multiple(server, **{k: str(v) for k, v in paths.items()})
        self.path_patch.start()

    def tearDown(self):
        self.path_patch.stop()
        self.temp.cleanup()
        locale.REQUEST_LANGUAGE.reset(self.language_token)
        locale.system_locale.cache_clear()

    def handler(self, path):
        h = object.__new__(server.Handler)
        h.path = path
        h.headers = Message()
        h.headers['Host'] = f'localhost:{server.PORT}'
        h.wfile = io.BytesIO()
        h.send_response = lambda status: None
        h.send_header = lambda key, value: None
        h.end_headers = lambda: None
        return h

    def response(self, h):
        return json.loads(h.wfile.getvalue().decode("utf-8"))

    def test_system_language_order_cache_and_environment_fallback(self):
        preferences = {"AppleLanguages": ["zh-Hant-TW", "en-US"],
                       "UnrelatedPreference": "must not be exposed"}
        result = subprocess.CompletedProcess([], 0, plistlib.dumps(preferences), b"")
        with patch.object(locale.subprocess, "run", return_value=result) as run:
            self.assertEqual(locale.system_locale(), {"language": "zh", "locale": "zh-Hant-TW", "source": "system"})
            self.assertEqual(locale.system_locale()["language"], "zh")
            self.assertEqual(run.call_count, 1)
        locale.system_locale.cache_clear()
        result.stdout = plistlib.dumps({"AppleLanguages": ["fr-FR", "zh-Hans"]})
        with patch.object(locale.subprocess, "run", return_value=result):
            self.assertEqual(locale.system_locale(), {"language": "en", "locale": "fr-FR", "source": "system"})
        locale.system_locale.cache_clear()
        with patch.object(locale.subprocess, "run", side_effect=OSError("unavailable")), \
                patch.dict(os.environ, {"LC_ALL": "", "LC_MESSAGES": "", "LANG": "en_US.UTF-8"}, clear=True):
            self.assertEqual(locale.system_locale(), {"language": "en", "locale": "en_US", "source": "environment"})

    def test_locale_api_and_explicit_language_override(self):
        expected = {"language": "zh", "locale": "zh-Hans-CN", "source": "system"}
        with patch.object(server, "system_locale", return_value=expected):
            h = self.handler("/api/locale")
            h.do_GET()
            self.assertEqual(self.response(h), expected)
        with patch.object(locale, "system_locale", return_value=expected):
            self.assertEqual(locale.request_language("en"), "en")
            self.assertEqual(locale.request_language("zh-CN"), "zh")
            self.assertEqual(locale.request_language(None), "zh")

    def test_unicode_storage_mirror_and_html(self):
        title = 'Café 中文 العربية 👩🏽‍💻\nA/B:<draft>'
        body = 'Latin e\u0301\n中文內容\nالعربية مرحبا\n👩🏽‍💻 & <literal>\n'
        project = '项目 العربية 🚀'
        data = {"rev": 0, "projects": [project], "prompts": [{"id": "TEST_UNICODE", "title": title,
                "project": project, "active": 1, "versions": [{"v": 1, "content": body}]}]}
        untouched = copy.deepcopy(data)
        server.save_data(data)
        self.assertEqual(server.load_data(), dict(untouched, rev=1))
        self.assertEqual(data, untouched)
        relative = server.mirror_plan(data)[("TEST_UNICODE", 1)]
        mirror = Path(server.MIRROR_DIR) / relative
        self.assertEqual(mirror.read_text(encoding="utf-8"), body)
        self.assertIn('Café 中文 العربية 👩🏽‍💻', mirror.name)
        self.assertNotIn('\n', mirror.name)
        self.assertNotIn('/', mirror.name)
        html = server.note_html('عنوان 中文 & <title>', body)
        self.assertIn('عنوان 中文 &amp; &lt;title&gt;', html)
        self.assertIn('العربية مرحبا', html)
        self.assertIn('👩🏽‍💻 &amp; &lt;literal&gt;', html)
        self.assertTrue(html.endswith('<div><br></div>'))

    def test_folder_metadata_uses_actual_name_and_filters_supported_trash_names(self):
        for default in ['Notes', '备忘录', '備忘錄', 'ملاحظات']:
            with self.subTest(default=default):
                output = server.US.join([default, '中文 project 👋', 'Recently Deleted', '最近删除', '最近刪除'])
                output += server.RS + default + server.US + 'folder-id'
                with patch.object(server, 'run_osascript', return_value=(output, None)):
                    h = self.handler('/api/notes/folders'); h.do_GET()
                self.assertEqual(self.response(h), {"folders": [default, '中文 project 👋'],
                                                   "defaultFolder": default, "defaultFolderId": "folder-id"})
        with patch.object(server, 'run_osascript', return_value=('Project' + server.RS + server.US, None)):
            h = self.handler('/api/notes/folders'); h.do_GET()
        self.assertEqual(self.response(h), {"folders": ['Project'], 'defaultFolder': '', 'defaultFolderId': ''})

    def test_emoji_names_keep_their_own_spelling_and_resolve_duplicates(self):
        project = '🚀' * 80
        title = '👋' * 80
        data = {'projects': [project], 'prompts': [
            {'id': 'TEST_' + str(n), 'project': project, 'title': title,
             'active': 1, 'versions': [{'v': 1, 'content': '中文 العربية 👋'}]}
            for n in range(2)]}
        plan = server.mirror_plan(data)
        self.assertEqual(len(set(plan.values())), 2)
        self.assertEqual(plan[('TEST_0', 1)], project + '/' + title + '_v1_active.txt')
        self.assertEqual(plan[('TEST_1', 1)], project + '/' + title + '_2_v1_active.txt')
        self.assertEqual(server.safe_name('既有标题'), '既有标题')

    def test_osascript_unicode_decoding_and_numeric_dates(self):
        expected = 'Café\n中文\nالعربية 👩🏽‍💻'
        result = subprocess.CompletedProcess([], 0, expected + '\n', '')
        with patch.object(server.subprocess, 'run', return_value=result) as run:
            self.assertEqual(server.run_osascript('mock', ['العربية 👋']), (expected, None))
            self.assertEqual(run.call_args.kwargs['encoding'], 'utf-8')
            self.assertEqual(run.call_args.args[0][-1], 'العربية 👋')
        self.assertEqual(server.norm_date('2026-9-2-8-3'), '2026-09-02 08:03')

    def test_chinese_errors_and_prompts_preserve_user_text(self):
        locale.REQUEST_LANGUAGE.set('zh')
        original = {'error': 'Save failed: ملف 中文 👋', 'title': 'Save failed: my own note'}
        h = self.handler('/unused'); h.send_json(original, 500)
        self.assertEqual(self.response(h), {'error': '保存失败：ملف 中文 👋', 'title': original['title']})
        self.assertEqual(original['error'], 'Save failed: ملف 中文 👋')
        self.assertIn('没有权限读取备忘录', locale.translate('Permission to read Apple Notes was denied. Open System Settings → Privacy & Security → Automation, allow Terminal to control Notes, then try again.'))

    def test_generated_report_localization_does_not_translate_entries(self):
        locale.REQUEST_LANGUAGE.set('zh')
        log = server.write_move_log('My own label 中文', [{'笔记标题': 'مرحبا 👋',
                                                        '原文件夹': 'Original', '目标文件夹': '项目'}])
        text = Path(log).read_text(encoding='utf-8')
        self.assertIn('# 备忘录移动记录', text)
        self.assertIn('My own label 中文', text)
        self.assertIn('| مرحبا 👋 | Original | 项目 |', text)
        server.save_data({'rev': 0, 'projects': [], 'prompts': []})
        self.assertIn('请在整理器网页中编辑内容', (Path(server.MIRROR_DIR) / 'README.txt').read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
