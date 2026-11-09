import pytest

from app.domain.roles import echo_dominant, role_for


@pytest.mark.parametrize('source,role', [('mic', 'answerer'), ('meeting', 'questioner'), ('room', 'unknown'), ('x', 'unknown')])
def test_online_mapping(source, role):
    assert role_for(source, 'online') == role


@pytest.mark.parametrize('source', ['mic', 'meeting', 'room'])
def test_offline_always_unknown(source):
    assert role_for(source, 'offline') == 'unknown'


def test_bad_mode():
    with pytest.raises(ValueError):
        role_for('mic', 'hybrid')


def test_echo_boundaries():
    assert echo_dominant(0.21, 0.1, 2.0)
    assert not echo_dominant(0.2, 0.1, 2.0)  # 恰等于比值，不算显著
    assert not echo_dominant(0.1, 0.1)
    assert echo_dominant(0.01, 0.0)  # meeting 静音
    assert not echo_dominant(0.0, 0.0)
    assert echo_dominant(0.3, 0.1, ratio=2.5)
    assert not echo_dominant(0.3, 0.1, ratio=3.0)
