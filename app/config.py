"""M15-1 配置加载与校验：config.default.json + data/config.json 合并，手写 schema；会话开始时冻结快照。"""
import copy
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_PATH = Path(__file__).resolve().parents[1] / 'config.default.json'

INT, NUM, BOOL, STR, OPT_NUM, ENUM = 'int', 'num', 'bool', 'str', 'opt_num', 'enum'
ASR_BACKENDS = ('auto', 'aliyun', 'sherpa')
# 点分键 -> (类型, 最小, 最大)；ENUM 的“最小”是允许值元组
SCHEMA = {
    'turn.T_silence': (INT, 50, 2000),
    'turn.T_long': (INT, 100, 5000),
    'turn.T_hard': (INT, 200, 10000),
    'turn.T_answer_silence': (INT, 200, 20000),
    'turn.mic_as_question': (BOOL, None, None),
    'turn.T_new_turn': (INT, 300, 5000),
    'turn.max_turn_ms': (INT, 5000, 120000),
    'turn.max_turn_chars': (INT, 20, 2000),
    'turn.monologue_segments': (INT, 1, 10),
    'turn.T_monologue': (INT, 3000, 60000),
    'retrieval_thresholds.tau_cos': (OPT_NUM, 0, 1),
    'retrieval_thresholds.tau_cos_strong': (OPT_NUM, 0, 1),
    'retrieval_thresholds.tau_mid': (OPT_NUM, 0, 1),
    'retrieval_thresholds.margin': (OPT_NUM, 0, 1),
    'asr.backend': (ENUM, ASR_BACKENDS, None),
    'model.name': (STR, 1, 64),
    'model.base_url': (STR, 8, 200),
    'search.live': (BOOL, None, None),
    'search.endpoint': (STR, 8, 200),
    'search.count': (INT, 1, 50),
    'search.need_content': (BOOL, None, None),
    'timeouts.web_first_ms': (INT, 100, 60000),
    'timeouts.web_total_ms': (INT, 100, 120000),
    'timeouts.llm_first_ms': (INT, 100, 60000),
    'timeouts.llm_total_ms': (INT, 100, 120000),
    'outbound.asr_audio': (BOOL, None, None),
    'outbound.asr_answerer': (BOOL, None, None),
    'outbound.llm_snippets': (BOOL, None, None),
    'outbound.web_questions': (BOOL, None, None),
    'budget.daily_limit': (NUM, 0, 100000),
    'budget.session_limit': (NUM, 0, 100000),
    'budget.max_inflight': (INT, 1, 64),
    'budget.asr_daily_limit': (NUM, 0, 100000),
    'budget.asr_session_limit': (NUM, 0, 100000),
    'prices.asr_per_hour': (NUM, 0, 100000),
    'prices.llm_per_1k_tokens': (NUM, 0, 100000),
    'prices.search_per_call': (NUM, 0, 100000),
    'prices.asr_note': (STR, 1, 200),
    'session.idle_timeout_s': (INT, 60, 86400),
}


def _flatten(d, prefix=''):
    for k, v in d.items():
        key = prefix + k
        if isinstance(v, dict):
            yield from _flatten(v, key + '.')
        else:
            yield key, v


URL_KEYS = ('model.base_url', 'search.endpoint')
LOOPBACK_HOSTS = ('127.0.0.1', 'localhost')


def _check_url(key, value):
    """这两项会带着 API Key 发请求：必须是 https（本机回环可用 http），不带账号密码。"""
    try:
        p = urlsplit(value.strip())
        host = p.hostname
    except ValueError:
        host = None
    if host and not p.username and not p.password and (p.scheme == 'https' or (p.scheme == 'http' and host in LOOPBACK_HOSTS)):
        return None
    return f'{key} 必须是 https 地址（本机回环地址可用 http），且不能带账号密码'


