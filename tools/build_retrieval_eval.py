"""M08-7 检索评测集构建：基于主库 226 条生成库内同义问法 + 自编库外题。

约束：纯规则式改写（同义替换/口语化/语气词），不调用任何大模型、不联网；只读本地库；
库外题全部为本文件内自编文本。输出 eval/retrieval/eval_set.json。
改写由规则生成，难度低于真实面试/ASR 转写，结果不能外推到真实语音链路。
"""
import argparse
import hashlib
import json
import random
import re
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_OF_LIBRARY = "OUT_OF_LIBRARY"

# (原词, [替换词]) 同义换说法
SWAPS = [
    ("为什么", ["为啥", "出于什么原因"]), ("怎么", ["如何", "怎样"]), ("如何", ["怎么", "怎样"]),
    ("怎样", ["怎么", "如何"]), ("具体", ["详细", "究竟"]), ("是否", ["是不是"]), ("有没有", ["是否有", "有无"]),
    ("你们", ["你们团队"]), ("目前", ["现在"]), ("现在", ["目前"]), ("最大的", ["最主要的"]), ("最大", ["最主要"]),
    ("困难", ["难点"]), ("难点", ["困难"]), ("区别", ["不同"]), ("介绍", ["讲讲"]), ("评测", ["评估"]),
    ("评估", ["评测"]), ("负责", ["主导"]), ("优势", ["长处"]), ("短板", ["不足"]), ("方案", ["做法"]),
    ("哪家", ["哪个公司"]), ("多少", ["大概多少"]), ("什么", ["哪些"]), ("离职", ["离开公司"]), ("做过", ["做过的"]),
    ("你说的", ["你刚才提到的"]), ("你提到的", ["你刚说的"]), ("吗", ["么"]), ("对吗", ["是吧"]), ("对吧", ["是吗"]),
    ("还是", ["或者是"]), ("遇到", ["碰到"]), ("解决", ["处理"]), ("项目", ["工作"]),
]
PREFIXES = ["请问，", "那个，", "嗯，我想问一下，", "接着问一下，", "就是说，", "我想了解下，"]
SUFFIXES = ["呢？", "，能讲讲吗？", "，可以说说吗？", "，是这样吗？", "，你怎么看？"]
HOMOPHONES = {"的": "地", "做": "作", "在": "再", "那": "哪", "是": "事", "以": "已", "型": "形", "模": "膜",
              "点": "店", "营": "迎", "销": "消", "么": "吗", "到": "道", "里": "理", "期": "其", "势": "是",
              "度": "渡", "法": "发", "户": "护", "单": "弹", "评": "平", "测": "册", "项": "向", "目": "木"}

# ---- 库外题（自编，主题与主库 226 条不重叠）----
OOS_GENERAL = """TCP 三次握手的过程是什么
HTTP 和 HTTPS 的区别是什么
快速排序的时间复杂度是多少
什么是红黑树
MySQL 的索引为什么用 B+ 树
什么是死锁，如何避免
进程和线程有什么区别
Redis 的持久化方式有哪些
什么是缓存穿透
CAP 定理说的是什么
Kubernetes 的 Pod 是什么
Docker 镜像和容器有什么区别
什么是 RESTful 接口
垃圾回收算法有哪几种
什么是哈希冲突
Python 的 GIL 是什么
数据库事务的 ACID 指什么
一致性哈希解决什么问题
消息队列如何保证不丢消息
什么是微服务的熔断
递归和迭代的区别
二叉树的前序遍历怎么写
什么是内存泄漏
Linux 下怎么查看端口占用
Git 的 rebase 和 merge 有什么不同
什么是 CDN
DNS 解析的流程是什么
什么是跨域，怎么解决
JavaScript 的闭包是什么
什么是单元测试覆盖率
光合作用的基本过程是什么
通货膨胀是怎么形成的
牛顿第一定律的内容是什么
中国的四大发明是什么
地球为什么有四季
复利是怎么计算的
什么是边际效用递减
水的沸点为什么随气压变化
第二次世界大战是哪一年结束的
什么是供给侧改革
勾股定理怎么证明
黄金为什么能保值
什么是量化宽松
光速是多少
细胞分裂分为哪几个阶段
唐朝是怎样走向衰落的
什么是博弈论中的囚徒困境
如何计算企业的折旧
资产负债表包含哪些项目
GDP 和 GNP 有什么区别
元素周期表是谁提出的""".splitlines()

