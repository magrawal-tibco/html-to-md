import re, sys
sys.stdout.reconfigure(encoding='utf-8')

md = open('output/pub/ebx/6.2.3/doc/html/en/user_data/userdata_viewing.md', encoding='utf-8').read()
fm_end = md.find('\n---\n', 3)
body = md[fm_end+5:] if fm_end != -1 else md

md_re = re.compile(r'!\[[^\]]*\]\([^)]+\)')
md_imgs = md_re.findall(body)
print(f'Compare regex found: {len(md_imgs)}')
for m in md_imgs:
    print(f'  {m[:80]}')

html_imgs = re.findall(r'<img[^>]+src=["\']([^"\']+)["\']', body, re.IGNORECASE)
print(f'\nRaw <img> tags in body: {len(html_imgs)}')
for m in html_imgs:
    print(f'  {m}')
