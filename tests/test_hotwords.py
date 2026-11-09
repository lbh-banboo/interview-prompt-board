from app.asr.hotwords import HotwordSet, extract_hotwords, format_aliyun_vocabulary

CORPUS = [
    '我们用Kafka和Redis做消息队列，B+树索引很重要',
    '参考《设计数据密集型应用》，项目“星河平台”上线',
    'The Redis 集群用 MySQL8 做持久化',
    '联系张三 “13800138000” 邮箱 “a@b.com”',
    '缓存穿透。缓存穿透，布隆过滤器',
    '缓存穿透的方案',
    '缓存穿透。面试常问',
]


def test_exact_extraction_on_synthetic_corpus():
    hs = extract_hotwords(CORPUS, blocklist=['张三'])
    # 英文词/专名/书名号 +2 加成；Redis 出现 2 条；缓存穿透出现 3 条达 min_freq；score 同分按字典序
    assert hs.words == ['Redis', '缓存穿透', '设计数据密集型应用', 'MySQL8', 'Kafka', '星河平台', 'B+树']
    assert hs.weights == [3, 3, 2, 2, 2, 2, 2]
    assert 'The' not in hs.words and '13800138000' not in hs.words and 'a@b.com' not in hs.words


def test_dedup_case_insensitive_keeps_common_form():
    hs = extract_hotwords(['Redis 很快', 'redis 与 Redis', '用 Redis'], max_words=10)
    assert [w.lower() for w in hs.words].count('redis') == 1
    assert 'Redis' in hs.words


def test_truncate_and_length_limits():
    texts = [f'Tool{i:03d} 与 X' for i in range(600)] + ['A' * 30 + 'b1']
    hs = extract_hotwords(texts)
    assert len(hs.words) == 500 == len(hs.weights)
    assert all(2 <= len(w) <= 20 for w in hs.words)
    assert len(extract_hotwords(texts, max_words=5).words) == 5
    assert 'X' not in hs.words


def test_blocklist_substring_and_case():
    hs = extract_hotwords(['Kafka 由 李雷Kafka 使用', '《李雷的项目》'], blocklist=['李雷', '', 'KAFKA'])
    assert hs.words == []


def test_low_freq_chinese_excluded_but_quoted_kept():
    hs = extract_hotwords(['仅出现一次的片段', '“专有名”'])
    assert hs.words == ['专有名']


def test_empty_input():
    assert extract_hotwords([]) == HotwordSet([], [])


def test_format_aliyun_vocabulary():
    hs = HotwordSet(['Kafka', '星河平台'], [3, 5])
    assert format_aliyun_vocabulary(hs) == [{'text': 'Kafka', 'weight': 3}, {'text': '星河平台', 'weight': 5}]


def test_wiring_hotwords_drop_profile_alias(tmp_path):
    """热词会发给云 ASR：档案里的回答者代号不得下发；同一库无档案时该词本会入选（对照）。"""
    import json
    from test_asr_usage import wiring
    wire, store = wiring(tmp_path)
    with store.connect() as db:
        db.execute("CREATE TABLE IF NOT EXISTS profiles(id TEXT PRIMARY KEY, alias TEXT)")
        db.execute("INSERT INTO profiles(id, alias) VALUES('p1', '李雷')")
        for i in range(3):
            db.execute("INSERT INTO entries(library_id, id, payload, status, approved_answer) VALUES('lib', ?, ?, 'approved', ?)",
                       (f'e{i}', json.dumps({'question': f'题{i}'}), '李雷。缓存穿透。'))
        db.execute("INSERT INTO sessions(id, mode, status, library_id, profile_id) VALUES('with', 'online', 'ready', 'lib', 'p1')")
        db.execute("INSERT INTO sessions(id, mode, status, library_id, profile_id) VALUES('without', 'online', 'ready', 'lib', NULL)")
    assert '李雷' in wire.hotwords('without') and '缓存穿透' in wire.hotwords('without')
    assert '李雷' not in wire.hotwords('with') and '缓存穿透' in wire.hotwords('with')
