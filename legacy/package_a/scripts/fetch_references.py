#!/usr/bin/env python3
"""Download public official PDFs to YOUR computer; no third-party manuals redistributed.
Run from anywhere: python scripts/fetch_references.py
Failures are reported individually; files are never silently declared successful.
"""
from pathlib import Path
import json, urllib.request, sys
ROOT=Path(__file__).resolve().parents[1]
out=ROOT/'references/downloaded';out.mkdir(exist_ok=True)
sources=json.loads((ROOT/'references/sources.json').read_text(encoding='utf-8'))
failed=[]
for s in sources:
    if 'download_filename' not in s:continue
    try:
        request=urllib.request.Request(s['url'],headers={'User-Agent':'Mozilla/5.0'})
        with urllib.request.urlopen(request,timeout=45) as r:content=r.read(30_000_000)
        if not content.startswith(b'%PDF'):raise ValueError('Response is not a PDF; use the official link manually')
        dest=out/s['download_filename'];dest.write_bytes(content)
        print(f"OK {s['id']}: {dest.name} ({len(content)} bytes)")
    except Exception as e:
        failed.append(s['id']);print(f"FAILED {s['id']}: {e}\nOfficial source: {s['url']}",file=sys.stderr)
print('Manufacturer copyright and licence terms remain applicable.')
raise SystemExit(1 if failed else 0)
