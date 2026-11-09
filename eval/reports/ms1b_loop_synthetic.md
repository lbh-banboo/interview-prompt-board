类别: SIMULATED（合成链路，无真人声音）

# MS1b 先验报告：commit -> prompt_sent 延迟（合成链路）

本报告只说明本机上答案环（检索 + 发布）自身的耗时量级，不是 MS1b 验收结论，不含真人语音、真实 ASR、真实网络与浏览器渲染，不给出通过/不通过判定。

## 方法

- 链路：FakePipe 注入 `committed` 轮次事件 -> EventBus.pull -> AnswerLoop 检索（专用单线程执行器）-> classify_hit -> publish_prompt_primary / prompt_status。
- 起点 t_committed：事件入列时刻（perf_counter_ns，与 pipeline.event_ns 同口径）；终点：publish 返回后。取自 metrics 表 `prompt_sent.latency_ms`。
- 库：20 个已审核条目的临时库（tmp，非 data/ 真实库）；每档 61 次 committed，首次（含模型首次推理预热）单列，不计入分位数；统计 60 次。
- 问法：2/3 为库内原句，1/3 为改写句（“你能说说X怎么做吗”）。
- 机器：macOS arm64（本机，单次运行，未做多轮重复，分位数仅供量级参考）。
- 不含：VAD/ASR 耗时、TurnDetector 判定延迟（T_silence 等）、WS 传输、浏览器渲染（display_ack 的 t_visible）。

## 结果（ms）

| 嵌入档 | 说明 | N | P50 | P95 | max | 首次(预热) | 产出分布 |
|---|---|---|---|---|---|---|---|
| HashEmbedder | SIMULATED，仅测流程，不代表语义效果 | 60 | 0.31 | 0.37 | 0.48 | 0.84 | L1 40 / 仅 status 21（改写句未命中是哈希向量的预期表现） |
| bge-small-zh-v1.5（fastembed 真实模型，本地） | 向量为真实模型，问句为合成文本 | 60 | 3.08 | 4.65 | 7.00 | 3.17 | L1 61 |

## 解读与限制

- 答案环自身在本机是个位数毫秒，相对 TurnDetector 的 T_silence（约 200ms）与 ASR 追赶可以忽略；端到端推送延迟的主要成分在音频侧，需真人音频实测。
- bge 档的 L1 全命中来自合成问句与库内问法高度相似（含改写句），不能外推到真实面试口语；阈值来自 `thresholds.json`（规则改写评测集标定，非真实语音）。
- 20 条库规模很小；条目数增大后向量路延迟的变化未测。
- 本报告为 SIMULATED，真人声音下的 commit->visible 延迟、误触发/漏触发率、串题率均未测量。
