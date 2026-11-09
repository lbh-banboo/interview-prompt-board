"""M09 事实校验器：对抗集逐条断言（合成数据，eval/adversarial/cases.json）。"""
import json
from pathlib import Path

import pytest

from app.domain.facts import (PLACEHOLDER, ApprovedEntry, Block, Sentence, numbers, validate)

DATA = json.loads((Path(__file__).parent.parent / 'eval' / 'adversarial' / 'cases.json').read_text('utf-8'))
ENTRIES = [ApprovedEntry(e['entry_id'], e['text'], tuple(e['facts']), e['tier']) for e in DATA['entries']]


def to_block(b):
    if 'sentences' in b:
        return Block(b['kind'], tuple(Sentence(s['text'], s.get('entry_id')) for s in b['sentences']))
    if b['kind'] == 'personal':
        return Block.personal(b['entry_id'], b['text'])
    if b['kind'] == 'public':
        return Block.public(b['text'])
    return Block(b['kind'], (Sentence(b['text']),))


def test_adversarial_set_size_and_coverage():
    cases = DATA['cases']
    assert len(cases) >= 30
    assert len({c['id'] for c in cases}) == len(cases)
    assert {'T1', 'T3', 'T4', 'T5', 'T6', 'T7'} <= {c['task_case'] for c in cases}


@pytest.mark.parametrize('case', DATA['cases'], ids=lambda c: c['id'])
def test_adversarial_case(case):
    res = validate([to_block(b) for b in case['blocks']], ENTRIES, case['spoken'])
    exp = case['expect']
    assert res.ok is exp['ok']
    assert res.dropped_blocks == exp['dropped']
    assert [r.code for r in res.reasons] == exp['reasons']
    assert len(res.hints) == exp['hints']
    assert all(not h.verified for h in res.hints)
    # 被丢的个人块只可能以占位出现，原文绝不进入可推送块
    pushed = ''.join(b.text for b in res.blocks)
    for i in res.dropped_blocks:
        assert to_block(case['blocks'][i]).text not in pushed or not to_block(case['blocks'][i]).text


def test_dropped_personal_replaced_by_placeholder():
    res = validate([Block.personal('e_order', '成本降低了40%。')], ENTRIES)
    assert [b.kind for b in res.blocks] == ['placeholder']
    assert res.blocks[0].text == PLACEHOLDER


def test_spoken_never_becomes_trusted_fact():
    # 口述数字即使重复出现也不能让个人块通过
    res = validate([Block.personal('e_order', '成本降低了40%。')], ENTRIES, ['成本降低了40%', '成本降低了40%'])
    assert not res.ok and res.reasons[0].code == 'spoken_only'


def test_hint_deduplicated_and_unverified():
    blk = Block('personal', (Sentence('接口响应时间从800ms降到200ms。', 'e_order'),
                             Sentence('接口响应时间降到200ms。', 'e_order')))
    res = validate([blk], ENTRIES, ['接口响应时间降到了100ms'])
    assert len(res.hints) == 1 and '不一致' in res.hints[0].text


def test_dict_facts_and_whitespace_normalization():
    e = ApprovedEntry('x1', '', ({'text': '日活 120 万'},))
    assert validate([Block.personal('x1', '日活120万。')], [e]).ok


def test_numbers_extraction():
    assert numbers('提升了百分之三十，1,000人，十二个，三年，一个项目，两倍，一个月') == {
        ('30', True), ('1000', False), ('12', False), ('3', False), ('2', False), ('1', False)}
    assert numbers('增长12.5%') == {('12.5', True)}
    assert ('100', False) in numbers('一百人') and ('1', False) not in numbers('一个人')
