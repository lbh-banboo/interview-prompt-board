export const GROUPS = [
    {
        key: 'turn', title: '判定参数（毫秒）',
        note: '决定“一句话什么时候算说完”。过小会截断长句，过大会让提示变慢。默认值属唯象取值【待标定】。',
        fields: [
            { key: 'turn.T_silence', label: '静音判定', kind: 'int', min: 50, max: 2000,
                hint: '连续静音超过该值时视为一句话结束；越小越灵敏。' },
            { key: 'turn.T_long', label: '长停顿', kind: 'int', min: 100, max: 5000,
                hint: '停顿达到该值按“长停顿”处理，更可能换题。' },
            { key: 'turn.T_hard', label: '强制切分', kind: 'int', min: 200, max: 10000,
                hint: '到点强制切分，防止一方长时间霸占音源。' },
            { key: 'turn.T_answer_silence', label: '回答后静音', kind: 'int', min: 200, max: 20000,
                hint: '回答者停止说话多久算回答结束，影响进入下一题的时机。' },
            { key: 'turn.T_new_turn', label: '新题判定', kind: 'int', min: 300, max: 5000,
                hint: '停顿与语义都指向新题时，等待该值确认换题。' },
            { key: 'turn.max_turn_ms', label: '单题最长时长', kind: 'int', min: 5000, max: 120000,
                hint: '超过该时长强制收束本题，避免一直不换题。' },
            { key: 'turn.max_turn_chars', label: '单题最长字数', kind: 'int', min: 20, max: 2000,
                hint: '识别文本超过该字数即收束本题。' },
            { key: 'turn.monologue_segments', label: '独白判定段数', kind: 'int', min: 1, max: 10,
                hint: '连续出现多少段提问方发言后视为独白。' },
            { key: 'turn.T_monologue', label: '独白累计时长（毫秒）', kind: 'int', min: 3000, max: 60000,
                hint: '独白累计达到该时长时暂停提词。' },
        ],
    },
    {
        key: 'mic_gate', title: '单麦模式',
        note: '线上跳过会议音频时，用声纹区分“你”和“对方”，把对方的话当作提问；需要先完成声音登记。',
        fields: [
            { key: 'turn.mic_as_question', label: '单麦模式', kind: 'bool',
                hint: '默认开启；关闭后只采集麦克风时不会识别对方提问（恢复旧行为）。改动下一场会话生效。' },
        ],
    },
    {
        key: 'retrieval_thresholds', title: '检索阈值（0~1，留空 = 未标定）',
        note: '当前先验值，未经数据标定；标定后建议按真实命中率调整。',
        fields: [
            { key: 'retrieval_thresholds.tau_cos', label: '余弦阈值 τ', kind: 'opt_num', min: 0, max: 1, step: 0.01,
                hint: '主检索的余弦相似度阈值；留空表示未标定。' },
            { key: 'retrieval_thresholds.tau_cos_strong', label: '强匹配阈值', kind: 'opt_num', min: 0, max: 1, step: 0.01,
                hint: '高于该值视为强匹配，可直接给主答案；留空表示未标定。' },
            { key: 'retrieval_thresholds.tau_mid', label: '中等匹配阈值', kind: 'opt_num', min: 0, max: 1, step: 0.01,
                hint: '中等匹配的下限；留空表示未标定。' },
            { key: 'retrieval_thresholds.margin', label: '领先差距 margin', kind: 'opt_num', min: 0, max: 1, step: 0.01,
                hint: '最高分与次高分的差值下限；太小说明有歧义。留空表示未标定。' },
        ],
    },
    {
        key: 'timeouts', title: '超时（毫秒）',
        note: '外部调用等待上限；首条超时不得大于总超时。',
        fields: [
            { key: 'timeouts.web_first_ms', label: '联网首条', kind: 'int', min: 100, max: 60000,
                hint: '联网检索首条结果等待上限。' },
            { key: 'timeouts.web_total_ms', label: '联网总时长', kind: 'int', min: 100, max: 120000,
                hint: '联网检索总时长上限，必须不小于首条超时。' },
            { key: 'timeouts.llm_first_ms', label: '模型首 token', kind: 'int', min: 100, max: 60000,
                hint: '模型首个 token 等待上限。' },
            { key: 'timeouts.llm_total_ms', label: '模型总时长', kind: 'int', min: 100, max: 120000,
                hint: '模型整段回答总时长上限，必须不小于首 token 超时。' },
            { key: 'session.idle_timeout_s', label: '空闲自动结束（秒）', kind: 'int', min: 60, max: 86400,
                hint: '采集停止后多久自动结束会话。' },
        ],
    },
    {
        key: 'budget', title: '预算',
        note: '估算上限，不是计费硬保证；超出后外部调用改为本地处理。转写使用独立额度，优先保障。',
        fields: [
            { key: 'budget.daily_limit', label: '日上限（元，搜索+模型）', kind: 'num', min: 0, max: 100000, step: 0.01,
                hint: '今日搜索/模型估算上限；0 = 停止这些外部调用（不影响转写）。' },
            { key: 'budget.session_limit', label: '单场上限（元，搜索+模型）', kind: 'num', min: 0, max: 100000, step: 0.01,
                hint: '单场搜索/模型估算上限；0 = 本场不做这些外部调用（不影响转写）。' },
            { key: 'budget.asr_daily_limit', label: '转写日上限（元）', kind: 'num', min: 0, max: 100000, step: 0.01,
                hint: '云转写独立日额度【估算，待真实单价】；用尽后自动回退本地转写。' },
            { key: 'budget.asr_session_limit', label: '转写单场上限（元）', kind: 'num', min: 0, max: 100000, step: 0.01,
                hint: '云转写独立单场额度【估算，待真实单价】；用尽后自动回退本地转写。' },
            { key: 'budget.max_inflight', label: '并发上限', kind: 'int', min: 1, max: 64,
                hint: '同时进行的外部请求数上限；占满时转写会退避重试，不当作故障。' },
        ],
    },
    {
        key: 'prices', title: '价格（元）',
        note: '待填写真实单价；当前默认值为估算占位值，只影响用量页的估算。用量口径：ASR=秒、搜索=次、LLM=估算 token。',
        fields: [
            { key: 'prices.asr_per_hour', label: '云转写 / 小时', kind: 'num', min: 0, max: 100000, step: 0.01,
                hint: '待填写真实单价（当前默认 2.5 为估算占位值）。' },
            { key: 'prices.llm_per_1k_tokens', label: '模型 / 千估算 token', kind: 'num', min: 0, max: 100000, step: 0.0001,
                hint: '按估算 token（约 2 字符/token）计价；待填写真实单价（当前为估算值）。' },
            { key: 'prices.search_per_call', label: '搜索 / 次', kind: 'num', min: 0, max: 100000, step: 0.001,
                hint: '待填写真实单价（当前为估算值）。' },
            { key: 'prices.asr_note', label: '单价备注', kind: 'str', maxLen: 200,
                hint: '显示在用量页，提醒单价是否为估算值。' },
        ],
    },
    {
        key: 'model', title: '模型',
        note: 'DeepSeek 兼容接口。Key 在首页凭证区配置，本页不输入 Key。',
        fields: [
            { key: 'model.name', label: '模型名', kind: 'str', maxLen: 64, hint: 'DeepSeek 兼容模型名。' },
            { key: 'model.base_url', label: 'base_url', kind: 'str', maxLen: 200, hint: '兼容 OpenAI 协议的接口地址。' },
        ],
    },
    {
        key: 'search', title: '联网搜索',
        note: '联网搜索使用 DeepSeek Search API，未单独配置 Key 时复用 DeepSeek Key，按次计费（单价为估算占位，待提供真实单价）。',
        fields: [
            { key: 'search.live', label: '启用真实搜索', kind: 'bool',
                hint: '关闭后不发起任何搜索请求，联网补充显示“外部补充不可用”。' },
            { key: 'search.endpoint', label: '接口地址', kind: 'str', maxLen: 200,
                hint: 'DeepSeek Search 端点，默认 https://api.deepseek.com/v1/tools/search。' },
            { key: 'search.count', label: '结果条数', kind: 'int', min: 1, max: 50,
                hint: '每次搜索返回的条数；过多会增加延迟与外发量。' },
            { key: 'search.need_content', label: '返回网页正文', kind: 'bool',
                hint: '默认关闭：只取摘要，降低延迟与外发量。' },
        ],
    },
];
export const FIELDS = GROUPS.flatMap((g) => g.fields);
export const ASR_OPTIONS = [
    { value: 'auto', label: '自动（默认）', disabled: false,
        hint: '有云 Key 且音频外发开启 → 阿里云；否则本地 sherpa。' },
    { value: 'aliyun', label: '阿里云实时转写', disabled: false, hint: '需要 DASHSCOPE_API_KEY 且音频外发开启。' },
    { value: 'sherpa', label: '本地 sherpa', disabled: false, hint: '不联网；需要本机模型文件。' },
];
export const OUTBOUND_ITEMS = [
    { key: 'asr_audio', title: '音频 → 云转写', text: '把会议/房间音频发送给阿里云实时转写；关闭后只在本机转写。单麦/线下模式下这会包含你自己的声音（混音只有一路）。' },
    { key: 'llm_snippets', title: '片段 → DeepSeek', text: '把识别出的问题与题库片段发送给 DeepSeek 生成建议。' },
    { key: 'web_questions', title: '联网问题 → 搜索', text: '把脱敏后的知识类问题发送给 DeepSeek Search 做联网补充。' },
];
export const ANSWERER_ITEM = {
    key: 'asr_answerer', title: '回答者逐字稿',
    text: '是否把回答者转写写进记录与导出；默认关闭。关闭时回答者音频仍可能随混音上传用于识别（受“音频 → 云转写”控制）。',
};
export const pathGet = (obj, key) => key.split('.').reduce((node, part) => (node == null ? undefined : node[part]), obj);
export function pathSet(out, key, value) {
    const parts = key.split('.');
    const leaf = parts.pop();
    let node = out;
    for (const p of parts)
        node = node[p] ??= {};
    node[leaf] = value;
}
export function fieldText(spec, value) {
    if (value === null || value === undefined)
        return '';
    return String(value);
}
export function parseField(spec, raw) {
    if (spec.kind === 'bool') {
        const text = raw.trim().toLowerCase();
        if (text === 'true')
            return { ok: true, value: true };
        if (text === 'false')
            return { ok: true, value: false };
        return { ok: false, error: `${spec.label} 必须是布尔值` };
    }
    if (spec.kind === 'str') {
        const value = raw.trim();
        if (!value)
            return { ok: false, error: `${spec.label} 不能为空` };
        if (spec.maxLen && value.length > spec.maxLen)
            return { ok: false, error: `${spec.label} 最多 ${spec.maxLen} 个字符` };
        return { ok: true, value };
    }
    const text = raw.trim();
    if (spec.kind === 'opt_num' && text === '')
        return { ok: true, value: null };
    if (text === '')
        return { ok: false, error: `${spec.label} 不能为空` };
    const n = Number(text);
    if (!Number.isFinite(n))
        return { ok: false, error: `${spec.label} 必须是数字` };
    if (spec.kind === 'int' && !Number.isInteger(n))
        return { ok: false, error: `${spec.label} 必须是整数` };
    if (spec.min !== undefined && n < spec.min)
        return { ok: false, error: `${spec.label} 不能小于 ${spec.min}` };
    if (spec.max !== undefined && n > spec.max)
        return { ok: false, error: `${spec.label} 不能大于 ${spec.max}` };
    return { ok: true, value: n };
}
/** 只把输入框里与当前配置不同的字段并入 patch；非法字段计入 errors、不进入 patch。 */
export function buildPatch(specs, current, raw) {
    const patch = {};
    const errors = [];
    let changed = 0;
    for (const spec of specs) {
        if (!(spec.key in raw))
            continue;
        const text = raw[spec.key];
        const parsed = parseField(spec, text);
        if (!parsed.ok) {
            errors.push(parsed.error);
            continue;
        }
        const now = pathGet(current, spec.key);
        const same = parsed.value === null ? now === null || now === undefined : parsed.value === now;
        if (same)
            continue;
        pathSet(patch, spec.key, parsed.value);
        changed += 1;
    }
    return { patch, errors, changed };
}
export const outboundPatch = (key, on) => ({ outbound: { [key]: on } });
export function outboundLine(flags) {
    const f = flags ?? {};
    const mark = (k) => (f[k] === false ? '关' : '开');
    return `本场外发：音频→云转写 ${mark('asr_audio')} · 片段→DeepSeek ${mark('llm_snippets')} · 联网 ${mark('web_questions')}`;
}
export const effectiveNotice = (active) => active ? '当前会话使用启动时的快照，修改下一场生效。' : '已保存，下一场会话生效。';
/** 当前阈值来源：任一阈值已设置 -> 下一场会话快照；全部留空 -> 默认文件。 */
export function thresholdSource(config) {
    const th = config?.retrieval_thresholds ?? {};
    const set = ['tau_cos', 'tau_cos_strong', 'tau_mid', 'margin']
        .filter((k) => th[k] !== null && th[k] !== undefined);
    return set.length
        ? '当前阈值来源：配置快照（下一场会话生效）'
        : '当前阈值来源：默认文件 app/retrieval/thresholds.json';
}
export const ACTIVE_WARNING = '有进行中的会话：本页修改不会改变当前会话（启动时已冻结快照），下一场生效。';
export function defaultHint(spec, defaults) {
    const d = pathGet(defaults, spec.key);
    const text = spec.kind === 'opt_num' && (d === null || d === undefined) ? '未标定'
        : spec.kind === 'bool' ? (d ? '开' : '关') : String(d ?? '—');
    return `默认 ${text}`;
}
export const profilePatch = (p, alias) => ({
    alias: alias.trim(), roles: p.roles ?? [], note: p.note ?? '', library_ids: p.library_ids ?? [],
});
export function profileRows(list) {
    return list.map((p) => ({ ...p, rolesText: (p.roles ?? []).join('、'), librariesText: `${(p.library_ids ?? []).length} 套主库` }));
}
export function renameError(alias) {
    return alias.trim() ? null : '资料代号不能为空';
}
export const DEVELOPER_NOTE = '故障注入只用于自动测试（tests 里的 FaultInjector），产品界面不提供注入入口，避免误触发现场故障。';
