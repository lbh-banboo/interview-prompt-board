#!/usr/bin/env python3
"""M07-T2 次优库种子合规扫描。用法: python tools/scan_seeds.py [目录] [--whitelist 文件] [--min 岗位=题数 ...]

检查：结构完整、示例为占位、无具体人名/公司/业绩数字、无第一人称经历、各岗题数达标。
有任何违规则打印清单并以退出码 1 结束。
"""
import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.library.secondary import PLACEHOLDER, parse_seed_text  # noqa: E402

MIN_COUNTS = {"tech": 60, "pm": 60, "ops": 60, "mgmt": 60, "general": 40}
DEFAULT_MIN = 60  # 新增岗位文件的默认下限

# 公共知识数字白名单（扫描前整段移除）；可用 --whitelist 追加，每行一个字面量
WHITELIST = ["三次握手", "四次挥手", "HTTP 状态码 200", "状态码 200", "200 OK", "404", "500", "502", "503",
             "6379", "3306", "5432", "443", "8080", "99 分位", "P99", "P95", "P50", "3NF", "2PC", "3PC",
             "CAP 三选二", "七层", "四层", "五分钟", "STAR", "OKR", "SMART", "5W2H", "5 Why", "5Why",
             "RICE", "MoSCoW", "RFC 2616", "IPv4", "IPv6", "HTTP/2", "HTTP/3", "TLS 1.3", "TLS 1.2",
             "99.9", "ACID", "BASE", "L1", "L2", "L3", "O(1)", "O(n)", "O(log n)", "Top 3", "Top3"]

COMPANIES = ["阿里", "腾讯", "字节", "百度", "美团", "京东", "华为", "滴滴", "拼多多", "小米", "网易", "快手",
             "蚂蚁", "携程", "哔哩", "微软", "谷歌", "亚马逊", "苹果", "脸书", "特斯拉", "Google", "Amazon",
             "Microsoft", "Meta", "Facebook", "Apple", "Tesla", "Netflix", "Uber", "Alibaba", "Tencent",
             "ByteDance", "Baidu", "Huawei"]
PERF = r"(?:增长|提升|提高|上升|下降|降低|减少|节省|节约|缩短|翻了?|达到|达成|超过|突破|完成)"
RULES = [
    ("百分比", re.compile(r"\d+(?:\.\d+)?\s*[%％]|百分之[零一二三四五六七八九十百\d]+")),
    ("金额", re.compile(r"[¥￥$€]\s*\d|\d+(?:\.\d+)?\s*[万亿千]?\s*(?:元|块钱|美元|美金|欧元|RMB|CNY|USD)")),
    ("具体年份", re.compile(r"(?<!\d)(?:19|20)\d{2}\s*年")),
    ("业绩数字", re.compile(PERF + r"\s*(?:了|到|至|近|约)?\s*\d")),
    ("业绩数字", re.compile(r"\d+(?:\.\d+)?\s*[万亿wW]\s*(?:用户|QPS|DAU|MAU|订单|日活|月活|GMV|营收|收入|流水)")),
    ("业绩数字", re.compile(r"(?:营收|收入|GMV|销售额|利润|业绩|转化率|留存率|DAU|MAU)\s*[为是约达]?\s*\d")),
    ("第一人称经历", re.compile(r"(?:我们?|本人)[^。；;！？\n]{0,6}(?:做过|负责|主导|带领|带过|参与过|曾经|曾|搭建过|设计过|推动过|管理过)")),
    ("第一人称经历", re.compile(r"(?:我们?|本人)在[^，。\s]{1,12}(?:公司|集团|银行|科技|团队|部门)")),
    ("具体人名", re.compile(r"[张王李赵刘陈杨黄周吴徐孙马朱胡郭何高林罗郑梁谢宋唐韩曹许邓萧冯曾程蔡彭潘袁于董余苏叶吕魏蒋田杜丁沈姜范江傅钟卢汪戴崔任陆廖姚方金邱夏谭韦贾邹石熊孟秦阎薛侯雷白龙段郝孔邵史毛常万顾赖武康贺严尹钱施牛洪龚]"
                        r"(?:总监|经理|老师|先生|女士|博士|主管|总(?![结计体量额数线共是览之部和述括纲价])|工(?![一-龥]))")),
    ("公司名", re.compile(r"[一-龥A-Za-z]{2,10}(?:有限公司|股份有限公司|有限责任公司|集团公司)")),
]
COMPANY_RE = re.compile("|".join(re.escape(c) for c in COMPANIES))


def scan_text(text, whitelist=WHITELIST):
    """全文逐行扫描，返回 [(行号, 类型, 命中片段)]。占位 [需本人补充：…] 内容先行抹除。"""
    out = []
    for no, line in enumerate(text.splitlines(), 1):
        s = re.sub(r"\[需本人补充[:：][^\[\]]*\]", "", line)
        for w in whitelist:
            s = re.sub(r"(?<![\d.])" + re.escape(w) + r"(?![\d.%％万亿元])", "", s)
        for kind, rx in RULES:
            for m in rx.finditer(s):
                out.append((no, kind, m.group(0)))
        for m in COMPANY_RE.finditer(s):
            out.append((no, "公司名", m.group(0)))
    return out


def scan_dir(directory, whitelist=WHITELIST, minimums=None):
    """返回 (违规列表[str], 统计 {文件: 题数})。"""
    mins = dict(MIN_COUNTS, **(minimums or {}))
    problems, stats, seen = [], {}, {}
    files = [p for p in sorted(Path(directory).glob("*.md")) if p.name.lower() != "readme.md"]
    if not files:
        problems.append(f"{directory}: 没有种子文件")
    for path in files:
        text = path.read_text("utf-8")
        entries, errors = parse_seed_text(text)
        problems += [f"{path.name} {i}: 结构 {m}" for i, m in errors]
        problems += [f"{path.name}:{no} {kind}: {frag}" for no, kind, frag in scan_text(text, whitelist)]
        for e in entries:
            if e["example"] and not PLACEHOLDER.match(e["example"]):
                problems.append(f"{path.name} {e['id']}: 示例非占位")
            if e["id"] in seen:
                problems.append(f"{path.name} {e['id']}: 题号与 {seen[e['id']]} 重复")
            seen[e["id"]] = path.name
        role = path.stem
        n = sum(1 for e in entries if role in e["roles"])
        stats[path.name] = n
        need = mins.get(role, DEFAULT_MIN)
        if n < need:
            problems.append(f"{path.name}: 岗位 {role} 题数 {n} < {need}")
        stray = [e["id"] for e in entries if role not in e["roles"]]
        if stray:
            problems.append(f"{path.name}: 岗位标签须包含 {role}: {','.join(stray[:5])}")
    return sorted(set(problems)), stats


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("directory", nargs="?", default=str(ROOT / "seeds" / "secondary"))
    ap.add_argument("--whitelist", help="追加白名单文件，每行一个字面量")
    ap.add_argument("--min", action="append", default=[], metavar="岗位=题数", help="覆盖题数下限，可重复")
    a = ap.parse_args(argv)
    wl = list(WHITELIST)
    if a.whitelist:
        wl += [x.strip() for x in Path(a.whitelist).read_text("utf-8").splitlines() if x.strip()]
    mins = {k: int(v) for k, v in (x.split("=") for x in a.min)}
    problems, stats = scan_dir(a.directory, wl, mins)
    for name, n in stats.items():
        print(f"{name}: {n} 题")
    for p in problems:
        print("违规:", p)
    print(f"扫描完成：{len(problems)} 项违规")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
