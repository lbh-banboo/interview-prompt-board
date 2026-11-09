"""次优库（L2）索引：会话开始时按岗位并集构建，只含 status=approved 的条目；满足 Router 的 SecondaryIndex 协议。

Index 持有 sqlite 连接，必须在 IndexProvider.executor 线程里建立和查询。
"""
import logging

from ..retrieval.index import Index
from ..retrieval.search import search

log = logging.getLogger('app')
# 会话岗位（中文多选项，app.sessions.setup.ROLES）-> 次优库岗位标签；未登记的原样当标签用
ROLE_TAGS = {'技术开发': 'tech', '产品经理': 'pm', '运营': 'ops', '管理': 'mgmt'}


def role_tags(roles):
    return sorted({ROLE_TAGS.get(r, r) for r in roles or []})


class SecondaryIndex:
    """index 为 None（无已审核条目）时恒不命中。cards：条目 id -> 次优库条目原文（结论/要点/示例/注意）。"""

    def __init__(self, index, cards):
        self.index, self.cards = index, cards

    def search(self, query, roles=None):
        if self.index is None or not self.index.size:
            return []
        return sorted(search(self.index, query).hits, key=lambda h: (-h.cos, h.entry_id))


def build_secondary(library, roles, embedder):
    """library：SecondaryLibrary；失败只记日志并返回空索引（L2 不命中，不影响 L1/L3）。"""
    try:
        tags = role_tags(roles)
        entries = library.index_entries(tags)
        if not entries:
            return SecondaryIndex(None, {})
        index = Index(embedder)
        index.build([dict(id=e['id'], question=e['question'], variants=e['variants']) for e in entries])
        return SecondaryIndex(index, {e['id']: e for e in library.candidates(tags)})
    except Exception:
        log.exception('次优库索引构建失败')
        return SecondaryIndex(None, {})