OOS_BEHAVIOR = """你每天通常怎么安排自己的时间
你怎么看待加班这件事
你喜欢团队合作还是独立工作
你最近读过什么书
你是怎么放松和减压的
遇到紧急任务同时到来你怎么排优先级
你更看重工作稳定还是挑战
你怎么看待远程办公
你理想中的上司是什么样的
你如何记笔记和整理资料
你如何面对批评
你认为诚信对工作重要吗
如果同事总迟到你会怎么做
你觉得自己是内向还是外向
你是怎么保持健康作息的
你对公司的企业文化有什么期望
你会怎么处理工作中的情绪
你怎么看待职场中的竞争""".splitlines()

OOS_DOMAIN = """什么是私域流量
RFM 模型怎么划分用户
漏斗转化分析怎么做
ROI 和 ROAS 有什么区别
归因模型有哪几种
Transformer 的自注意力机制是什么
RAG 的基本流程是什么
什么是 embedding
LoRA 微调的原理是什么
temperature 参数有什么作用
什么是大模型幻觉
Function Calling 是什么
常见的向量数据库有哪些
什么是思维链提示
RLHF 是什么
token 是怎么计算的
什么是 KOL 营销
用户生命周期价值怎么计算
NPS 净推荐值是什么
什么是增长黑客
SEO 和 SEM 的区别
卷积神经网络用来做什么
什么是长尾效应
用户画像一般包含哪些维度
PRD 文档应该包含哪些部分
什么是 MVP 最小可行产品
敏捷开发和瀑布模型的区别
什么是 OKR""".splitlines()

OOS_PERSONAL = """你大学读的什么专业
你的家乡在哪里
你有什么兴趣爱好
你的英语水平怎么样
你有驾照吗
你结婚了吗
你平时喜欢运动吗
你上一次旅行去了哪里
你最喜欢的电影是什么
你住在城市的哪个区
你会做饭吗
你养宠物吗""".splitlines()

PROCESS = """我们开始吧
能听到我说话吗
稍等一下我看看材料
好的，明白了
先让我喝口水
你那边画面能看到吗
我们换个话题吧
好，今天就先到这里
网络有点卡，你再说一遍
麻烦把摄像头打开
先跟你确认一下时间
好的，谢谢你的回答
不好意思刚才信号断了
我这边先记录一下
咱们进入下一个环节""".splitlines()

# 短词：(词, 期望条目由“问题包含该词”自动推出；无命中则 OUT_OF_LIBRARY)
SHORT_TERMS = ["MA", "MCP", "Agent", "Prompt", "LangChain", "QA", "LLM", "Skill", "KPI", "豆包", "火山", "千问",
               "扣子", "次卡", "复购", "年金", "安克", "泰康", "有道", "闲鱼", "CDP", "C 端",
               "缓存", "CAP", "索引", "栈", "堆", "锁", "SQL", "Java", "HTTP", "Git", "天气", "足球", "奶茶"]


def load_entries(db_path):
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    rows = []
    for (p,) in con.execute("SELECT payload FROM entries ORDER BY rowid"):
        d = json.loads(p)
        rows.append({"id": d["id"], "question": d["question"]})
    return rows


def _swap(q, rng, max_swaps=3, avoid=None):
    cands = [(a, rng.choice(b)) for a, b in SWAPS if a in q]
    rng.shuffle(cands)
    used, out = [], q
    for a, b in cands:
        if a in out and len(used) < max_swaps and not any(a in u or u in a for u, _ in used):
            out = out.replace(a, b, 1)
            used.append((a, b))
    return out, [f"swap:{a}->{b}" for a, b in used]


