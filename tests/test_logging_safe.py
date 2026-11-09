import logging
import stat

from app.logging_safe import RedactFilter, install, redact

KEY = 'sk-abcdef1234567890XYZ'  # pragma: allowlist secret（假 Key）


def test_redact_patterns():
    s = redact(f'k={KEY} Authorization: Bearer abc.def-123 DEEPSEEK_API_KEY=zzz999 手机13812345678 mail a.b@x.com {{"api_key": "qq11"}}')
    for leak in (KEY, 'abc.def-123', 'zzz999', '13812345678', 'a.b@x.com', 'qq11'):
        assert leak not in s
    assert 'DEEPSEEK_API_KEY=***' in s and '[手机号]' in s


def test_words_and_empty_word():
    assert redact('我在ACME公司做Falcon项目', ['acme', 'Falcon', '']) == '我在***公司做***项目'


def test_filter_handles_args_binary_and_exception():
    f = RedactFilter()
    try:
        raise ValueError(f'bad {KEY}')
    except ValueError:
        import sys
        rec = logging.LogRecord('t', logging.ERROR, __file__, 1, 'got %s %s', (KEY, b'\x00' * 8), sys.exc_info())
    f.filter(rec)
    assert KEY not in rec.getMessage() and '二进制 8 字节' in rec.getMessage()
    assert rec.exc_info is None and KEY not in rec.exc_text and 'ValueError' in rec.exc_text
    single = logging.LogRecord('t', logging.INFO, __file__, 1, 'v=%s', KEY, None)
    f.filter(single)
    assert KEY not in single.getMessage()


def test_token_query_filter_masks_uvicorn_style_records_and_keeps_args_shape():
    from app.logging_safe import TokenQueryFilter
    f = TokenQueryFilter()
    ws = logging.LogRecord('uvicorn.error', logging.INFO, __file__, 1, '%s - "WebSocket %s" [accepted]',
                           ('127.0.0.1:5000', '/ws/audio/s1?a=1&token=SECRET_abc-123&b=2'), None)
    f.filter(ws)
    assert isinstance(ws.args, tuple) and len(ws.args) == 2  # access 格式化器要按位置解包
    assert 'SECRET_abc-123' not in ws.getMessage() and 'token=***&b=2' in ws.getMessage()
    plain = logging.LogRecord('uvicorn.error', logging.INFO, __file__, 1, 'GET /ws/board/s?token=SECRET_xyz', None, None)
    f.filter(plain)
    assert 'SECRET_xyz' not in plain.getMessage() and plain.getMessage().endswith('token=***')
    other = logging.LogRecord('uvicorn.error', logging.INFO, __file__, 1, '%s %d', ('/api/x?mytoken=1', 7), None)
    f.filter(other)
    assert other.args[1] == 7  # 非字符串参数原样保留


def test_mask_uvicorn_tokens_is_idempotent_and_applies_to_both_loggers():
    from app.logging_safe import TokenQueryFilter, mask_uvicorn_tokens
    mask_uvicorn_tokens()
    mask_uvicorn_tokens()
    for name in ('uvicorn.error', 'uvicorn.access'):
        assert sum(isinstance(x, TokenQueryFilter) for x in logging.getLogger(name).filters) == 1


def test_install_writes_redacted_file_with_700(tmp_path):
    log = logging.getLogger('test_logging_safe_isolated')
    log.setLevel(logging.INFO)
    log.propagate = False
    d = tmp_path / 'data' / 'logs'
    h = install(d, words=['秘密公司'], logger=log)
    try:
        log.info('call with %s from 秘密公司', KEY)
        log.error('failed', exc_info=(ValueError, ValueError(f'Bearer {KEY}'), None))
        h.flush()
    finally:
        log.removeHandler(h)
        h.close()
    text = (d / 'app.log').read_text('utf-8')
    assert KEY not in text and '秘密公司' not in text and 'sk-***' in text
    assert stat.S_IMODE(d.stat().st_mode) == 0o700
    assert h.when == 'MIDNIGHT'
