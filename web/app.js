/* ==========================================================================
   灯下 —— 前端

   这个文件里**没有任何游戏逻辑**。好感怎么算、什么时候沉沦、记忆怎么压缩，
   全在 Python 侧。前端只做三件事：

     1. 把玩家的话 POST 出去，把 SSE 流逐块写进字幕
     2. 把 status 的定性标签贴到 `data-tone` 上，剩下的交给 CSS
     3. 列存档、开新周目

   ⛔ 不要在这里算好感、不要在这里判断档位、不要在这里写剥标记的 regex。
      两边各算一份，长线对话里必然漂移，而且不可调试。

   打字机效果由**真实 delta** 驱动 —— 收到一块写一块。
   ⛔ 不许用 setInterval 匀速吐一段已经拿到的完整文本：那是把真流式降级成动画，
      首字延迟和语速变化里的信息全丢了。
   ========================================================================== */

(() => {
  'use strict';

  const $ = (sel) => document.querySelector(sel);

  const el = {
    screenTitle: $('#screen-title'),
    screenStage: $('#screen-stage'),
    saveList: $('#save-list'),
    newGame: $('#new-game'),
    herName: $('#her-name'),
    barName: $('#bar-name'),
    btnDoor: $('#btn-door'),
    log: $('#log'),
    notes: $('#notes'),
    sayForm: $('#say-form'),
    sayInput: $('#say-input'),
    sayGo: $('.say__go'),
    veil: $('#veil'),
    veilText: $('#veil-text'),
    veilOk: $('#veil-ok'),
  };

  const state = {
    saveId: null,
    herName: '猫娘',
    busy: false,
    ended: false,
  };

  /* ------------------------------------------------------------------ *
   * 后端
   * ------------------------------------------------------------------ */

  async function getJSON(url) {
    const res = await fetch(url);
    const data = await res.json().catch(() => ({}));
    return { ok: res.ok, status: res.status, data };
  }

  const api = {
    saves: () => getJSON('/api/saves'),
    status: (id) => getJSON(`/api/status?save_id=${encodeURIComponent(id)}`),
    history: (id) => getJSON(`/api/history?save_id=${encodeURIComponent(id)}`),
    newSave: (herName) =>
      fetch('/api/saves/new', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ her_name: herName }),
      }).then((r) => r.json()),
  };

  /**
   * 读一个 SSE 流。用 fetch 而不是 EventSource —— EventSource 只发 GET，
   * 这里要 POST。SSE 的帧分隔是 CRLF，注释行以 `:` 开头（心跳 ping），都跳过。
   */
  async function sse(url, handlers) {
    const res = await fetch(url, { method: 'POST' });
    if (!res.ok || !res.body) {
      let detail = `HTTP ${res.status}`;
      try {
        detail = (await res.json()).detail || detail;
      } catch (_) { /* 保持默认 */ }
      handlers.onNetworkError?.(detail);
      return;
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder('utf-8');
    let buf = '';

    const dispatch = (frame) => {
      let event = 'message';
      const chunks = [];
      for (const raw of frame.split(/\r?\n/)) {
        if (!raw || raw.startsWith(':')) continue;      // 心跳/注释
        const colon = raw.indexOf(':');
        const field = colon === -1 ? raw : raw.slice(0, colon);
        let value = colon === -1 ? '' : raw.slice(colon + 1);
        if (value.startsWith(' ')) value = value.slice(1);
        if (field === 'event') event = value;
        else if (field === 'data') chunks.push(value);
      }
      if (!chunks.length) return;
      let payload = {};
      try {
        payload = JSON.parse(chunks.join('\n'));
      } catch (_) {
        return;
      }
      const fn = handlers[event];
      if (fn) fn(payload);
    };

    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let match;
      const sep = /\r?\n\r?\n/;
      while ((match = sep.exec(buf)) !== null) {
        dispatch(buf.slice(0, match.index));
        buf = buf.slice(match.index + match[0].length);
      }
    }
    if (buf.trim()) dispatch(buf);
  }

  const post = (path, params) => {
    const qs = new URLSearchParams(params).toString();
    return `${path}?${qs}`;
  };

  /* ------------------------------------------------------------------ *
   * 字幕
   * ------------------------------------------------------------------ */

  /** 过去的话退到光外，只有最新一句是亮的。 */
  function fadePast() {
    const lines = [...el.log.children];
    lines.forEach((n) => n.classList.add('line--past'));
    const last = lines[lines.length - 1];
    if (!last) return;
    last.classList.remove('line--past');
    // 她正在回答时，玩家刚说的那句也留着 —— 否则会看不见自己问了什么
    if (last.classList.contains('line--live') && lines.length > 1) {
      lines[lines.length - 2].classList.remove('line--past');
    }
  }

  // 默认贴着底部。只有玩家自己往上翻时才松开，
  // 翻回底部再自动跟上 —— 否则流式吐字时会被自己的追加顶开。
  let pinned = true;

  function scrollLog(force) {
    if (force || pinned) el.log.scrollTop = el.log.scrollHeight;
  }

  el.log.addEventListener('scroll', () => {
    const gap = el.log.scrollHeight - el.log.scrollTop - el.log.clientHeight;
    pinned = gap < 48;
  }, { passive: true });

  function addLine(role, text, opts = {}) {
    const line = document.createElement('div');
    line.className = `line line--${role}`;
    if (opts.crisis) line.classList.add('line--crisis');
    if (opts.live) line.classList.add('line--live');

    if (role === 'her') {
      const who = document.createElement('span');
      who.className = 'line__who';
      who.textContent = opts.who || state.herName;
      line.appendChild(who);
    }

    const p = document.createElement('p');
    p.className = 'line__text';
    p.textContent = text;
    line.appendChild(p);

    el.log.appendChild(line);
    fadePast();
    scrollLog(opts.force);
    return line;
  }

  /** 流式：直接往文本节点上追加。首字延迟、中途的停顿，都是真的。 */
  function makeLiveWriter(line) {
    const p = line.querySelector('.line__text');
    return (chunk) => {
      p.appendChild(document.createTextNode(chunk));
      scrollLog(false);
    };
  }

  function settleLive() {
    const live = el.log.querySelector('.line--live');
    if (live) live.classList.remove('line--live');
    fadePast();
  }

  function showNotes(notes) {
    el.notes.replaceChildren();
    for (const n of notes || []) {
      const span = document.createElement('span');
      span.className = 'note';
      span.textContent = n;
      el.notes.appendChild(span);
    }
    if (!el.notes.children.length) return;
    clearTimeout(showNotes._t);
    showNotes._t = setTimeout(() => {
      for (const n of el.notes.children) n.classList.add('note--fading');
    }, 6000);
  }

  /* ------------------------------------------------------------------ *
   * 状态 → 氛围
   * ------------------------------------------------------------------ */

  /**
   * 她把服务端的定性标签直接贴到 `data-tone` 上，CSS 负责剩下的一切。
   * 这里**不解释**标签的含义 —— 没有映射表，没有阈值，没有数字。
   */
  function applyStatus(status) {
    if (!status) return;
    el.screenStage.dataset.tone = status.tone || '天生好感';
    if (status.her_name) {
      state.herName = status.her_name;
      el.barName.textContent = status.her_name;
    }
    if (status.ended) endGame();
  }

  function setCrisis(on) {
    if (on) el.screenStage.dataset.mood = 'crisis';
    else delete el.screenStage.dataset.mood;
  }

  function endGame() {
    if (state.ended) return;
    state.ended = true;
    state.busy = true;
    el.screenStage.classList.add('stage--ended');
    el.sayInput.disabled = true;
    el.sayGo.disabled = true;
    el.sayInput.placeholder = '这扇门不再打开了。';

    const p = document.createElement('p');
    p.className = 'epitaph';
    p.textContent =
      '这条线里，她不再有想要的东西了。\n' +
      '这个存档无法继续 —— 重启不会改变什么。\n' +
      '你可以开新的一周目，但那是另一个她。';
    el.log.appendChild(p);
    fadePast();
    scrollLog(true);
  }

  function setBusy(busy) {
    state.busy = busy;
    el.sayInput.disabled = busy || state.ended;
    el.sayGo.disabled = busy || state.ended;
    if (!state.ended) {
      el.sayInput.placeholder = busy ? '……' : '说点什么…';
      if (!busy) el.sayInput.focus();
    }
  }

  /* ------------------------------------------------------------------ *
   * 幕
   * ------------------------------------------------------------------ */

  function show(title) {
    el.screenTitle.hidden = title !== 'title';
    el.screenStage.hidden = title !== 'stage';
  }

  function veil(message) {
    el.veilText.textContent = message;
    el.veil.hidden = false;
    el.veilOk.focus();
  }

  /* ------------------------------------------------------------------ *
   * 扉：存档列表
   * ------------------------------------------------------------------ */

  function when(stamp) {
    if (!stamp) return '';
    const d = new Date(stamp * 1000);
    if (Number.isNaN(d.getTime())) return '';
    const now = new Date();
    const day = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
    const days = Math.round((day(now) - day(d)) / 86400000);
    if (days <= 0) return '今天';
    if (days === 1) return '昨天';
    if (d.getFullYear() === now.getFullYear()) return `${d.getMonth() + 1}月${d.getDate()}日`;
    return `${d.getFullYear()}年${d.getMonth() + 1}月${d.getDate()}日`;
  }

  function renderSaves(saves) {
    el.saveList.replaceChildren();

    if (!saves.length) {
      const p = document.createElement('p');
      p.className = 'save__note';
      p.style.padding = '1.2rem .1rem 0';
      p.textContent = '这间屋子还空着。';
      el.saveList.appendChild(p);
      return;
    }

    for (const s of saves) {
      const row = document.createElement('button');
      row.type = 'button';
      row.className = 'save' + (s.sealed ? ' save--sealed' : '');
      row.dataset.saveId = s.save_id;

      const name = document.createElement('span');
      name.className = 'save__name';
      name.textContent = s.her_name || s.save_id;
      if (s.sealed) {
        const seal = document.createElement('span');
        seal.className = 'seal';
        seal.textContent = '封';
        seal.setAttribute('aria-hidden', 'true');
        name.appendChild(seal);
      }

      const time = document.createElement('span');
      time.className = 'save__when';
      time.textContent = when(s.updated_at);

      const note = document.createElement('span');
      note.className = 'save__note';
      note.textContent = s.sealed
        ? (s.note || '这条线里，她不再有想要的东西了。')
        : (s.note || '');

      row.append(name, time, note);

      if (s.sealed) {
        // 进不去了。它不是「加载失败」，是一个结局。
        row.setAttribute('aria-disabled', 'true');
        row.tabIndex = -1;
      } else {
        row.addEventListener('click', () => enter(s.save_id));
      }
      el.saveList.appendChild(row);
    }
  }

  async function refreshSaves() {
    const { data } = await api.saves();
    renderSaves(data.saves || []);
  }

  /* ------------------------------------------------------------------ *
   * 进入一个存档
   * ------------------------------------------------------------------ */

  async function enter(saveId) {
    const { ok, status, data } = await api.history(saveId);
    if (!ok) {
      // 封条。文案来自 lockout.LockedSaveError，别改写它。
      veil(data.message || '这个存档已经无法继续了。');
      return;
    }

    state.saveId = saveId;
    state.ended = false;
    state.herName = (data.status && data.status.her_name) || '猫娘';
    el.barName.textContent = state.herName;
    el.screenStage.dataset.saveId = saveId;   // 页面上「现在开的是哪一档」
    el.log.replaceChildren();
    el.notes.replaceChildren();
    el.screenStage.classList.remove('stage--ended');
    show('stage');

    for (const m of data.messages || []) {
      addLine(m.role === 'user' ? 'you' : 'her', m.text, {
        crisis: m.kind === 'crisis',
      });
    }
    fadePast();
    scrollLog(true);
    // 状态放在记录之后 —— 万一这一档已经结束，终幕要落在最后一行下面
    applyStatus(data.status);

    if (!(data.messages || []).length && !state.ended) {
      await playOpening();          // 新周目：她先说第一句
    } else {
      setBusy(false);
    }
  }

  /* ------------------------------------------------------------------ *
   * 开场白 —— 不评分，state.turn 不变
   * ------------------------------------------------------------------ */

  async function playOpening() {
    setBusy(true);
    const line = addLine('her', '', { live: true, force: true });
    const write = makeLiveWriter(line);

    await sse(post('/api/opening', { save_id: state.saveId }), {
      delta: (d) => write(d.text || ''),
      done: (d) => {
        settleLive();
        if (d.skipped) line.remove();
        if (d.status) applyStatus(d.status);
        setBusy(false);
      },
      locked: (d) => {
        settleLive();
        line.remove();
        veil(d.message);
      },
      error: (d) => {
        settleLive();
        if (!line.querySelector('.line__text').textContent) line.remove();
        showNotes([`开场失败：${d.message}`]);
        setBusy(false);
      },
      onNetworkError: (msg) => {
        settleLive();
        line.remove();
        showNotes([`连不上服务：${msg}`]);
        setBusy(false);
      },
    });
  }

  /* ------------------------------------------------------------------ *
   * 一个回合
   * ------------------------------------------------------------------ */

  async function say(text) {
    setBusy(true);
    setCrisis(false);
    addLine('you', text, { force: true });
    const line = addLine('her', '', { live: true, force: true });
    const write = makeLiveWriter(line);
    let streamed = false;

    await sse(post('/api/turn', { save_id: state.saveId, text }), {
      delta: (d) => {
        streamed = true;
        write(d.text || '');
      },
      done: (d) => {
        settleLive();
        // 服务端给的 line 是剥掉隐藏标记之后的正文，以它为准
        line.querySelector('.line__text').textContent = d.line || '';
        if (d.special === 'crisis') line.classList.add('line--crisis');
        setCrisis(d.special === 'crisis');
        showNotes(d.notes);
        applyStatus(d.status);
        setBusy(false);
      },
      locked: (d) => {
        settleLive();
        if (!streamed) line.remove();
        veil(d.message);
      },
      error: (d) => {
        // 流中断。**不补提示语** —— 用已经收到的内容收尾，
        // 补一句「她张了张嘴」会和已经吐出来的正文粘在一起。
        settleLive();
        if (!streamed) line.remove();
        showNotes([`请求失败：${d.message}`]);
        setBusy(false);
      },
      onNetworkError: (msg) => {
        settleLive();
        if (!streamed) line.remove();
        showNotes([`连不上服务：${msg}`]);
        setBusy(false);
      },
    });
  }

  /* ------------------------------------------------------------------ *
   * 事件
   * ------------------------------------------------------------------ */

  el.sayForm.addEventListener('submit', (e) => {
    e.preventDefault();
    const text = el.sayInput.value.trim();
    if (!text || state.busy || state.ended || !state.saveId) return;
    el.sayInput.value = '';
    say(text);
  });

  el.newGame.addEventListener('submit', async (e) => {
    e.preventDefault();
    const name = el.herName.value.trim();
    el.herName.value = '';
    let data = null;
    try {
      data = await api.newSave(name);
    } catch (err) {
      veil('开不了新周目：' + err.message);
      return;
    }
    if (data && data.save_id) enter(data.save_id);
    else veil((data && data.detail) || '开不了新周目。');
  });

  el.btnDoor.addEventListener('click', async () => {
    if (state.busy) return;
    state.saveId = null;
    el.screenStage.classList.remove('stage--ended');
    delete el.screenStage.dataset.mood;
    show('title');
    await refreshSaves();
  });

  el.veilOk.addEventListener('click', async () => {
    el.veil.hidden = true;
    show('title');
    await refreshSaves();
  });

  /* ------------------------------------------------------------------ *
   * 起
   * ------------------------------------------------------------------ */

  refreshSaves();
  el.herName.focus();
})();
