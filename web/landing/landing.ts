// 欢迎首页行为：背景视频 × prefers-reduced-motion、统计数字（真实接口 + count-up）、
// 最近会话、汉堡菜单。数据取不到时显示 —，接口失败静默降级，不影响页面渲染。
import {
  buildStats, recentSessions, menuState, shouldCloseMenuOnResize,
  countUpValue, finalStatValue,
} from './logic.js';
import { TOP_ITEMS } from '../nav/logic.js';

/* ---------- 0) 顶栏导航与全站共享同一份导航项数据 ---------- */
function alignNavItems(): void {
  const groups = ['.site-header .nav-link', '.mobile-menu .m-link'];
  for (const selector of groups) {
    const links = Array.from(document.querySelectorAll<HTMLAnchorElement>(selector));
    TOP_ITEMS.forEach((item, i) => {
      const a = links[i];
      if (!a) return;
      a.textContent = item.label;
      a.setAttribute('href', item.href);
    });
  }
}
alignNavItems();

const reducedMQ = window.matchMedia('(prefers-reduced-motion: reduce)');

/* ---------- 1) 背景视频 ---------- */
const bgVideo = document.querySelector('video.bg-video') as HTMLVideoElement | null;

function syncVideoToMotionPreference(): void {
  if (!bgVideo) return;
  if (reducedMQ.matches) {
    bgVideo.pause();
    bgVideo.removeAttribute('autoplay');
  } else if (bgVideo.paused) {
    bgVideo.setAttribute('autoplay', '');
    const p = bgVideo.play();
    if (p && typeof p.catch === 'function') p.catch(() => {});  // autoplay 被拦：保留海报帧
  }
}
syncVideoToMotionPreference();
if (typeof reducedMQ.addEventListener === 'function') {
  reducedMQ.addEventListener('change', syncVideoToMotionPreference);
} else if ('addListener' in reducedMQ) {
  (reducedMQ as MediaQueryList & { addListener(cb: () => void): void }).addListener(syncVideoToMotionPreference);
}

/* ---------- 2) 统计数字 ---------- */
const reduced = (): boolean => reducedMQ.matches;

async function getJSON(url: string): Promise<unknown> {
  try {
    const res = await fetch(url);
    return res.ok ? await res.json() : null;
  } catch {
    return null;
  }
}

interface CountJob { el: HTMLElement; index: number }
const countJobs: CountJob[] = [];

function countUp(el: HTMLElement, index: number): void {
  const target = parseFloat(el.dataset.target || '0') || 0;
  if (reduced()) {
    el.textContent = finalStatValue(target);
    return;
  }
  const duration = 1500 + index * 80;
  const startDelay = 480 + index * 90;
  let startedAt: number | null = null;
  function frame(now: number): void {
    if (startedAt === null) startedAt = now;
    const elapsed = now - startedAt;
    const t = elapsed >= duration ? 1 : elapsed / duration;
    el.textContent = countUpValue(target, t);
    if (t < 1) requestAnimationFrame(frame);
    else el.textContent = finalStatValue(target);
  }
  window.setTimeout(() => requestAnimationFrame(frame), startDelay);
}

function startCountUps(): void {
  const els = Array.from(document.querySelectorAll<HTMLElement>('.stat-value'));
  const jobs = els
    .map((el) => ({ el, index: els.indexOf(el) }))
    .filter(({ el }) => el.dataset.target !== undefined);
  if (!('IntersectionObserver' in window) || reduced()) {
    jobs.forEach(({ el, index }) => countUp(el, index));
    return;
  }
  const seen = new WeakSet<Element>();
  const io = new IntersectionObserver((entries) => {
    entries.forEach((entry) => {
      if (!entry.isIntersecting || seen.has(entry.target)) return;
      seen.add(entry.target);
      io.unobserve(entry.target);
      const job = jobs.find((j) => j.el === entry.target);
      if (job) countUp(job.el, job.index);
    });
  }, { threshold: 0.25 });
  jobs.forEach(({ el }) => io.observe(el));
}

function renderStats(data: { libraries: unknown; secondary: unknown; sessions: unknown; status: unknown }): void {
  const stats = buildStats({
    libraries: data.libraries as never,
    secondary: data.secondary as never,
    sessions: data.sessions as never,
    status: data.status as never,
  });
  stats.forEach((s) => {
    const el = document.querySelector<HTMLElement>(`[data-stat="${s.key}"]`);
    if (!el) return;
    if (s.value === null) {  // 接口失败或文本项：直接显示文字（— 或 转写方式）
      el.textContent = s.text;
      return;
    }
    if (reduced()) {
      el.textContent = finalStatValue(s.value);
      return;
    }
    el.dataset.target = String(s.value);
  });
  startCountUps();
}

/* ---------- 3) 最近会话 ---------- */
function renderRecent(sessions: unknown): void {
  const section = document.getElementById('recent');
  const list = document.getElementById('recentList');
  if (!section || !list) return;
  const items = recentSessions(sessions as never);
  if (!items.length) {
    section.hidden = true;
    return;
  }
  list.replaceChildren(...items.map((it) => {
    const li = document.createElement('li');
    const a = document.createElement('a');
    a.className = 'recent-item';
    a.href = it.href;
    const time = document.createElement('span');
    time.className = 'r-time';
    time.textContent = it.timeLabel;
    const mode = document.createElement('span');
    mode.className = 'r-mode';
    mode.textContent = it.modeLabel;
    const status = document.createElement('span');
    status.className = 'r-status';
    status.textContent = it.statusLabel;
    a.append(time, mode, status);
    li.append(a);
    return li;
  }));
  section.hidden = false;
}

void (async () => {
  const [libraries, secondary, sessions, status] = await Promise.all([
    getJSON('/api/libraries'),
    getJSON('/api/secondary/progress'),
    getJSON('/api/sessions'),
    getJSON('/api/status'),
  ]);
  renderStats({ libraries, secondary, sessions, status });
  renderRecent(sessions);
})();

/* ---------- 4) 汉堡菜单 ---------- */
const burger = document.getElementById('burger');
const overlay = document.getElementById('mobileOverlay');

if (burger && overlay) {
  const menu = document.getElementById('mobileMenu');
  const lock = document.querySelector('.page');

  function applyMenu(open: boolean): void {
    const state = menuState(open);
    burger!.setAttribute('aria-expanded', state.expanded);
    burger!.setAttribute('aria-label', state.label);
    burger!.classList.toggle('is-open', open);
    overlay!.hidden = state.overlayHidden;
    overlay!.setAttribute('aria-hidden', open ? 'false' : 'true');
    document.body.classList.toggle('menu-open', open);
    if (lock) lock.setAttribute('aria-hidden', open ? 'true' : 'false');
  }

  burger.addEventListener('click', () => {
    applyMenu(burger.getAttribute('aria-expanded') !== 'true');
  });

  overlay.addEventListener('click', (event) => {
    if (event.target === overlay) applyMenu(false);
  });

  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && burger.getAttribute('aria-expanded') === 'true') {
      applyMenu(false);
      burger.focus();
    }
  });

  if (menu) {
    menu.addEventListener('click', (event) => {
      if ((event.target as Element).closest('a')) applyMenu(false);
    });
  }

  window.addEventListener('resize', () => {
    if (shouldCloseMenuOnResize(burger.getAttribute('aria-expanded') === 'true', window.innerWidth)) {
      applyMenu(false);
    }
  });

  applyMenu(false);
}
