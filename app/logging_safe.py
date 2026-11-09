"""M16-2 日志脱敏：Key 样式、手机号、邮箱、自定义词表；不记录音频内容；日志目录 700、按天滚动。"""
import logging
import os
import re
import traceback
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

_KEYNAME = r'(?:api[_-]?key|access[_-]?key|secret|token|password|dashscope|deepseek)'
PATTERNS = [
    (re.compile(r'sk-[A-Za-z0-9_\-]{8,}'), 'sk-***'),
    (re.compile(r'(?i)\bBearer\s+[A-Za-z0-9._~+/\-]+=*'), 'Bearer ***'),
    (re.compile(r'(?i)(["\']?[\w\-]*' + _KEYNAME + r'[\w\-]*["\']?\s*[=:]\s*)(["\']?)[^\s"\',}&]+'), r'\1\2***'),
    (re.compile(r'(?<!\d)1[3-9]\d{9}(?!\d)'), '[手机号]'),
    (re.compile(r'[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+'), '[邮箱]'),
]


def redact(text, words=()):
    for rx, repl in PATTERNS:
        text = rx.sub(repl, text)
    for w in words:
        if w:
            text = re.sub(re.escape(w), '***', text, flags=re.I)
    return text


def _arg(a):
    if isinstance(a, (bytes, bytearray, memoryview)):
        return f'<二进制 {len(a)} 字节，已省略>'
    return a


class RedactFilter(logging.Filter):
    def __init__(self, words=()):
        super().__init__()
        self.words = tuple(words)

    def filter(self, record):
        if record.args:
            args = record.args if isinstance(record.args, tuple) else (record.args,)
            record.args = tuple(_arg(a) for a in args)
        record.msg = redact(record.getMessage(), self.words)
        record.args = None
        if record.exc_info:
            text = ''.join(traceback.format_exception(*record.exc_info))
            record.exc_text = redact(text, self.words)
            record.exc_info = None
        return True


_TOKEN_QUERY = re.compile(r'([?&]token=)[^&\s"\']+')


class TokenQueryFilter(logging.Filter):
    """uvicorn 会把 WebSocket 地址（含 ?token=）原样写进自己的日志。只改参数里的查询串，保持 args 结构
    （access 日志的格式化器要按位置解包 args，不能像 RedactFilter 那样清空）。"""

    def filter(self, record):
        if isinstance(record.args, tuple):
            record.args = tuple(_TOKEN_QUERY.sub(r'\1***', a) if isinstance(a, str) else a for a in record.args)
        elif isinstance(record.msg, str):
            record.msg = _TOKEN_QUERY.sub(r'\1***', record.msg)
        return True


def mask_uvicorn_tokens():
    """给 uvicorn.error / uvicorn.access 挂上 TokenQueryFilter；重复调用不叠加。"""
    for name in ('uvicorn.error', 'uvicorn.access'):
        lg = logging.getLogger(name)
        if not any(isinstance(f, TokenQueryFilter) for f in lg.filters):
            lg.addFilter(TokenQueryFilter())


def install(log_dir, words=(), logger=None, keep_days=14):
    """data/logs/app.log 按天滚动；目录 700、文件 600。返回 handler 便于测试移除。"""
    d = Path(log_dir)
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(d, 0o700)
    handler = TimedRotatingFileHandler(d / 'app.log', when='midnight', backupCount=keep_days, encoding='utf-8')
    os.chmod(d / 'app.log', 0o600)
    handler.addFilter(RedactFilter(words))
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s %(message)s'))
    (logger or logging.getLogger()).addHandler(handler)
    return handler
