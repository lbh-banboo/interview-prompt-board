"""声音登记 HTTP 接口（M03-3）。register(app, ctx)；声纹模板只存本机，响应中不含 embedding。

ctx.config 可选键：voice_extractor（注入提取器，测试用）、voice_model（模型路径）。
"""
import base64
import binascii
import io
import wave

from fastapi import HTTPException, Request
from pydantic import BaseModel, Field

from ..audio.enroll import ENROLL_TEXT, MESSAGES, Reason, VoiceProfileStore, check_enrollment
from ..audio.voiceprint import DEFAULT_MODEL, SAMPLE_RATE, load_extractor

PROFILE = 'default'


class EnrollBody(BaseModel):
    segments: list[str] = Field(max_length=10)  # 每段 base64 的 16kHz 单声道 PCM16 或 WAV


def decode_segment(b64: str) -> bytes:
    try:
        raw = base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError('音频分段不是有效的 base64')
    if raw[:4] != b'RIFF':
        return raw[:len(raw) // 2 * 2]
    try:
        with wave.open(io.BytesIO(raw)) as w:
            if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != (SAMPLE_RATE, 1, 2):
                raise ValueError('WAV 必须是 16kHz 单声道 16bit')
            return w.readframes(w.getnframes())
    except (wave.Error, EOFError):
        raise ValueError('WAV 文件无法解析')


def register(app, ctx):
    store = VoiceProfileStore(ctx.store.connect, ctx.store.lock, ctx.root)
    state = {}

    def guard(request: Request):
        if request.headers.get('x-local-token') != ctx.token:
            raise HTTPException(403, '本地会话令牌无效，请刷新页面')

    def extractor():
        injected = ctx.config.get('voice_extractor') if ctx.config else None
        if injected is not None:
            return injected
        if 'ex' not in state:
            state['ex'] = load_extractor(ctx.config.get('voice_model', DEFAULT_MODEL) if ctx.config else DEFAULT_MODEL)
        return state['ex']

    @app.get('/api/voice/profile')
    def profile_status(request: Request):
        guard(request)
        row = store.load(PROFILE)
        return dict(enrolled=row is not None, quality=row[1] if row else None, created_at=row[2] if row else None,
                    model_available=extractor() is not None, enroll_text=ENROLL_TEXT)

    @app.post('/api/voice/enroll')
    def enroll(body: EnrollBody, request: Request):
        guard(request)
        ex = extractor()
        if ex is None:
            raise HTTPException(503, MESSAGES[Reason.MODEL_UNAVAILABLE])
        result = check_enrollment([decode_segment(s) for s in body.segments], ex)
        if result.ok:
            store.save(PROFILE, result.template, result.quality)
        return result.public()

    @app.delete('/api/voice/profile')
    def delete_profile(request: Request):
        guard(request)
        return dict(deleted=store.delete(PROFILE))
