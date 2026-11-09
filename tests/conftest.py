"""测试隔离：开发机环境里的真实 Key 不得进入任何测试（避免误连云 ASR / 大模型），本机 sherpa 模型也不在默认测试里加载。"""
import pytest


@pytest.fixture(autouse=True)
def _no_real_credentials_or_models(monkeypatch, tmp_path_factory):
    for name in ('DASHSCOPE_API_KEY', 'DEEPSEEK_API_KEY', 'SEARCH_API_KEY'):
        monkeypatch.delenv(name, raising=False)
    import app.wiring
    monkeypatch.setattr(app.wiring, 'SHERPA_DIR', tmp_path_factory.mktemp('no_models') / 'sherpa')
