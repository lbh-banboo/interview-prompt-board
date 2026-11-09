"""提问点校对台后端：列出标注文件、读条目、写回人工判定、带 Range 的音频。

判定语义只有一份：tools/import_review_md.mark_item；本模块只把按钮映射成四个勾选框。
"""
import json
import os
import threading
from pathlib import Path

from fastapi import Depends, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from tools.import_review_md import mark_item

ROOT = Path(__file__).resolve().parents[2]
SUFFIX = '.review.json'
VERDICTS = ('yes', 'no', 'followup', 'topic_change')
BOX = {'yes': '是问题结束点', 'no': '不是', 'followup': '追问', 'topic_change': '改题'}
_LOCK = threading.Lock()  # 同一文件的读-改-写串行化（路由是同步函数，跑在线程池）


class VerdictBody(BaseModel):
    verdict: str | None = None  # 只写备注时为 None，条目保持未判定
    note: str = Field(default='', max_length=1000)


def checkboxes(verdict):
    """按钮 -> 勾选框；勾选后的写入规则由 mark_item 决定。"""
    return {name: name == BOX[verdict] for name in BOX.values()}


def progress(items):
    judged = sum(bool(it.get('verified')) for it in items)  # 未判定不计入已判定数
    return dict(total=len(items), judged=judged, pct=round(judged * 100 / len(items)) if items else 0)


def register(app, ctx):
    ann_dir = Path((ctx.config or {}).get('annotations_dir') or ROOT / 'eval' / 'annotations')
    audio_dir = Path((ctx.config or {}).get('audio16k_dir') or ROOT / 'eval' / 'audio16k')

    def guard(x_local_token: str | None = Header(default=None)):
        if x_local_token != ctx.token:
            raise HTTPException(403, '本地会话令牌无效，请刷新页面')

    def audio_ids():
        return sorted(p.name[:-len(SUFFIX)] for p in ann_dir.glob('*' + SUFFIX))

    def review_path(audio_id):
        # 白名单：只接受目录里真实存在的 <audio_id>.review.json，天然挡住 ../ 与绝对路径
        if audio_id not in audio_ids():
            raise HTTPException(404, '标注文件不存在')
        return ann_dir / (audio_id + SUFFIX)

    def read(path):
        return json.loads(path.read_text(encoding='utf-8'))

    def write(path, doc):
        tmp = path.with_name(path.name + '.tmp')
        tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(tmp, path)  # 同目录原子替换，不留半截文件

    @app.get('/api/annotations')
    def list_annotations():
        rows = []
        for aid in audio_ids():
            try:
                doc = read(ann_dir / (aid + SUFFIX))
            except (OSError, ValueError):
                continue
            rows.append(dict(audio_id=aid, has_audio=(audio_dir / (aid + '.wav')).is_file(), **progress(doc['items'])))
        return rows

    @app.get('/api/annotations/{audio_id}')
    def get_annotation(audio_id: str):
        doc = read(review_path(audio_id))
        return dict(audio_id=audio_id, items=doc['items'], progress=progress(doc['items']))

    @app.put('/api/annotations/{audio_id}/items/{n}', dependencies=[Depends(guard)])
    def put_item(audio_id: str, n: int, body: VerdictBody):
        if body.verdict is not None and body.verdict not in VERDICTS:
            raise HTTPException(400, '未知判定')
        path = review_path(audio_id)
        with _LOCK:
            doc = read(path)
            item = next((it for it in doc['items'] if it.get('id') == n), None)
            if item is None:
                raise HTTPException(404, '条目不存在')
            if body.verdict is not None:
                mark_item(item, checkboxes(body.verdict))
            item.setdefault('human', {})['note'] = body.note
            write(path, doc)
        return dict(item=item, progress=progress(doc['items']))

    @app.get('/api/annotations/{audio_id}/audio')
    def audio(audio_id: str):
        review_path(audio_id)  # 白名单校验
        wav = audio_dir / (audio_id + '.wav')
        if not wav.is_file():
            raise HTTPException(404, '音频不存在')
        return FileResponse(wav, media_type='audio/wav', headers={'Accept-Ranges': 'bytes'})
