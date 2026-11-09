"""M10-T1 脱敏单测：全部合成数据。"""
import pytest

from app.privacy import redact
from app.privacy.redact import DEFAULT_RULES, _split_vocab

VOCAB = {"names": ["张三丰"], "companies": ["星河科技"], "projects": ["天枢系统"], "terms": ["内部灰度网关"]}


def test_name_company_project_removed():
    r = redact("张三丰在星河科技做的天枢系统，Redis 持久化怎么做", VOCAB)
    assert not r.blocked
    for w in ("张三丰", "星河科技", "天枢系统"):
        assert w not in r.text
    assert "Redis 持久化怎么做" in r.text
    assert r.hits.count("vocab") == 3


def test_plain_iterable_vocab_and_case_insensitive():
    r = redact("ACME 的 Kafka 分区", ["acme", " ", ""])
    assert "ACME" not in r.text and "Kafka 分区" in r.text


def test_single_char_alias_not_replaced_and_warns(caplog):
    with caplog.at_level("WARNING", logger="app.privacy.redact"):
        r = redact("是这个意思吗？Redis 持久化怎么做", {"names": ["是"]})
    assert "是" in r.text and "Redis 持久化怎么做" in r.text
    assert "vocab" not in r.hits and not r.blocked
    assert any("单字别名" in rec.getMessage() for rec in caplog.records)


def test_multi_char_alias_still_removed():
    r = redact("张三 问 Redis 持久化", {"names": ["张三"]})
    assert "张三" not in r.text and "Redis 持久化" in r.text
    assert r.hits.count("vocab") == 1


@pytest.mark.parametrize("raw,kind", [
    ("联系 13812345678 问 Redis", "phone"),
    ("手机 +86 138-1234-5678 问 Redis", "phone"),
    ("邮箱 foo.bar@corp.com 问 Redis", "email"),
    ("身份证 11010519491231002X 问 Redis", "id_card"),
    ("卡号 6222021234567890123 问 Redis", "bank_card"),
    ("在华为技术有限公司 问 Redis", "company"),
    ("at Acme Widgets Inc. ask Redis", "company"),
])
def test_pii_removed(raw, kind):
    r = redact(raw)
    assert kind in r.hits
    assert "Redis" in r.text
    for leak in ("13812345678", "138-1234-5678", "foo.bar", "11010519491231002X", "6222021234567890123", "Acme", "华为"):
        assert leak not in r.text


def test_url_user_identifiers():
    r = redact("看 https://bob:pw@site.com/user/alice/post?uid=42&page=2 和 https://x.com/a?token=abc 的写法")
    assert "bob" not in r.text and "alice" not in r.text and "uid=42" not in r.text and "token" not in r.text
    assert "page=2" in r.text and "site.com" in r.text
    assert r.hits.count("url_user") == 2


def test_url_without_user_info_untouched():
    r = redact("参考 https://docs.python.org/3/library/asyncio.html")
    assert "https://docs.python.org/3/library/asyncio.html" in r.text
    assert "url_user" not in r.hits


@pytest.mark.parametrize("q", ["Redis 默认端口 6379", "TCP 三次握手", "时间戳 1700000000000 是什么", "HTTP 状态码 502 含义", "什么是 B+ 树"])
def test_no_false_positive(q):
    r = redact(q)
    assert r.text == q and not r.hits and not r.blocked


@pytest.mark.parametrize("q", [
    "你负责的项目里怎么做缓存", "你们项目用什么架构", "你做过高并发吗", "请做一下自我介绍",
    "Tell me about yourself", "Have you ever used Kafka", "你的经历里最难的问题",
])
def test_personal_intent_blocked(q):
    r = redact(q)
    assert r.blocked and r.reason == "personal_or_private"


def test_knowledge_question_with_ni_not_blocked():
    assert not redact("你知道 Redis 持久化吗").blocked


def test_term_in_vocab_blocks_and_not_removed():
    r = redact("内部灰度网关 的限流怎么做", VOCAB)
    assert r.blocked and r.reason == "personal_or_private"


def test_empty_after_redact_blocked():
    r = redact("13812345678")
    assert r.blocked and r.text == ""


def test_empty_input_and_no_vocab():
    r = redact(None)
    assert r.text == "" and not r.blocked and r.hits == []
    assert _split_vocab(None) == ([], [])


def test_rules_configurable_and_pure():
    q = "手机 13812345678"
    assert redact(q, rules=()).text == q
    assert redact("保密词 foo", personal_patterns=(r"保密词",)).blocked
    assert redact(q).text == redact(q).text
    assert len(DEFAULT_RULES) == 5
