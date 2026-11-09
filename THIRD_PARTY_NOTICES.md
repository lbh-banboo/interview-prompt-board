# THIRD_PARTY_NOTICES

源码快照由 `tools/r0_fetch.py` 按完整 commit SHA 取得，哈希见 `third_party/MANIFEST.json`（sha256 + git blob）。
源码快照本身不随仓库发布（见 `.gitignore`），仓库只保留 `MANIFEST.json` 与各上游 LICENSE；需要时按 MANIFEST 的 commit 重新取得。
移植纪律见《开源复用评估与模块标记》§6：文件头写 `Ported from <repo>@<完整commit SHA>:<path>, <License> © <holder>. Modified: ...`。

| 仓库 | commit | 许可证 | 版权方 | 状态 |
|---|---|---|---|---|
| buluoai6-eng/online-meeting-interview-assistant | 78083e18e05eb29e089a612c496a2d425d882f1b | MIT | © 2026 buluoai6-eng | 仅取得，尚未移植 |
| lesue1/interview-assistant | 739b01f2a0f816dbeaf9c0d1b0afa80ed0c61f5e | MIT | © 2026 lesue1 | 仅取得，尚未移植 |
| JWM0203/MeetingCopilot | 337973a0a2e34424fb541a158d89533b5c4514cd | Apache-2.0 | © 2026 JWM0203（无 NOTICE 文件，API 404） | 仅取得；移植须附许可证副本并在改动文件注明修改 |
| ukez15/interview-copilot | 4a80d842fa22a359b4ab05eea8b2c8432c113e07 | MIT | © 2026 ukez15 | 仅取得，尚未移植 |
| Kaalia0912/ai-interview-assistant | f6dc0f7b11887c0bc81d8dc5e8a37c38e9da6bef | MIT | © 2026 Kaalia0912 | 仅取得，尚未移植 |

核验范围：仅仓库根 LICENSE。文件级声明、模型权重、语料正文（Kaalia 所列两个语料仓库）、提示词授权仍 **NOT VERIFIED**，未核验资产不进入正式包。

## 已移植/引入的文件（2026-10-29 S1a）
| 文件 | 来源 | 许可证 |
|---|---|---|
| web/capture/{pcm-worklet,pcm,frame-buffer,backoff,framer,sender}.ts | ukez15/interview-copilot@4a80d842fa22（见各文件头） | MIT © 2026 ukez15 |
| app/audio/vad.py | JWM0203/MeetingCopilot@337973a0a2e3:electron/asr/vad.ts（改写） | Apache-2.0 © 2026 JWM0203，文件头注明已修改 |
| app/domain/semantic.py | buluoai6 question-detector/turn-guard；lesue1 main.py is_non_question（改写） | MIT |
| models/silero_vad_16k_op15.onnx | PyPI silero-vad==6.2.3（sha256 见 models/MANIFEST.json） | MIT，副本 models/LICENSE.silero-vad |
| sherpa-onnx 2.0.1 | pip 依赖 | Apache-2.0 |

## 集成接线时补记（2026-10-29 P2~P4）
| 文件 / 资产 | 来源 | 许可证 |
|---|---|---|
| app/health/monitor.py | JWM0203/MeetingCopilot@337973a0a2e3:electron/providerErrors.ts（错误分类改写，只保留错误码与中文文案） | Apache-2.0 © 2026 JWM0203，文件头注明已修改 |
| app/llm/prompts.py | MeetingCopilot@337973a0a2e3:electron/llm/prompts.ts（smartClip / 稳定前缀思路，改写）；提示词“只整理不创作”思路参考 Kaalia structurer | Apache-2.0 © 2026 JWM0203；Kaalia 部分为 MIT，仅借鉴 |
| app/llm/client.py | MeetingCopilot@337973a0a2e3:electron/providerErrors.ts（HTTP 状态/传输层错误分类，改写） | Apache-2.0 © 2026 JWM0203，文件头注明已修改 |
| app/library/enhance.py | Kaalia0912/ai-interview-assistant@f6dc0f7b11:rag-assistant/app/core/structurer.py（仅借鉴“只整理、不创作”思路，无代码移植） | MIT © 2026 Kaalia0912 |
| models/bge-small-zh-v1.5/ | Qdrant/bge-small-zh-v1.5（原模型 BAAI/bge-small-zh-v1.5），revision 46fbe35f…，经 hf-mirror 取得 | MIT |
| models/speaker/3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx | csukuangfj/speaker-embedding-models（原模型 modelscope iic/speech_campplus_sv_zh-cn_16k-common，3D-Speaker CAM++） | Apache-2.0（以上游为准，商用前需复核） |
| models/sherpa-streaming-paraformer-bilingual-zh-en/ | csukuangfj/sherpa-onnx-streaming-paraformer-bilingual-zh-en（原模型 modelscope paraformer-bilingual-zh-en） | 以上游仓库/原模型为准，**NOT VERIFIED**，商用前需复核 |
| models/silero_vad_16k_op15.onnx | 见上表 | MIT，副本 models/LICENSE.silero-vad |

模型权重的 sha256 / revision / 取得方式见 `models/MANIFEST.json`。权重不随仓库发布，用 `tools/fetch_models.py` 下载并校验。

## 首页视觉素材（来源待确认，2026-11-08）

欢迎首页（`web/index.html` + `web/landing/`）的视觉素材取自一份本地参考页
（紫色格子主题，来源不明的单文件演示页），样式与动态为手工移植，但下列**二进制素材
的原始出处与再分发许可均未核实**。素材集中在 `web/landing/assets/`，便于整体替换。

| 文件 | 原参考页注释的来源描述 | 许可证 |
|---|---|---|
| `web/landing/assets/video/bg-loop.mp4`（紫粉渐变字符雨循环视频，10s） | 来源未知 | **NOT VERIFIED** |
| `web/landing/assets/poster/bg-poster.jpg`（视频海报帧） | 来源未知（与视频同源） | **NOT VERIFIED** |
| `web/landing/assets/logo/logo.webp`（页头圆形 logo 图） | 来源未知 | **NOT VERIFIED** |
| `web/landing/assets/fonts/inter-var-latin.woff2` | 原文件注释称取自 Google Fonts（Inter） | 原项目 Inter 为 SIL OFL 1.1，但本文件的子集化/再分发链未核实 |
| `web/landing/assets/fonts/bubbledot-icg-finepos.woff2` | 原文件注释称取自 OnlineWebFonts（BubbledotICG-FinePos 点阵展示体） | **NOT VERIFIED**（OnlineWebFonts 的再分发条款历来严格） |
| `web/landing/assets/fonts/geist-pixel-circle.woff2` | 原文件注释称为 Vercel Geist 字体的点阵回退 | **NOT VERIFIED** |

**开源发布前须确认许可，不能确认的请替换**：视频/海报可换成自制或 CC0 素材；
点阵标题字体可换成 OFL 许可的展示体；logo 需要自有设计。参考页的 Font Awesome 6
Brands 子集（品牌图标）**未随移植带入**本仓库，无对应条目。

**发布决定（2026-11-09）**：维护者决定首页素材按现状随仓库发布，上表的来源与许可**仍未核实**。
这些文件不在 MIT 许可范围内（见 `LICENSE` 末段）；收到权利人异议时会立即替换（素材集中在 `web/landing/assets/`）。

## 变更记录
- 2026-11-08：腾讯云实时 ASR 适配器为自研代码（无第三方代码移植），已随“不做腾讯 ASR”的决定移除；本清单无对应第三方条目需要保留。
