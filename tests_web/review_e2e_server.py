"""审核工作台 E2E 后端：真实 create_app + 假增强器，合成夹具。

stdout 首行 {"port","library"}；stdin 收到任意行即退出。
"""
import json
import socket
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

import uvicorn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.server import create_app  # noqa: E402

TEXT = '\n'.join([
    '## H1 合成章节',
    '**H1-01｜00:01｜介绍“星河”项目？** 参考回答：我负责“星河”项目，团队 8 人。',
    '**H1-02｜00:02｜“星河”项目规模？** 参考回答：在“星河”项目里带过 12 人团队。',
    '**H1-03｜00:03｜如何做技术取舍？** 参考回答：按风险与收益排序，保留回退方案。',
    '**H1-04｜00:04｜讲一次失败复盘？** 参考回答：记录事实、归因、改进项 [待核]。',
    '**H1-05｜00:05｜如何做跨部门协作？** 参考回答：先对齐目标，再拆约束与优先级。',
])


class FakeEnhancer:
    """不联网：按条目编号生成合法拆解，facts 留空，事实仍由规则抽取/校验。"""

    async def split(self, entry_text):
        key = entry_text.split('｜')[0].strip('*')
        return dict(question=f'{key} 规范问法？', question_variants=['问法一', '问法二', '问法三'],
                    answer_outline=dict(conclusion='只整理原文结论', points=['要点一', '要点二', '要点三'],
                                        examples=[], cautions=['保留 [待核]'] if '[待核]' in entry_text else []),
                    facts=[], role_tags=['技术开发'], topic_tags=['合成'])


def main():
    app = create_app(tempfile.mkdtemp(prefix='review2-e2e-'), config=dict(enhancer=FakeEnhancer()))
    store = app.state.store
    lib = store.import_text('合成样例', TEXT)['id']
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='error', access_log=False))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.02)
    urllib.request.urlopen(f'http://127.0.0.1:{port}/api/status').read()
    print(json.dumps({'port': port, 'library': lib}), flush=True)
    sys.stdin.readline()
    server.should_exit = True


if __name__ == '__main__':
    main()
