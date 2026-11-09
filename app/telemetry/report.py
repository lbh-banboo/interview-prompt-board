"""M14-3/5 报告：分位数、Wilson 置信区间、三类报告与分母规则。

rows 每题一条 dict（时间单位 ms）：
  sample_id, audio_path, log_path   样本索引（失败样本清单用）
  verified      True 才进分母（未校对的标注段排除并计数）
  mode          可选 'online'/'offline'，与 build_report 的 mode 不同则不属于本报告
  t_end, t_committed   真值结束时刻 / 本地提交时刻（t_committed 缺省=未提交=漏判）。
                       两者都是"调用方传入的音频时钟时刻"，本模块不换算、不校正时钟。
  simulated     任一行为真则整份报告强制为 SIMULATED（无论传入 kind），永不出现 PASS
  premature     commit_rolled_back 次数（candidate_withdrawn 不计入，用 withdrawn 单列）
  missed        漏判标记；t_committed 缺失或晚于 t_end+3000ms 也视为漏判
  role_ok, role_unknown, overlap_truth   角色判对 / 判为 unknown / 真值重叠区间
  t_visible, t_web_trigger, t_web_first  可选，首提示可见延迟、联网首条

结论取值：PASS / FAIL / PROVISIONAL / NOT TESTED / SIMULATED。
  SIMULATED   kind 为 SIMULATED 或任一行 simulated 为真
  NOT TESTED  有效样本 n=0
  FAIL        n>0 且点估计不达标（含有效样本全无 t_committed，P95 缺失）
  PROVISIONAL 点估计达标但 n<30（样本不足，不得写 PASS）
  PASS        点估计达标且 n>=30；过早/漏判率的 Wilson 上界超过门槛时在结论旁注明"上界未达标"
"""
import csv
import io
import math

KINDS = ('ALGO_REPLAY', 'REAL_BROWSER', 'REAL_VISIBLE', 'SIMULATED')
MISS_WINDOW_MS = 3000
GATE_P95_MS, GATE_PREMATURE, GATE_MISSED = 300, 0.02, 0.02
SMALL_N = 30
Z95 = 1.96


def percentiles(values):
    """线性插值：升序 x[0..n-1]，P_p = x[floor(h)] + (h-floor(h))*(x[ceil(h)]-x[floor(h)])，h=(n-1)*p/100。
    n=1 时各分位均为该值；空输入返回 None 值。"""
    xs = sorted(values)
    if not xs:
        return dict(p50=None, p95=None, p99=None, max=None)

    def pct(p):
        h = (len(xs) - 1) * p / 100
        lo = math.floor(h)
        hi = min(lo + 1, len(xs) - 1)
        return xs[lo] + (h - lo) * (xs[hi] - xs[lo])
    return dict(p50=pct(50), p95=pct(95), p99=pct(99), max=xs[-1])


def wilson_ci(k, n, z=Z95):
    """Wilson 得分区间：center=(p+z²/2n)/(1+z²/n)，half=z*sqrt(p(1-p)/n+z²/4n²)/(1+z²/n)；n=0 返回 None。"""
    if n <= 0:
        return None
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, c - h), min(1.0, c + h)


def _f(x):
    return '-' if x is None else f'{x:.1f}'


def _pct(k, n):
    if n == 0:
        return '-'
    lo, hi = wilson_ci(k, n)
    return f'{k}/{n} = {k / n * 100:.2f}% (95% CI {lo * 100:.2f}%~{hi * 100:.2f}%)'


def _dist_line(label, values):
    p = percentiles(values)
    return (f'- {label}: n={len(values)}, P50={_f(p["p50"])}, P95={_f(p["p95"])}, '
            f'P99={_f(p["p99"])}, max={_f(p["max"])} (ms)')


def _analyse(row):
    t_end, t_c = row.get('t_end'), row.get('t_committed')
    lat = None if t_c is None or t_end is None else t_c - t_end
    missed = bool(row.get('missed')) or lat is None or lat > MISS_WINDOW_MS
    return lat, missed


