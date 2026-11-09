import { BoardClient } from './board/client.js';
import { keyAction, nextScale } from './board/keys.js';
import { Renderer } from './board/render.js';

async function main(): Promise<void> {
  const status = await (await fetch('/api/status')).json();
  const session = new URLSearchParams(location.search).get('session') || status.active_session?.id;
  if (!session) { document.getElementById('qtext')!.textContent = '没有进行中的会话'; return; }
  const renderer = new Renderer();
  const client: BoardClient = new BoardClient(session, status.token, {
    onState: (s, connected) => renderer.render(s, connected),
    onBanner: (text) => { renderer.setBanner(text); if (client.state) renderer.render(client.state, true); },
    sendAck: (ws, promptId) => ws.send(JSON.stringify({ type: 'display_ack', prompt_id: promptId, t_visible: performance.timeOrigin + performance.now() })),
  });
  renderer.onPromptShown = (id) => client.ack(id);

  let scale = 0;
  document.addEventListener('keydown', (e) => {
    const cand = client.state?.vadCandidate;
    const action = keyAction(e, !!cand);
    if (!action) return;
    if (action === 'accept_candidate') {
      e.preventDefault();
      if (cand) client.acceptCandidate(cand.candidate_id);
    } else if (action === 'toggle_hide') {
      document.body.classList.toggle('is-hidden');  // 只切换显示，WS 与渲染照常
    } else {
      scale = nextScale(scale, action);
      document.documentElement.dataset.scale = String(scale);
    }
  });
  await client.start();
}

void main();
