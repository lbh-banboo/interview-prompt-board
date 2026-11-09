"""M12-3 zip 打包：Markdown + 各路 WAV + metrics CSV + manifest；WAV 流式分块写入。"""
import csv
import io
import json
import zipfile

from .export import export_markdown, recording_files

CHUNK = 1 << 20


def write_bundle(store, session, fileobj, root=None, answerer_transcribed=None):
    """写入 fileobj（可不可 seek）；缺失/损坏录音不入包，只在 manifest 记状态。
    answerer_transcribed 与服务端单文件导出一致：决定“回答者未转写”说明是否写入 transcript.md。"""
    files = []
    with zipfile.ZipFile(fileobj, 'w', zipfile.ZIP_DEFLATED) as zf:
        md = export_markdown(store, session, root, answerer_transcribed).encode()
        zf.writestr('transcript.md', md)
        files.append(dict(name='transcript.md', status='ok', size=len(md)))

        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(['session_id', 'turn_id', 'name', 't_ns', 'value'])
        for m in store.main_rows('SELECT * FROM metrics WHERE session_id=? ORDER BY t_ns, rowid', (session,)):
            w.writerow([m['session_id'], m['turn_id'], m['name'], m['t_ns'], m['value']])
        zf.writestr('metrics.csv', buf.getvalue().encode())
        files.append(dict(name='metrics.csv', status='ok', size=len(buf.getvalue().encode())))

        for r in recording_files(store, session, root):
            entry = dict(name=f"recordings/{r['name']}", source=r['source'], sequence=r['sequence'], status=r['status'])
            if r['status'] == 'ok':
                size = 0
                with open(r['path'], 'rb') as src, zf.open(entry['name'], 'w', force_zip64=True) as dst:
                    while chunk := src.read(CHUNK):
                        dst.write(chunk)
                        size += len(chunk)
                entry['size'] = size
            files.append(entry)
        manifest = dict(session_id=session, files=files)
        zf.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2))
    return manifest
