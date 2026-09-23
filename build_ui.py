#!/usr/bin/env python3
"""Build both language pages from one UI template and the shared locale table.
Run: python3 build_ui.py. --check verifies generated pages without writing them.
The table stores literal values, JavaScript template fragments, and static HTML.
It never transforms library data, API field names, or legacy folder aliases.
"""
import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TOKEN = re.compile(r'@@(u[0-9a-f]{12})@@')


def render(template, catalog, language):
    def expand(match):
        entry = catalog[match.group(1)]
        value = entry[language]
        if not isinstance(value, str):
            raise ValueError(f'Missing {language} text for {match.group(1)}')
        if entry['kind'] == 'literal':
            return json.dumps(value, ensure_ascii=False).replace('</script', '<\\/script')
        if entry['kind'] == 'template' and ('${' in value or '`' in value):
            raise ValueError(f'Unexpected JavaScript in {match.group(1)}')
        return value
    html = TOKEN.sub(expand, template)
    html = re.sub(r'<html lang="[^"]+">', '<html lang="' + ('en' if language == 'en' else 'zh-CN') + '">', html, count=1)
    html = html.replace('/* UI_LOCALE */ "zh"', json.dumps(language))
    head, script = html.split("<script>", 1)
    return "\n".join(line.rstrip() for line in head.split("\n")) + "<script>" + script


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    template = (ROOT / 'ui.template.html').read_text(encoding='utf-8')
    catalog = json.loads((ROOT / 'ui-locales.json').read_text(encoding='utf-8'))
    for language, name in [('zh', 'app.zh-CN.html'), ('en', 'app.en.html')]:
        output = render(template, catalog, language)
        target = ROOT / name
        if args.check:
            if not target.exists() or target.read_text(encoding='utf-8') != output:
                raise SystemExit(f'{name} differs from shared template/catalog')
        else:
            target.write_text(output, encoding='utf-8')
    print('Both UI pages match the shared template/catalog.' if args.check else 'Built both UI pages.')


if __name__ == '__main__':
    main()