def paraphrases(entry, rng):
    q = entry["question"].replace("[说话人待核] ", "")
    base = q.rstrip("？?")
    out, seen = [], {q}
    # 规则 1：同义换说法（至多 3 处）
    s, rules = _swap(q, rng)
    if s != q:
        out.append((s, rules))
        seen.add(s)
    # 规则 2：口语化（加前缀/语气词，必要时去掉“你”）+ 至多 1 处换说法
    for _ in range(8):
        b, r = _swap(base + "？", rng, max_swaps=1)
        b = b.rstrip("？")
        pre = rng.choice(PREFIXES)
        suf = rng.choice(SUFFIXES)
        if rng.random() < 0.35 and "你" in b:
            b = b.replace("你", "", 1)
            r = r + ["drop:你"]
        s = pre + b + suf
        if s not in seen:
            out.append((s, r + [f"prefix:{pre}", f"suffix:{suf}"]))
            seen.add(s)
            break
    if len(out) < 2:  # 没有可替换词时，再补一个纯口语化版本
        for pre in PREFIXES:
            s = pre + base + "？"
            if s not in seen:
                out.append((s, [f"prefix:{pre}"]))
                break
    return out[:2]


def asr_noise(q, rng):
    chars, changed = [], False
    for c in q.replace("[说话人待核] ", ""):
        r = rng.random()
        if c in HOMOPHONES and r < 0.15:
            chars.append(HOMOPHONES[c]); changed = True
        elif r > 0.965 and c not in "？，":
            changed = True
        else:
            chars.append(c)
    s = "".join(chars).replace("？", "").replace("，", "")
    return s if changed else s[:-1]


def build(entries, seed=20261029):
    items = []

    def add(**kw):
        kw["uid"] = f"E{len(items) + 1:04d}"
        items.append(kw)

    same = {}
    for e in entries:
        same.setdefault(re.sub(r"\W", "", e["question"].lower()), []).append(e["id"])
    equiv = {i: [j for j in ids if j != i] for ids in same.values() for i in ids}
    for e in entries:
        r = random.Random(f"{seed}-{e['id']}")
        for text, rules in paraphrases(e, r):
            add(type="paraphrase", noise="none", query=text, expected=e["id"], equivalent_ids=equiv[e["id"]], rules=rules)
    for e in entries:
        r = random.Random(f"{seed}-asr-{e['id']}")
        add(type="paraphrase", noise="synthetic_asr", query=asr_noise(e["question"], r), expected=e["id"],
            equivalent_ids=equiv[e["id"]], rules=["synthetic:homophone+drop"])
    groups = [("general", OOS_GENERAL), ("behavior", OOS_BEHAVIOR), ("domain_adjacent", OOS_DOMAIN),
              ("personal", OOS_PERSONAL)]
    for sub, lines in groups:
        for t in lines:
            add(type="oos", subtype=sub, noise="none", query=t + "？", expected=OUT_OF_LIBRARY)
    for t in PROCESS:
        add(type="process", noise="none", query=t, expected=OUT_OF_LIBRARY)
    for term in SHORT_TERMS:
        ids = [e["id"] for e in entries if term.lower() in e["question"].lower()]
        add(type="short_term", noise="none", query=term, expected=ids if ids else OUT_OF_LIBRARY,
            expected_scope="any_of" if ids else "none")
    return items


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT / "data" / "interview.sqlite3"))
    ap.add_argument("--out", default=str(ROOT / "eval" / "retrieval" / "eval_set.json"))
    a = ap.parse_args()
    entries = load_entries(a.db)
    items = build(entries)
    lib_hash = hashlib.sha256(json.dumps([[e["id"], e["question"]] for e in entries], ensure_ascii=False).encode()).hexdigest()[:12]
    counts = {}
    for it in items:
        k = it["type"] + (":" + it["subtype"] if "subtype" in it else "") + (":" + it["noise"] if it["noise"] != "none" else "")
        counts[k] = counts.get(k, 0) + 1
    doc = {
        "category": "ALGO_REPLAY（文本侧检索评测，非语音链路）",
        "provenance": "基于本地主库 226 条问题，规则式改写生成；库外题全部自编；未调用任何大模型，未联网，未外发条目文本。",
        "limitations": "规则改写不等于真实面试问法；synthetic_asr 为合成噪声（同音字替换/漏字），不等于真实 ASR 转写误差。",
        "library_hash": lib_hash, "library_size": len(entries), "counts": counts, "items": items,
    }
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", "utf-8")
    print(json.dumps(counts, ensure_ascii=False))


if __name__ == "__main__":
    main()
