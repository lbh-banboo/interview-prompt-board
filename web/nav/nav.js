// gnav：全站顶栏（白色圆形 logo + 白色胶囊导航 + 更多下拉 + 窄屏汉堡菜单）。
// 由各页以 <script type="module" src="/nav/nav.js"> 引入；模块脚本 defer 执行，
// 顶栏注入到 body 最前面；脚本未执行时页面原有内容仍可用。
import { TOP_ITEMS, buildMoreItems, matchRoute, activeSessionId, liveHref, needsLeaveConfirm, LEAVE_CONFIRM_TEXT, burgerState, shouldCloseMenuOnResize, } from './logic.js';
/* ---------- 首页自带顶栏：跳过注入 ---------- */
if (document.querySelector('.site-header') || document.body.hasAttribute('data-no-gnav') || document.getElementById('gnav')) {
    // 已有顶栏或显式禁用，保持页面原样。
}
else {
    init();
}
function init() {
    const route = matchRoute(location.pathname + location.search);
    const topLink = (item) => {
        const current = route.top === item.key;
        return `<a class="gnav-link${current ? ' is-active' : ''}" href="${item.href}"`
            + ` data-gnav-top="${item.key}" data-gnav-link`
            + `${current ? ' aria-current="page"' : ''}>${item.label}</a>`;
    };
    const moreLink = (item) => {
        const current = route.more === item.key;
        const live = item.key === 'live';
        return `<a class="gnav-more-item${current ? ' is-current' : ''}${live ? ' is-off' : ''}" href="${item.href}"`
            + ` role="menuitem" data-gnav-more="${item.key}" data-group="${item.group}" data-gnav-link`
            + `${live ? ' data-gnav-live-more' : ''}`
            + `${current ? ' aria-current="page"' : ''}>`
            + `<span>${item.label}</span>`
            + `${live ? '<span class="gnav-tag" data-gnav-live-tag>无进行中</span>' : ''}</a>`;
    };
    const sheetTop = (item) => {
        const current = route.top === item.key;
        return `<a class="gnav-m-item${current ? ' is-active' : ''}" href="${item.href}"`
            + ` data-gnav-mobile-top="${item.key}" data-gnav-link`
            + `${current ? ' aria-current="page"' : ''}>`
            + `<span>${item.label}</span>${current ? '<span class="gnav-m-dots" aria-hidden="true"></span>' : ''}</a>`;
    };
    const sheetMore = (item) => {
        const current = route.more === item.key;
        const live = item.key === 'live';
        return `<a class="gnav-m-item${current ? ' is-active' : ''}${live ? ' is-off' : ''}" href="${item.href}"`
            + ` data-gnav-mobile-more="${item.key}" data-gnav-link`
            + `${live ? ' data-gnav-live-more' : ''}`
            + `${current ? ' aria-current="page"' : ''}>`
            + `<span>${item.label}</span>`
            + `${live ? '<span class="gnav-tag" data-gnav-live-tag>无进行中</span>' : ''}</a>`;
    };
    const bar = document.createElement('div');
    bar.className = 'gnav';
    bar.id = 'gnav';
    bar.setAttribute('data-gnav', '');
    bar.innerHTML = `
    <div class="gnav-in">
      <a class="gnav-logo" href="/" aria-label="面试提示板 · 首页" data-gnav-top="home" data-gnav-link>
        <img src="/landing/assets/logo/logo.webp" alt="" width="32" height="32">
      </a>
      <nav class="gnav-pill" aria-label="主导航">
        ${TOP_ITEMS.map(topLink).join('')}
        <div class="gnav-more">
          <button class="gnav-more-btn" id="gnav-more-btn" type="button"
                  aria-haspopup="menu" aria-expanded="false" aria-controls="gnav-more-menu">
            <span>更多</span><span class="gnav-caret" aria-hidden="true"></span>
          </button>
          <div class="gnav-more-menu" id="gnav-more-menu" role="menu" aria-label="更多功能" hidden>
            ${buildMoreItems(null).map(moreLink).join('')}
          </div>
        </div>
      </nav>
      <button class="gnav-burger" id="gnav-burger" type="button"
              aria-label="打开菜单" aria-expanded="false" aria-controls="gnav-sheet">
        <span></span><span></span><span></span>
      </button>
    </div>`;
    const overlay = document.createElement('div');
    overlay.className = 'gnav-overlay';
    overlay.id = 'gnav-overlay';
    overlay.hidden = true;
    overlay.innerHTML = `
    <nav class="gnav-sheet" id="gnav-sheet" aria-label="移动端导航">
      <p class="gnav-sheet-title">主导航</p>
      ${TOP_ITEMS.map(sheetTop).join('')}
      <p class="gnav-sheet-title">更多</p>
      ${buildMoreItems(null).map(sheetMore).join('')}
    </nav>`;
    const modal = document.createElement('div');
    modal.className = 'gnav-modal';
    modal.id = 'gnav-confirm';
    modal.hidden = true;
    modal.setAttribute('role', 'dialog');
    modal.setAttribute('aria-modal', 'true');
    modal.setAttribute('aria-labelledby', 'gnav-confirm-title');
    modal.setAttribute('aria-describedby', 'gnav-confirm-text');
    modal.innerHTML = `
    <div class="gnav-modal-box">
      <p class="gnav-modal-title" id="gnav-confirm-title">离开会话页？</p>
      <p class="gnav-modal-text" id="gnav-confirm-text">${LEAVE_CONFIRM_TEXT}</p>
      <div class="gnav-modal-actions">
        <button class="gnav-btn" id="gnav-confirm-cancel" type="button">留在此页</button>
        <button class="gnav-btn gnav-btn-danger" id="gnav-confirm-ok" type="button">仍然离开</button>
      </div>
    </div>`;
    document.body.insertBefore(overlay, document.body.firstChild);
    document.body.insertBefore(bar, document.body.firstChild);
    document.body.append(modal);
    document.body.classList.add('gnav-on');
    const moreWrap = bar.querySelector('.gnav-more');
    const moreBtn = bar.querySelector('#gnav-more-btn');
    const moreMenu = bar.querySelector('#gnav-more-menu');
    const burger = bar.querySelector('#gnav-burger');
    const sheet = overlay.querySelector('#gnav-sheet');
    const cancelBtn = modal.querySelector('#gnav-confirm-cancel');
    const okBtn = modal.querySelector('#gnav-confirm-ok');
    /* ---------- 会话状态：更多里的“会话页”直达进行中的会话 ---------- */
    function applySession(sessionId) {
        for (const a of Array.from(document.querySelectorAll('[data-gnav-live-more]'))) {
            a.setAttribute('href', liveHref(sessionId));
            a.classList.toggle('is-off', !sessionId);
            a.title = sessionId ? '进入进行中的会话' : '当前没有进行中的会话，前往开始会话';
            const tag = a.querySelector('[data-gnav-live-tag]');
            if (tag)
                tag.textContent = sessionId ? '进行中' : '无进行中';
        }
        const promoted = document.getElementById('gnav-live-item');
        if (!sessionId) {
            promoted?.remove();
            return;
        }
        let link = promoted;
        if (!link) {
            link = document.createElement('a');
            link.id = 'gnav-live-item';
            link.className = 'gnav-link gnav-live';
            link.dataset.gnavLink = '';
            link.dataset.gnavLive = '';
            link.title = '会话进行中，进入会话页';
            link.innerHTML = '<span class="gnav-dot" aria-hidden="true"></span><span>会话</span>';
            moreWrap.parentElement.insertBefore(link, moreWrap);
        }
        link.setAttribute('href', liveHref(sessionId));
        if (route.more === 'live') {
            link.classList.add('is-active');
            link.setAttribute('aria-current', 'page');
        }
    }
    void (async () => {
        try {
            const res = await fetch('/api/status');
            applySession(res.ok ? activeSessionId(await res.json()) : null);
        }
        catch {
            applySession(null);
        }
    })();
    /* ---------- “更多”下拉 ---------- */
    const menuItems = () => Array.from(moreMenu.querySelectorAll('a'));
    function setMore(open, focus = null) {
        moreBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
        moreMenu.hidden = !open;
        if (open && focus) {
            const items = menuItems();
            const target = focus === 'first' ? items[0] : items[items.length - 1];
            target?.focus();
        }
    }
    moreBtn.addEventListener('click', () => {
        setMore(moreBtn.getAttribute('aria-expanded') !== 'true');
    });
    moreBtn.addEventListener('keydown', (event) => {
        if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
            event.preventDefault();
            setMore(true, event.key === 'ArrowDown' ? 'first' : 'last');
        }
        else if (event.key === 'Escape') {
            setMore(false);
        }
    });
    moreMenu.addEventListener('keydown', (event) => {
        const items = menuItems();
        const index = items.indexOf(document.activeElement);
        if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
            event.preventDefault();
            const next = event.key === 'ArrowDown' ? (index + 1) % items.length : (index - 1 + items.length) % items.length;
            items[next]?.focus();
        }
        else if (event.key === 'Home' || event.key === 'End') {
            event.preventDefault();
            items[event.key === 'Home' ? 0 : items.length - 1]?.focus();
        }
        else if (event.key === 'Escape') {
            event.preventDefault();
            setMore(false);
            moreBtn.focus();
        }
    });
    moreMenu.addEventListener('click', () => setMore(false));
    document.addEventListener('click', (event) => {
        if (!moreWrap.contains(event.target))
            setMore(false);
    });
    /* ---------- 窄屏汉堡菜单 ---------- */
    function applyMenu(open, restoreFocus = false) {
        const state = burgerState(open);
        burger.setAttribute('aria-expanded', state.expanded);
        burger.setAttribute('aria-label', state.label);
        burger.classList.toggle('is-open', open);
        overlay.hidden = state.overlayHidden;
        overlay.setAttribute('aria-hidden', open ? 'false' : 'true');
        if (open) {
            window.setTimeout(() => sheet.querySelector('a')?.focus(), 0);
        }
        else if (restoreFocus) {
            burger.focus();
        }
    }
    const menuOpen = () => burger.getAttribute('aria-expanded') === 'true';
    burger.addEventListener('click', () => applyMenu(!menuOpen()));
    overlay.addEventListener('click', (event) => {
        if (event.target === overlay)
            applyMenu(false, true);
    });
    sheet.addEventListener('click', (event) => {
        if (event.target.closest('a'))
            applyMenu(false);
    });
    sheet.addEventListener('keydown', (event) => {
        if (event.key !== 'Tab')
            return;
        const focusables = Array.from(sheet.querySelectorAll('a[href]'));
        if (!focusables.length)
            return;
        const first = focusables[0];
        const last = focusables[focusables.length - 1];
        if (event.shiftKey && document.activeElement === first) {
            event.preventDefault();
            last.focus();
        }
        else if (!event.shiftKey && document.activeElement === last) {
            event.preventDefault();
            first.focus();
        }
    });
    window.addEventListener('resize', () => {
        if (shouldCloseMenuOnResize(menuOpen(), window.innerWidth))
            applyMenu(false);
    });
    applyMenu(false);
    /* ---------- 会话页采集中的离开保护 ---------- */
    const modalOpen = () => !modal.hidden;
    let confirmTarget = null;
    let confirmReturnFocus = null;
    let leavingConfirmed = false;
    const isCapturing = () => needsLeaveConfirm({
        pathname: location.pathname,
        phase: document.getElementById('live-state')?.dataset.state ?? '',
    });
    function openConfirm(href) {
        confirmTarget = href;
        confirmReturnFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
        modal.hidden = false;
        cancelBtn.focus();
    }
    function closeConfirm(restoreFocus = true) {
        modal.hidden = true;
        confirmTarget = null;
        if (restoreFocus)
            confirmReturnFocus?.focus();
    }
    cancelBtn.addEventListener('click', () => closeConfirm());
    okBtn.addEventListener('click', () => {
        const target = confirmTarget;
        closeConfirm(false);
        if (!target)
            return;
        leavingConfirmed = true; // 已确认离开，不再触发浏览器 beforeunload 二次提示
        location.assign(target);
    });
    modal.addEventListener('keydown', (event) => {
        if (event.key === 'Escape') {
            event.preventDefault();
            closeConfirm();
            return;
        }
        if (event.key !== 'Tab')
            return;
        const focusables = [cancelBtn, okBtn];
        const index = focusables.indexOf(document.activeElement);
        if (event.shiftKey && index <= 0) {
            event.preventDefault();
            okBtn.focus();
        }
        else if (!event.shiftKey && index === focusables.length - 1) {
            event.preventDefault();
            cancelBtn.focus();
        }
    });
    document.addEventListener('click', (event) => {
        if (event.defaultPrevented)
            return;
        if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey)
            return;
        const link = event.target.closest('a[data-gnav-link]');
        if (!link)
            return;
        if (!isCapturing())
            return; // 非采集状态直接跳转
        event.preventDefault();
        openConfirm(link.href);
    });
    window.addEventListener('beforeunload', (event) => {
        if (leavingConfirmed || !isCapturing())
            return;
        event.preventDefault();
        event.returnValue = '';
    });
    document.addEventListener('keydown', (event) => {
        if (event.key !== 'Escape')
            return;
        if (modalOpen()) {
            event.preventDefault();
            closeConfirm();
        }
        else if (moreBtn.getAttribute('aria-expanded') === 'true') {
            event.preventDefault();
            setMore(false);
            moreBtn.focus();
        }
        else if (menuOpen()) {
            event.preventDefault();
            applyMenu(false, true);
        }
    });
}