def _check(key, value):
    """返回错误文案，合法返回 None。"""
    spec = SCHEMA.get(key)
    if spec is None:
        return f'未知配置项 {key}'
    kind, lo, hi = spec
    if kind == BOOL:
        return None if isinstance(value, bool) else f'{key} 必须是布尔值'
    if kind == ENUM:
        return None if value in lo else f'{key} 必须是 {" / ".join(lo)} 之一'
    if kind == STR:
        if not isinstance(value, str) or not lo <= len(value.strip()) <= hi:
            return f'{key} 必须是 {lo}~{hi} 字符的字符串'
        return _check_url(key, value) if key in URL_KEYS else None
    if kind == OPT_NUM and value is None:
        return None
    ok = isinstance(value, int) if kind == INT else isinstance(value, (int, float))
    if not ok or isinstance(value, bool) or not lo <= value <= hi:
        return f'{key} 必须是 {lo}~{hi} 的{"整数" if kind == INT else "数值"}'
    return None


def validate_patch(patch):
    """校验（可部分的）配置字典，返回 (扁平合法项, 错误列表)。"""
    if not isinstance(patch, dict):
        return {}, ['配置必须是对象']
    good, errors = {}, []
    for key, value in _flatten(patch):
        err = _check(key, value)
        if err:
            errors.append(err)
        else:
            good[key] = value
    return good, errors


def _apply(base, flat):
    for key, value in flat.items():
        node = base
        *parents, leaf = key.split('.')
        for p in parents:
            node = node[p]
        node[leaf] = value
    return base


def _cross_check(cfg):
    t, w = cfg['turn'], cfg['timeouts']
    errors = []
    if not t['T_silence'] <= t['T_long'] <= t['T_hard']:
        errors.append('须满足 T_silence <= T_long <= T_hard')
    if w['web_first_ms'] > w['web_total_ms']:
        errors.append('联网首条超时不得大于总超时')
    if w.get('llm_first_ms', 0) > w.get('llm_total_ms', 0):
        errors.append('模型首 token 超时不得大于总超时')
    return errors


def _read(path):
    try:
        return json.loads(Path(path).read_text('utf-8')), None
    except FileNotFoundError:
        return {}, None
    except (OSError, ValueError):
        return {}, f'{Path(path).name} 无法解析，已忽略'


def load_config(default_path=DEFAULT_PATH, user_path=None):
    """返回 (配置, 错误列表)。用户文件的非法项/损坏文件回退默认值，不阻止启动。"""
    cfg = json.loads(Path(default_path).read_text('utf-8'))
    errors = []
    if user_path is not None:
        user, err = _read(user_path)
        errors += [err] if err else []
        good, bad = validate_patch(user)
        errors += bad
        trial = _apply(copy.deepcopy(cfg), good)
        cross = _cross_check(trial)
        if cross:
            errors += cross
        else:
            cfg = trial
    return cfg, errors


class ConfigManager:
    """持久化用户配置；运行中的会话只使用 snapshot_for_session() 冻结的副本。"""

    def __init__(self, user_path, default_path=DEFAULT_PATH):
        self.user_path = Path(user_path)
        self.default_path = Path(default_path)
        self.config, self.errors = load_config(self.default_path, self.user_path)

    def update(self, patch):
        """合并并落盘；非法则抛 ValueError（含全部错误），不写文件。"""
        good, errors = validate_patch(patch)
        trial = _apply(copy.deepcopy(self.config), good)
        errors += _cross_check(trial) if not errors else []
        if errors:
            raise ValueError('；'.join(errors))
        user, _ = _read(self.user_path)
        merged, _ = validate_patch(user)
        merged.update(good)
        out = {}
        for key, value in merged.items():
            node = out
            *parents, leaf = key.split('.')
            for p in parents:
                node = node.setdefault(p, {})
            node[leaf] = value
        self.user_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.user_path.with_suffix('.tmp')
        tmp.write_text(json.dumps(out, ensure_ascii=False, indent=2), 'utf-8')
        os.replace(tmp, self.user_path)
        self.config = trial
        self.errors = []
        return copy.deepcopy(self.config)

    def snapshot_for_session(self):
        """会话开始时调用，返回写入 sessions.config_snapshot_json 的 JSON 串。"""
        return json.dumps(self.config, ensure_ascii=False, sort_keys=True)