def build_report(rows, kind, mode='online', csv_path=None):
    if kind not in KINDS:
        raise ValueError(f'kind 必须是 {KINDS}')
    if any(r.get('simulated') for r in rows):
        kind = 'SIMULATED'
    mode_rows = [r for r in rows if r.get('mode', mode) == mode]
    other_mode = len(rows) - len(mode_rows)
    valid = [r for r in mode_rows if r.get('verified') is True]
    unverified = len(mode_rows) - len(valid)
    n = len(valid)

    recs = []
    for r in valid:
        lat, missed = _analyse(r)
        recs.append(dict(r=r, lat=lat, missed=missed, premature=int(r.get('premature') or 0),
                         overlap=bool(r.get('overlap_truth')), unknown=bool(r.get('role_unknown')),
                         role_ok=bool(r.get('role_ok')) and not r.get('role_unknown')))
    lats = [x['lat'] for x in recs if x['lat'] is not None]
    n_missed = sum(x['missed'] for x in recs)
    n_premature = sum(x['premature'] for x in recs)
    n_prem_rows = sum(x['premature'] > 0 for x in recs)
    n_success = sum((not x['missed']) and x['premature'] == 0 for x in recs)
    role_rows = [x for x in recs if not x['overlap']]
    n_overlap = n - len(role_rows)
    n_unknown = sum(x['unknown'] for x in role_rows)
    n_role_ok = sum(x['role_ok'] for x in role_rows)
    p95 = percentiles(lats)['p95']

    if kind == 'SIMULATED':
        verdict = 'SIMULATED'
    elif n == 0:
        verdict = 'NOT TESTED'
    else:
        ok = (p95 is not None and p95 <= GATE_P95_MS and n_premature / n <= GATE_PREMATURE
              and n_missed / n <= GATE_MISSED)
        verdict = 'FAIL' if not ok else 'PROVISIONAL' if n < SMALL_N else 'PASS'
        over = []
        if ok:
            if wilson_ci(n_prem_rows, n)[1] > GATE_PREMATURE:
                over.append('过早率')
            if wilson_ci(n_missed, n)[1] > GATE_MISSED:
                over.append('漏判率')
        if verdict == 'PROVISIONAL':
            verdict += f'（点估计达标但样本不足 n<{SMALL_N}，不得视为通过）'
        if over:
            verdict += f'（{"/".join(over)}95% CI 上界未达标）'

    L = [f'类别: {kind}', '', f'# 性能评测报告（{"线上" if mode == "online" else "线下"} / {mode}）', '',
         f'- 结论: {verdict}',
         f'- 样本数 n={n}（未校对排除 {unverified}，其他 mode 排除 {other_mode}）']
    if kind == 'SIMULATED':
        L.append('- 说明: 模拟/Mock 结果，不构成真实通过结论，也不得被其他类别继承。')
    else:
        L.append('- 说明: 本类别结论不得由其他类别继承。门槛: 判定延迟 P95<=300ms，过早<=2%，漏判<=2%。')
        if n < SMALL_N:
            L.append(f'- 小样本提示: n={n}<{SMALL_N}，点估计达标≠稳定达标，请以 95% CI 为准。')
    L += ['', '## 延迟（按有效事件分布，失败延迟未剔除）',
          _dist_line('判定延迟 t_committed-t_end', lats)]
    vis = [r['t_visible'] - r['t_committed'] for r in valid
           if r.get('t_visible') is not None and r.get('t_committed') is not None]
    web = [r['t_web_first'] - r['t_web_trigger'] for r in valid
           if r.get('t_web_first') is not None and r.get('t_web_trigger') is not None]
    if vis:
        L.append(_dist_line('首提示延迟 t_visible-t_committed', vis))
    if web:
        L.append(_dist_line('联网首条 t_web_first-t_web_trigger', web))
        L.append(f'- 联网首条逐次达标(<=1500ms): {sum(v <= 1500 for v in web)}/{len(web)}')
    L += ['', '## 全样本', f'- 全样本成功率（未漏判且无过早）: {_pct(n_success, n)}',
          f'- 漏判数: {n_missed}，漏判率 {_pct(n_missed, n)}',
          f'- 过早结束(commit_rolled_back): 回滚总数 {n_premature} / 题数 {n}；有回滚的题 {n_prem_rows}；'
          f'过早率 {_pct(n_prem_rows, n)}（CI 按有回滚的题数；门槛判定用回滚总数/题数'
          f'={"-" if n == 0 else f"{n_premature / n * 100:.2f}%"}）',
          f'- candidate_withdrawn（单独统计，不计入过早）: {sum(int(x["r"].get("withdrawn") or 0) for x in recs)}',
          '', '## 角色识别',
          f'- 真值重叠区间（不进分母，单列）: {n_overlap}',
          f'- 角色分母: {len(role_rows)}',
          f'- 角色正确率（unknown 计错）: {_pct(n_role_ok, len(role_rows))}',
          f'- unknown 数: {n_unknown}，覆盖率（非 unknown 占比）: {_pct(len(role_rows) - n_unknown, len(role_rows))}']

    fails = []
    for x in recs:
        why = []
        if x['missed']:
            why.append('漏判')
        if x['premature']:
            why.append(f'过早x{x["premature"]}')
        if not x['overlap'] and x['unknown']:
            why.append('unknown')
        elif not x['overlap'] and not x['role_ok']:
            why.append('角色错误')
        if why:
            fails.append((x['r'], '+'.join(why)))
    L += ['', '## 失败/漏判样本清单（保留音频与日志索引）']
    L += [f'- {r.get("sample_id")} | {reason} | audio={r.get("audio_path")} | log={r.get("log_path")}'
          for r, reason in fails] or ['- 无']
    L += ['', '## 重叠讲话样本（单列）']
    L += [f'- {x["r"].get("sample_id")} | audio={x["r"].get("audio_path")}' for x in recs if x['overlap']] or ['- 无']
    text = '\n'.join(L) + '\n'

    if csv_path is not None:
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator='\n')
        w.writerow(['sample_id', 'latency_ms', 'missed', 'premature', 'role_ok', 'role_unknown',
                    'overlap_truth', 'audio_path', 'log_path'])
        for x in recs:
            r = x['r']
            w.writerow([r.get('sample_id'), '' if x['lat'] is None else f'{x["lat"]:.1f}', int(x['missed']),
                        x['premature'], int(x['role_ok']), int(x['unknown']), int(x['overlap']),
                        r.get('audio_path'), r.get('log_path')])
        with open(csv_path, 'w', encoding='utf-8', newline='') as f:
            f.write(buf.getvalue())
    return text
