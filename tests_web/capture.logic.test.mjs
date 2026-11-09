import test from 'node:test';
import assert from 'node:assert/strict';
import { CAPTURE_HINT, resolveSession } from '../web/capture/session.js';

test('capture reuses the active setup session instead of creating one', () => {
  assert.deepEqual(resolveSession({ active_session: { id: 'abc123' } }, ''), { id: 'abc123', source: 'active' });
  assert.equal(resolveSession({ active_session: null }, ''), null);
  assert.equal(resolveSession({}, ''), null);
});

test('explicit session in URL wins over status', () => {
  assert.deepEqual(resolveSession({ active_session: { id: 'active' } }, 'linked'), { id: 'linked', source: 'query' });
});

test('missing session points back to the setup wizard, not legacy creation', () => {
  assert.match(CAPTURE_HINT, /会前配置向导/);
  assert.match(CAPTURE_HINT, /开始会话/);
});

test('after stop, capture page offers board, export and next-session links', async () => {
  const { afterStopLinks } = await import('../web/capture/session.js');
  const links = afterStopLinks('s 1');
  assert.deepEqual(links.map((l) => l.href), ['/board.html?session=s%201', '/api/sessions/s%201/export', '/setup.html']);
});
