"""M01 会前自检的生产默认探测：DeepSeek 连通、ASR 后端、音源、搜索、声纹登记。

语义诚实优先：
- llm_ping：无 Key -> FAIL；外发关闭 -> WARN 且不发请求；否则 GET {base_url}/models（Bearer，短超时，
  不产生 token 计费、不发送个人内容），只回固定短句，Key 绝不进入 detail/异常。
- asr_ping：与 wiring.asr_factory 的选择一致。云端只有 Key 存在，无无副作用鉴权探测 -> WARN；
  本地 sherpa 三个文件齐全 -> PASS。
- audio_sources：自检在采集页授权之前，服务端无法探测 -> WARN（不阻断开始）。
- search_ping：无 Key -> WARN；外发关闭 -> WARN；已配置 -> WARN（搜索按次计费，默认不做连通探测）。
- voice_enrolled：读本机声纹登记（与 app.api.voice 的 PROFILE 一致）。
"""
import httpx

from .. import wiring
from ..credentials import SOURCE_LABELS
from .selfcheck import FAIL, PASS, WARN

DEFAULT_BASE_URL = 'https://api.deepseek.com'
SHERPA_FILES = ('encoder.int8.onnx', 'decoder.int8.onnx', 'tokens.txt')


def _source_label(creds, name):
    """只给类别名（环境变量/钥匙串/.env），便于排查；绝不返回值。"""
    getter = getattr(creds, 'source', None)
    return SOURCE_LABELS.get(getter(name) if getter else None, '')


def _source(creds, name):
    label = _source_label(creds, name)
    return f'（Key 来自{label}）' if label else ''


def _settings(ctx):
    """生产用 ConfigManager 的当前配置；测试可只在 ctx.config 里放 outbound。"""
    cfg = ctx.config or {}
    manager = cfg.get('config_manager')
    return manager.config if manager is not None else cfg


def llm_ping(creds, config, transport=None, timeout=2.0):
    def check():
        key = creds.get('DEEPSEEK_API_KEY') if creds is not None else None
        if not key:
            return FAIL, '未配置 DeepSeek 凭证'
        if not (config.get('outbound') or {}).get('llm_snippets', True):
            return WARN, '外发已关闭'
        base = (config.get('model') or {}).get('base_url') or DEFAULT_BASE_URL
        try:
            with httpx.Client(transport=transport, timeout=timeout) as http:
                resp = http.get(f'{base.rstrip("/")}/models', headers={'Authorization': f'Bearer {key}'})
        except httpx.TimeoutException:
            return FAIL, '连接超时'
        except httpx.HTTPError:
            return FAIL, '无法连接 DeepSeek'
        if resp.status_code == 200:
            return PASS, '连通' + _source(creds, 'DEEPSEEK_API_KEY')
        if resp.status_code in (401, 403):
            return FAIL, 'Key 无效' + _source(creds, 'DEEPSEEK_API_KEY')
        return FAIL, f'服务返回 {resp.status_code}'
    return check


def asr_ping(creds, config):
    def check():
        key = creds.get('DASHSCOPE_API_KEY') if creds is not None else None
        cloud_allowed = bool((config.get('outbound') or {}).get('asr_audio', True))
        if key and cloud_allowed:
            # 云端转写没有无副作用的鉴权探测；不伪造连通结论。
            source = _source_label(creds, 'DASHSCOPE_API_KEY')
            tail = f'，来源：{source}' if source else ''
            return WARN, f'云转写已配置（Key 已设置{tail}），未做连通探测'
        if all((wiring.SHERPA_DIR / n).is_file() for n in SHERPA_FILES):
            return PASS, '本地转写（sherpa）'
        if key:
            return FAIL, '云端转写已关闭，本地模型缺失'
        return FAIL, '无可用转写后端：未配置云端 Key，本地模型缺失'
    return check


def audio_ping():
    return WARN, '音源需在采集页授权后检测'


def _search_key_name(creds):
    """与搜索路径一致：SEARCH_API_KEY 优先，未配置时复用 DEEPSEEK_API_KEY。只返回凭证名。"""
    return next((n for n in ('SEARCH_API_KEY', 'DEEPSEEK_API_KEY') if creds is not None and creds.get(n)), None)


def search_ping(creds=None, config=None):
    def check():
        cfg_ = config or {}
        name = _search_key_name(creds)
        if not name:
            return WARN, '搜索未配置，联网补充不可用'
        if not (cfg_.get('outbound') or {}).get('web_questions', True):
            return WARN, '外发已关闭'
        if not (cfg_.get('search_transport') or (cfg_.get('search') or {}).get('live', True)):
            return WARN, '搜索传输未启用，联网补充不可用'
        source = _source_label(creds, name)
        tail = f'，来源：{source}' if source else ''
        return WARN, f'搜索已配置（Key 已设置{tail}），未做连通探测'
    return check


def voice_ping(store, root):
    def check():
        from ..audio.enroll import VoiceProfileStore
        row = VoiceProfileStore(store.connect, store.lock, root).load('default')
        return (PASS, '已登记') if row else (FAIL, '声音未登记')
    return check


def asr_label(creds, config):
    """与 asr_ping 同一判断：有 Key 且外发开 -> 云转写；否则本地模型齐全 -> 本地转写。"""
    key = creds.get('DASHSCOPE_API_KEY') if creds is not None else None
    cloud_allowed = bool((config.get('outbound') or {}).get('asr_audio', True))
    if key and cloud_allowed:
        return '云转写'
    if all((wiring.SHERPA_DIR / n).is_file() for n in SHERPA_FILES):
        return '本地转写'
    return '未接入'


def model_label(creds):
    return '已配置' if creds is not None and creds.get('DEEPSEEK_API_KEY') else '未接入'


def search_label(creds, config):
    if _search_key_name(creds) is None:
        return '未接入'
    if not (config.get('outbound') or {}).get('web_questions', True):
        return '外发关闭'
    if config.get('search_transport') or (config.get('search') or {}).get('live', True):
        return '已配置'
    return '未接入'


def speaker_label(store, root):
    from ..audio.enroll import VoiceProfileStore
    row = VoiceProfileStore(store.connect, store.lock, root).load('default')
    return '已登记' if row else '未登记'


def status_labels(creds, config, store, root):
    """首页能力灯的文案；只读本地状态，不发任何外部请求，也不返回值或 Key。"""
    return dict(asr=asr_label(creds, config), model=model_label(creds),
                search=search_label(creds, config), speaker=speaker_label(store, root))


def session_outbound(ctx, session_cfg):
    """把会话快照里的外发覆盖（本场只能关闭）并入全局配置；无覆盖返回 None。"""
    override = (session_cfg or {}).get('outbound') or {}
    if not override:
        return None
    base = _settings(ctx)
    return {'outbound': {**base.get('outbound', {}), **override}}


def default_deps(ctx, timeout=2.0, config=None):
    """生产默认依赖；config 用于并入会话快照覆盖（如本场外发）。
    调用方用 selfcheck_deps 覆盖（含显式 None）即视为用户注入。"""
    cfg = ctx.config or {}
    merged = {**_settings(ctx), **config} if config else _settings(ctx)
    creds = cfg.get('credential_store')
    return dict(audio_sources=audio_ping,
                asr_ping=asr_ping(creds, merged),
                llm_ping=llm_ping(creds, merged, transport=cfg.get('llm_transport'), timeout=timeout),
                search_ping=search_ping(creds, merged),
                voice_enrolled=voice_ping(ctx.store, ctx.root))
