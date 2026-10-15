/* ===== ronbun — 勉強用の共通パーツ =====
   ① 理解度チェック（.quiz 内の .q）: 選択肢を押すと正誤＋解説を表示。結果は localStorage に保存
   ② 質問チャット: 右下の「💬 質問」から、このページと論文本文をもとに Claude に質問できる
      （tools/server.py で起動しているときだけ動く。会話はページごとに localStorage に保存） */
(function () {
  const slug = (location.pathname.split('/').pop() || '').replace(/\.html$/, '');
  const store = {
    get(k, d) { try { const v = localStorage.getItem(k); return v ? JSON.parse(v) : d; } catch (e) { return d; } },
    set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) {} },
    del(k) { try { localStorage.removeItem(k); } catch (e) {} },
  };
  const esc = s => s.replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

  /* ---------- ① 理解度チェック ---------- */
  function initQuiz() {
    const quiz = document.querySelector('.quiz');
    if (!quiz) return;
    const qs = [...quiz.querySelectorAll('.q')];
    const key = 'ronbun:quiz:' + slug;
    const saved = store.get(key, {});
    const score = document.createElement('div');
    score.className = 'quiz-score';
    quiz.appendChild(score);

    function render() {
      const done = Object.keys(saved).length;
      const ok = Object.values(saved).filter(Boolean).length;
      score.innerHTML = done === 0 ? `全${qs.length}問。選ぶとすぐ正解と解説が出ます。`
        : `<b>${ok} / ${qs.length}</b> 問正解（${done}問回答）` +
          (done === qs.length ? (ok === qs.length ? ' 🎉 完璧！' : ' — 間違えた問題は解説の章へ') : '') +
          ' <button type="button" class="quiz-reset">もう一度解く</button>';
      const r = score.querySelector('.quiz-reset');
      if (r) r.onclick = () => { for (const k in saved) delete saved[k]; store.del(key); qs.forEach(reset); render(); };
    }
    function reset(q) {
      q.classList.remove('answered');
      q.querySelectorAll('.choice').forEach(b => { b.disabled = false; b.classList.remove('right', 'wrong', 'picked'); });
    }
    function answer(q, i, btn, persist) {
      const correct = btn.hasAttribute('data-correct');
      q.classList.add('answered');
      q.querySelectorAll('.choice').forEach(b => {
        b.disabled = true;
        if (b.hasAttribute('data-correct')) b.classList.add('right');
      });
      btn.classList.add('picked');
      if (!correct) btn.classList.add('wrong');
      if (persist) { saved[i] = correct; store.set(key, saved); render(); }
    }
    qs.forEach((q, i) => {
      const btns = [...q.querySelectorAll('.choice')];
      btns.forEach((b, j) => {
        b.type = 'button';
        b.onclick = () => { if (!q.classList.contains('answered')) { answer(q, i, b, true); q.dataset.pick = j; } };
      });
    });
    render();
  }

  /* ---------- ② 質問チャット ---------- */
  function mdLite(s) {
    // 最小限の Markdown: コードブロック / `code` / **太字** / 箇条書き / 見出し / 改行
    const blocks = [];
    s = s.replace(/```[a-z]*\n?([\s\S]*?)```/g, (_, c) => { blocks.push(c); return `\u0000${blocks.length - 1}\u0000`; });
    s = esc(s)
      .replace(/`([^`\n]+)`/g, '<code>$1</code>')
      .replace(/\*\*([^*\n]+)\*\*/g, '<b>$1</b>')
      .replace(/^#{1,4}\s+(.+)$/gm, '<b class="h">$1</b>')
      .replace(/^\s*[-*・]\s+(.+)$/gm, '<span class="li">$1</span>')
      .replace(/\n{2,}/g, '<br><br>').replace(/\n/g, '<br>');
    return s.replace(/\u0000(\d+)\u0000/g, (_, i) => `<pre>${esc(blocks[+i])}</pre>`);
  }

  function initChat() {
    if (!document.querySelector('.section-head')) return;   // 解説ページだけ
    const key = 'ronbun:chat:' + slug;
    let st = store.get(key, { session: null, msgs: [] });

    const fab = document.createElement('button');
    fab.className = 'chat-fab'; fab.type = 'button';
    fab.innerHTML = '💬 質問する';
    const panel = document.createElement('div');
    panel.className = 'chat-panel'; panel.hidden = true;
    panel.innerHTML = `
      <div class="chat-head"><b>この論文について質問</b>
        <button type="button" class="chat-rev" title="分からなかった点をもとに、このページの該当箇所を書き直す" hidden>ページに反映</button>
        <button type="button" class="chat-cli" title="この会話をターミナルの claude -r で続けるコマンドをコピー" hidden>ターミナルで続ける</button>
        <button type="button" class="chat-new" title="会話をリセット">新しい会話</button>
        <button type="button" class="chat-x" aria-label="閉じる">×</button></div>
      <div class="chat-log"></div>
      <form class="chat-form">
        <div class="chat-quote" hidden></div>
        <textarea rows="2" placeholder="例: 3章の図のオレンジの線は何？（⌘+Enter で送信）"></textarea>
        <button type="submit" class="btn-primary">送信</button>
      </form>`;
    document.body.append(fab, panel);
    const log = panel.querySelector('.chat-log');
    const form = panel.querySelector('form');
    const ta = panel.querySelector('textarea');
    const quoteEl = panel.querySelector('.chat-quote');
    let quote = '', busy = false;

    const hints = ['ひとことで言うと何をした論文？', 'いちばん大事な図はどれ？見方を教えて', '前提として知っておくべきことは？'];
    const cli = panel.querySelector('.chat-cli');
    cli.onclick = async () => {
      const cmd = `cd '${st.cliDir || 'tools/chat'}' && claude -r ${st.session}`;
      try { await navigator.clipboard.writeText(cmd); cli.textContent = 'コピーしました'; setTimeout(() => cli.textContent = 'ターミナルで続ける', 1500); }
      catch (e) { window.prompt('ターミナルに貼り付けてください', cmd); }
    };
    const rev = panel.querySelector('.chat-rev');
    const syncButtons = () => { cli.hidden = !st.session; rev.hidden = !st.msgs.some(m => m.role === 'ai'); };
    rev.onclick = async () => {
      if (busy) return;
      busy = true; rev.disabled = true;
      const note = add('ai wait', 'ページを書き直しています…');
      let res;
      try {
        res = await fetch('/api/revise', { method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ slug, msgs: st.msgs }) }).then(r => r.json());
      } catch (e) { res = { error: 'サーバーにつながりません。' }; }
      if (res.error) { note.remove(); add('ai err', res.error); busy = false; rev.disabled = false; return; }
      const t0 = Date.now();
      const iv = setInterval(async () => {
        let j;
        try { j = await fetch('/api/status?id=' + res.job_id).then(r => r.json()); } catch (e) { return; }
        note.textContent = (j.msg || '書き直し中…') + ` ${Math.round((Date.now() - t0) / 1000)}秒`;
        if (j.state === 'done') {
          clearInterval(iv);
          try { sessionStorage.setItem('ronbun:revised', JSON.stringify({ slug, backup: j.backup })); } catch (e) {}
          location.href = j.url; location.reload();
        } else if (j.state === 'error') {
          clearInterval(iv); note.remove(); add('ai err', j.error || '書き直しに失敗しました');
          busy = false; rev.disabled = false;
        }
      }, 3000);
    };
    function revisedNotice() {
      let r = null;
      try { r = JSON.parse(sessionStorage.getItem('ronbun:revised') || 'null'); sessionStorage.removeItem('ronbun:revised'); } catch (e) {}
      if (!r || r.slug !== slug) return;
      open();
      const d = add('ai sys', 'ページを書き直しました。書き直した箇所には ✎ の印があります。');
      const b = document.createElement('button');
      b.type = 'button'; b.className = 'chat-undo'; b.textContent = '元に戻す';
      b.onclick = async () => {
        const res = await fetch('/api/revert', { method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ slug, backup: r.backup }) }).then(x => x.json()).catch(() => ({ error: 'サーバーにつながりません。' }));
        if (res.error) { add('ai err', res.error); return; }
        location.hash = ''; location.reload();
      };
      d.appendChild(b);
    }
    function draw() {
      syncButtons();
      log.innerHTML = st.msgs.length ? '' :
        `<div class="chat-empty">ページと論文本文をもとに答えます。
          <div class="chat-hints">${hints.map(h => `<button type="button">${h}</button>`).join('')}</div>
          <small>文章を選択してから開くとその部分について聞けます。</small></div>`;
      log.querySelectorAll('.chat-hints button').forEach(b => b.onclick = () => { ta.value = b.textContent; send(); });
      st.msgs.forEach(m => add(m.role, m.text, false));
    }
    function add(role, text, scroll = true) {
      const d = document.createElement('div');
      d.className = 'msg ' + role;
      d.innerHTML = role === 'ai' ? mdLite(text) : esc(text).replace(/\n/g, '<br>');
      log.appendChild(d);
      if (scroll) log.scrollTop = log.scrollHeight;
      return d;
    }
    function open() {
      const sel = String(window.getSelection() || '').trim();
      if (sel && !panel.contains(window.getSelection().anchorNode)) {
        quote = sel.slice(0, 600);
        quoteEl.textContent = '「' + (quote.length > 120 ? quote.slice(0, 120) + '…' : quote) + '」について';
        quoteEl.hidden = false;
      }
      panel.hidden = false; fab.hidden = true;
      log.scrollTop = log.scrollHeight; ta.focus();
    }
    fab.onclick = open;
    panel.querySelector('.chat-x').onclick = () => { panel.hidden = true; fab.hidden = false; };
    panel.querySelector('.chat-new').onclick = () => { if (busy) return; st = { session: null, msgs: [] }; store.del(key); draw(); };
    quoteEl.onclick = () => { quote = ''; quoteEl.hidden = true; };
    ta.addEventListener('keydown', e => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); send(); } });
    form.onsubmit = e => { e.preventDefault(); send(); };

    async function send() {
      const q = ta.value.trim();
      if (!q || busy) return;
      busy = true;
      if (!st.msgs.length) log.innerHTML = '';
      const shown = quote ? `「${quote.length > 80 ? quote.slice(0, 80) + '…' : quote}」\n${q}` : q;
      st.msgs.push({ role: 'me', text: shown }); add('me', shown);
      ta.value = '';
      const wait = add('ai wait', '考え中…');
      const t0 = Date.now();
      const tick = setInterval(() => { wait.textContent = `考え中… ${Math.round((Date.now() - t0) / 1000)}秒`; }, 1000);
      let res;
      try {
        res = await fetch('/api/chat', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ slug, question: q, quote, session: st.session }),
        }).then(r => r.ok || r.status < 500 ? r.json() : Promise.reject(r.status));
      } catch (e) {
        res = { error: 'サーバーにつながりません。チャットは python3 tools/server.py で起動したときだけ使えます。' };
      }
      clearInterval(tick); wait.remove();
      if (res.error) {
        add('ai err', res.error);
      } else {
        st.session = res.session || st.session;
        if (res.cli_dir) st.cliDir = res.cli_dir;
        syncButtons();
        st.msgs.push({ role: 'ai', text: res.answer });
        add('ai', res.answer);
        store.set(key, st);
      }
      quote = ''; quoteEl.hidden = true; busy = false;
      if (res.revise) rev.onclick();   // チャットで「書き直して」と頼まれたらそのまま反映
    }
    draw();
    revisedNotice();
  }

  initQuiz();
  initChat();
})();
