"""check_secrets 只报位置、不打印值；测试全部使用临时目录与伪造 Key。"""
from tools import check_secrets


def test_pattern_hit_reports_location_without_value(tmp_path):
    fake = 'sk-' + 'A1' * 12
    (tmp_path / 'a.py').write_text(f'x = 1\nKEY = "{fake}"\n', 'utf-8')
    (tmp_path / 'b.txt').write_text('clean\n', 'utf-8')
    hits = check_secrets.scan(tmp_path, values={})
    assert [(h.path.name, h.line, h.kind) for h in hits] == [('a.py', 2, 'sk- 形态 Key')]
    out = '\n'.join(check_secrets.format_hits(hits, tmp_path))
    assert 'a.py:2' in out and fake not in out


def test_pragma_allowlists_fixture_pattern_but_not_real_values(tmp_path):
    (tmp_path / 'fixture.py').write_text('KEY = "sk-' + 'FAKE' * 5 + '"  # pragma: allowlist secret\n', 'utf-8')
    assert check_secrets.scan(tmp_path, values={}) == []

    real = 'fake-env-value-0123456789'
    (tmp_path / 'leak.txt').write_text(f'DASHSCOPE_API_KEY={real}  # pragma: allowlist secret\n', 'utf-8')
    hits = check_secrets.scan(tmp_path, values={'DASHSCOPE_API_KEY': real})
    assert [(h.path.name, h.kind) for h in hits] == [('leak.txt', 'DASHSCOPE_API_KEY 的实际值')]
    assert real not in '\n'.join(check_secrets.format_hits(hits, tmp_path))


def test_skips_data_venv_and_binary(tmp_path):
    for d in ('data', '.venv', 'node_modules'):
        (tmp_path / d).mkdir()
        (tmp_path / d / 'k.txt').write_text('sk-' + 'B' * 20, 'utf-8')
    (tmp_path / 'blob.onnx').write_bytes(b'sk-' + b'C' * 20)
    (tmp_path / 'bin.dat').write_bytes(b'\x00sk-' + b'D' * 20)
    assert check_secrets.scan(tmp_path, values={}) == []


def test_dotenv_values_are_used_for_comparison(tmp_path, monkeypatch):
    (tmp_path / 'data').mkdir()
    (tmp_path / 'data' / '.env').write_text('DEEPSEEK_API_KEY="fake-dotenv-value-12345"\n', 'utf-8')
    (tmp_path / 'README.md').write_text('见到 fake-dotenv-value-12345 就是泄漏\n', 'utf-8')
    hits = check_secrets.scan(tmp_path)
    assert [(h.path.name, h.line, h.kind) for h in hits] == [('README.md', 1, 'DEEPSEEK_API_KEY 的实际值')]


def test_main_exit_codes_and_output_never_contains_value(tmp_path, capsys):
    assert check_secrets.main([str(tmp_path)]) == 0
    fake = 'sk-' + 'E9' * 10
    (tmp_path / 'leak.py').write_text(f'K = "{fake}"\n', 'utf-8')
    assert check_secrets.main([str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert 'leak.py:1' in out and fake not in out
    assert check_secrets.main([str(tmp_path / 'missing')]) == 2
