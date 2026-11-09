类别: SYNTHETIC_TTS（合成语音，非真人；不代表真实声音结果）

# 阿里云 fun-asr-realtime 首轮实测（2026-10-29）

- 端点: `dashscope.aliyuncs.com`；模型 fun-asr-realtime；max_sentence_silence=200；heartbeat=true
- 语音: macOS say -v Tingting，14 句，前导静音 300ms、尾部静音 1300ms；20ms 帧真实节拍推送

## 逐句结果

| 句 | 语音区间 ms | 首个 partial 延迟 ms（音频/墙钟） | final 延迟 ms（音频/墙钟，相对语音结束） | 覆盖滞后中位 ms | 事件数 | 句数(start 键) | 编辑距离/期望字数 | 识别文本 |
|---|---|---|---|---|---|---|---|---|
| s01 | 320-4220 | 460 / 447 | 680 / 672 | 220 | 8 | 1 | 0/18 | 请你介绍一下你在上一家公司负责的项目。 |
| s02 | 300-3040 | 400 / 388 | 640 / 639 | 460 | 5 | 1 | 1/25 | TAFKA的consumer group是怎么工作的？ |
| s03 | 300-2200 | 440 / 423 | 920 / 911 | 300 | 5 | 1 | 0/10 | 你为什么选择这个岗位？ |
| s04 | 320-3580 | 460 / 448 | 880 / 864 | 520 | 7 | 1 | 0/14 | 请问你对微服务架构有什么理解？ |
| s05 | 300-6500 | 360 / 344 | 720 / 701 | 210 | 8 | 1 | 0/23 | Redis 的持久化机制 RDB 和 AOF 有什么区别？ |
| s06 | 300-3040 | 340 / 333 | 520 / 517 | 230 | 6 | 1 | 0/20 | 你在项目里是怎么做 code review 的？ |
| s07 | 320-3960 | 440 / 428 | 760 / 754 | 290 | 9 | 2 | 0/14 | 讲讲。你遇到过最棘手的线上故障？ |
| s08 | 320-3000 | 360 / 344 | 740 / 730 | 240 | 6 | 1 | 0/20 | Spring Boot的自动配置原理是什么？ |
| s09 | 300-5100 | 380 / 378 | 760 / 744 | 470 | 8 | 1 | 0/22 | 如果让你设计一个短链接系统，你会怎么考虑高并发？ |
| s10 | 300-3200 | 1040 / 1025 | 1500 / 1487 | 900 | 6 | 1 | 0/22 | 你对Kubernetes的Pod调度了解多少？ |
| s11 | 300-5160 | 660 / 640 | 740 / 733 | 340 | 9 | 1 | 0/21 | 你的职业规划是什么？未来三年想往哪个方向发展？ |
| s12 | 320-4600 | 300 / 294 | 580 / 576 | 160 | 8 | 1 | 1/14 | MySQL 的索引为什么用 B 加树？ |
| s13 | 320-4100 | 320 / 313 | 900 / 892 | 330 | 6 | 1 | 0/22 | 请用Python解释一下GIL对多线程的影响。 |
| s14 | 300-2060 | 340 / 332 | 720 / 712 | 240 | 5 | 1 | 0/10 | 你有什么想问我们的吗？ |

## 汇总（合成语音）

- 覆盖滞后（到达时已推音频 - audio_covered_until_ms）P50=300ms P95=860ms（96 个事件）
- 粗 CER = 2/255 = 0.8%（去标点空格、转小写后的字符级编辑距离；英文词若被转写成中文谐音计为错误）

## TurnDetector 先验（K1）

条件 `audio_covered_until_ms >= t_speech_end - 100`：在 speech_end 之后 W ms 内（按音频时钟到达）收到的事件里，最大覆盖值是否满足。

| W (ms 静音窗口) | 满足句数 | 比例 |
|---|---|---|
| 200 | 0/14 | 0% |
| 300 | 2/14 | 14% |
| 500 | 6/14 | 43% |
| 700 | 10/14 | 71% |

- TurnDetector 在 speech_end+1500ms 内产生 committed 的句数: 14/14；判定延迟(音频ms) P50=520 P95=700
- 逐句事件序列: s01:waiting_continue,waiting_continue,candidate,committed；s02:waiting_continue,waiting_continue,candidate,committed；s03:waiting_continue,waiting_continue,candidate,committed；s04:waiting_continue,waiting_continue,candidate,committed；s05:waiting_continue,waiting_continue,candidate,committed；s06:waiting_continue,waiting_continue,candidate,committed；s07:waiting_continue,waiting_continue,candidate,committed；s08:waiting_continue,waiting_continue,candidate,committed；s09:waiting_continue,waiting_continue,candidate,committed；s10:waiting_continue,waiting_continue,candidate,committed；s11:waiting_continue,waiting_continue,candidate,committed；s12:waiting_continue,waiting_continue,candidate,committed；s13:waiting_continue,waiting_continue,candidate,committed；s14:waiting_continue,waiting_continue,candidate,committed

## 说明

- 全部为 SYNTHETIC_TTS，单一音色、无噪声、无口音，只能作为协议与时序的首轮先验，不能当作真实声音 PASS。
- 失败会话数: 0；握手超时后重试成功/尝试次数: 0
