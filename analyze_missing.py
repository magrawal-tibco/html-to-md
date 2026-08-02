import json
import os

report_path = "C:/Users/Mayur.Agrawal/AppData/Local/Temp/ebx_compare.json"
data = json.loads(open(report_path, encoding='utf-8').read())
missing = [i for i in data['issues'] if i['check'] == 'missing_md']

by_lang = {}
for m in missing:
    path = m['html_file'].replace(os.sep, '/')
    parts = path.split('/')
    try:
        idx = parts.index('html')
        lang = parts[idx+1] if idx+1 < len(parts) else 'unknown'
    except ValueError:
        lang = 'unknown'
    by_lang[lang] = by_lang.get(lang, 0) + 1

print('Missing by language:')
for lang, count in sorted(by_lang.items(), key=lambda x: -x[1]):
    print(f'  {lang}: {count}')

sep = '/'
en_miss = [m['html_file'] for m in missing if sep + 'en' + sep in m['html_file'].replace(os.sep, sep)]
fr_miss = [m['html_file'] for m in missing if sep + 'fr' + sep in m['html_file'].replace(os.sep, sep)]
ja_miss = [m['html_file'] for m in missing if sep + 'ja' + sep in m['html_file'].replace(os.sep, sep)]

print(f'\nEN missing ({len(en_miss)}):')
for f in en_miss[:5]:
    print(' ', f)

print(f'\nJA missing ({len(ja_miss)}):')
for f in ja_miss[:5]:
    print(' ', f)

print(f'\nFR missing ({len(fr_miss)}) samples:')
for f in fr_miss[:5]:
    print(' ', f)

# Check if fr files exist in cache
print('\nChecking FR files exist in cache:')
for f in fr_miss[:5]:
    cache_path = 'cache/pub/' + f
    exists = os.path.exists(cache_path)
    print(f'  {"YES" if exists else "NO "}: {cache_path}')

# Check manifest coverage for FR
print('\nChecking manifest for FR entries:')
import glob
manifests = glob.glob('manifests/manifest_ebx*.json')
print(f'  Manifests found: {manifests}')
for mf in manifests[:2]:
    entries = json.loads(open(mf, encoding='utf-8').read())
    fr_entries = [e for e in entries if '/fr/' in e.get('url','')]
    print(f'  {mf}: {len(entries)} total, {len(fr_entries)} fr entries')
