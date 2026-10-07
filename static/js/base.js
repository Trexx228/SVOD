/* СВОД: глобальные скрипты — панели, уведомления, вложения, лайтбокс, таймеры */
(function () {
  'use strict';

  /* ---------- панели настроек и поддержки ---------- */
  const gear = document.getElementById('gear-btn');
  const panel = document.getElementById('settings-panel');

  if (gear && panel) {
    gear.addEventListener('click', e => {
      e.stopPropagation();
      panel.hidden = !panel.hidden;
    });

    document.addEventListener('click', e => {
      if (!panel.hidden && !panel.contains(e.target) && e.target !== gear) {
        panel.hidden = true;
      }
    });

    document.addEventListener('keydown', e => {
      if (e.key === 'Escape') panel.hidden = true;
    });
  }

  const sbtn = document.getElementById('support-btn');
  const spanel = document.getElementById('support-panel');

  if (sbtn && spanel) {
    sbtn.addEventListener('click', e => {
      e.stopPropagation();
      spanel.hidden = !spanel.hidden;
    });

    document.addEventListener('click', e => {
      if (!spanel.hidden && !spanel.contains(e.target) && e.target !== sbtn) {
        spanel.hidden = true;
      }
    });
  }

  /* ---------- бейджи и звук уведомлений ---------- */
  function setBadge(id, n) {
    const el = document.getElementById(id);
    if (!el) return;

    el.hidden = !n;
    el.textContent = n > 99 ? '99+' : n;
  }

  window.chime = function () {
    if (document.body.dataset.sound === '0') return;

    try {
      const ctx = window.__actx || (window.__actx = new (window.AudioContext || window.webkitAudioContext)());
      if (ctx.state === 'suspended') ctx.resume();

      const now = ctx.currentTime;

      [[880, 0], [660, 0.18]].forEach(pair => {
        const o = ctx.createOscillator();
        const g = ctx.createGain();

        o.type = 'sine';
        o.frequency.value = pair[0];

        g.gain.setValueAtTime(0.0001, now + pair[1]);
        g.gain.exponentialRampToValueAtTime(0.25, now + pair[1] + 0.02);
        g.gain.exponentialRampToValueAtTime(0.0001, now + pair[1] + 0.4);

        o.connect(g);
        g.connect(ctx.destination);

        o.start(now + pair[1]);
        o.stop(now + pair[1] + 0.45);
      });
    } catch (e) {}
  };

  document.addEventListener('click', () => {
    if (window.__actx && window.__actx.state === 'suspended') {
      window.__actx.resume();
    }
  });

  let lastNotes = -1;
  let lastChats = -1;

  setInterval(async () => {
    try {
      const r = await fetch('/comms/poll/');
      const d = await r.json();

      const notes = d.notes || 0;
      const chats = d.chats || 0;

      setBadge('bell-badge', notes);
      setBadge('chat-badge', chats);

      if (lastNotes >= 0 && (notes > lastNotes || chats > lastChats)) {
        window.chime();
      }

      lastNotes = notes;
      lastChats = chats;
    } catch (e) {}
  }, 5000);

  /* ---------- вложения: Ctrl+V, выбор, drag&drop ---------- */
  function getCookie(name) {
    const escaped = name.replace(/[.+?^${}()|[\]\\]/g, '\\$&');
    const m = document.cookie.match(new RegExp('(^|; )' + escaped + '=([^;]*)'));
    return m ? decodeURIComponent(m[2]) : '';
  }

  const CSRF = getCookie('csrftoken');

  function widgets() {
    const legacy = {
      box: document.getElementById('attach-box'),
      ids: document.getElementById('attach_ids'),
      file: document.getElementById('attach-input')
    };

    const sup = {
      box: document.getElementById('support-attach-box'),
      ids: document.getElementById('support_attach_ids'),
      file: document.getElementById('support-attach-input')
    };

    const supPanel = document.getElementById('support-panel');
    const supOpen = supPanel && !supPanel.hidden;

    const list = [];

    if (sup.box && supOpen) list.push(sup);
    if (legacy.box) list.push(legacy);
    if (sup.box && !supOpen) list.push(sup);

    return list;
  }

  const visible = w => w.box && w.box.offsetParent !== null;

  widgets().forEach(w => {
    if (w.box) w.box.classList.add('attach-zone');
  });

  function flash(w) {
    if (!w.box) return;

    w.box.classList.add('flash');
    setTimeout(() => w.box.classList.remove('flash'), 900);
  }

  function pushId(w, id) {
    if (!w.ids) return;

    const ids = w.ids.value ? w.ids.value.split(',') : [];

    if (!ids.includes(String(id))) {
      ids.push(String(id));
    }

    w.ids.value = ids.join(',');
  }

  function dropId(w, id) {
    if (!w.ids) return;

    w.ids.value = (w.ids.value ? w.ids.value.split(',') : [])
      .filter(x => x && x !== String(id))
      .join(',');
  }

  function preview(w, d) {
    const wrap = document.createElement('div');
    wrap.style.cssText = 'position:relative;border:1px solid var(--line);border-radius:12px;padding:4px;max-width:120px';

    const remove = document.createElement('button');
    remove.type = 'button';
    remove.title = 'Убрать';
    remove.textContent = '×';
    remove.style.cssText = 'position:absolute;top:-8px;right:-8px;border:0;border-radius:999px;background:var(--red);color:#fff;width:20px;height:20px;cursor:pointer';

    remove.onclick = () => {
      dropId(w, d.id);
      wrap.remove();
    };

    if (d.image) {
      const img = document.createElement('img');
      img.src = d.url;
      img.style.cssText = 'width:100%;border-radius:8px;display:block';
      wrap.appendChild(img);
    } else {
      const div = document.createElement('div');
      div.style.cssText = 'font-size:11px;padding:8px';
      div.textContent = '📄 ' + (d.name || '');
      wrap.appendChild(div);
    }

    wrap.appendChild(remove);
    w.box.appendChild(wrap);
    pushId(w, d.id);
  }

  function pending(w) {
    const el = document.createElement('div');
    el.className = 'attach-pending';
    el.textContent = '⏳ Загрузка…';
    w.box.appendChild(el);
    return el;
  }

  function upload(w, file) {
    if (!file) return;

    if (file.size > 10 * 1024 * 1024) {
      alert('Файл больше 10 МБ — не прикреплён.');
      return;
    }

    const ph = pending(w);
    const fd = new FormData();
    fd.append('file', file);

    fetch('/comms/upload/', {
      method: 'POST',
      headers: { 'X-CSRFToken': CSRF },
      body: fd
    })
      .then(r => {
        if (!r.ok) throw new Error();
        return r.json();
      })
      .then(d => {
        ph.remove();

        if (d && d.id) {
          preview(w, d);
          flash(w);
        } else {
          alert('Не удалось прикрепить файл.');
        }
      })
      .catch(() => {
        ph.remove();
        alert('Ошибка загрузки файла.');
      });
  }

  window.attachUpload = function (file) {
    const w = widgets().find(visible) || widgets()[0];
    if (w) upload(w, file);
  };

  document.addEventListener('paste', e => {
    if (e.__svodPaste) return;

    e.__svodPaste = true;

    const w = widgets().find(visible);
    if (!w) return;

    const items = (e.clipboardData || {}).items || [];
    let hit = false;

    for (const it of items) {
      if (it.type && it.type.startsWith('image/')) {
        const f = it.getAsFile();

        if (f) {
          upload(w, f);
          hit = true;
        }
      }
    }

    if (hit) e.preventDefault();
  });

  document.addEventListener('change', e => {
    const w = widgets().find(x => x.file === e.target);

    if (w && e.target.files) {
      Array.from(e.target.files).forEach(f => upload(w, f));
      e.target.value = '';
    }
  });

  document.addEventListener('dragover', e => {
    const w = widgets().find(visible);

    if (!w || !e.dataTransfer || !Array.from(e.dataTransfer.types || []).includes('Files')) {
      return;
    }

    e.preventDefault();
    w.box.classList.add('drag');
  });

  document.addEventListener('drop', e => {
    const w = widgets().find(visible);

    if (!w || !e.dataTransfer || !e.dataTransfer.files || !e.dataTransfer.files.length) {
      return;
    }

    e.preventDefault();
    w.box.classList.remove('drag');

    Array.from(e.dataTransfer.files).forEach(f => upload(w, f));
  });

  document.addEventListener('dragleave', () => {
    widgets().forEach(w => {
      if (w.box) w.box.classList.remove('drag');
    });
  });

  /* ---------- лайтбокс вложений ---------- */
  let overlay = null;

  function closeOverlay() {
    if (overlay) {
      overlay.remove();
      overlay = null;
    }
  }

  document.addEventListener('click', e => {
    if (overlay) {
      e.preventDefault();
      closeOverlay();
      return;
    }

    const img = e.target.closest('img');

    if (!img || !img.src || img.src.indexOf('/media/') === -1) {
      return;
    }

    e.preventDefault();

    overlay = document.createElement('div');
    overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.85);z-index:100;display:flex;align-items:center;justify-content:center;padding:24px;cursor:zoom-out';

    const big = document.createElement('img');
    big.src = img.src;
    big.style.cssText = 'max-width:95vw;max-height:92vh;border-radius:12px;box-shadow:0 20px 80px rgba(0,0,0,.6);cursor:zoom-out';

    overlay.appendChild(big);
    document.body.appendChild(overlay);
  });

  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') closeOverlay();
  });

  /* ---------- живые таймеры задач ---------- */
  const cfg = document.getElementById('timer-cfg');
  const UNIT = (cfg && cfg.dataset.unit) || 'auto';
  const NORM = parseFloat((cfg && cfg.dataset.norm) || '8') || 8;

  function fmtSec(sec) {
    if (UNIT === 'min') return Math.ceil(sec / 60) + ' мин';
    if (UNIT === 'h') return (sec / 3600).toFixed(2).replace('.', ',') + ' ч';
    if (UNIT === 'd') return (sec / 3600 / NORM).toFixed(2).replace('.', ',') + ' чел-дн';

    sec = Math.max(0, Math.ceil(sec / 60) * 60);

    const m = Math.floor(sec / 60);
    const d = Math.floor(m / 1440);
    const h = Math.floor((m % 1440) / 60);
    const mi = m % 60;

    return d ? d + ' д ' + h + ' ч ' + mi + ' мин' : h ? h + ' ч ' + mi + ' мин' : mi + ' мин';
  }

  function tick() {
    document.querySelectorAll('[data-timer]').forEach(el => {
      let s = parseFloat(el.dataset.total || 0) * 3600;

      if (el.dataset.start) {
        s += (Date.now() - new Date(el.dataset.start)) / 1000;
      }

      el.textContent = fmtSec(s);
    });
  }

  if (document.querySelector('[data-timer]')) {
    setInterval(tick, 1000);
    tick();
  }
})();


/* ---------- календари: кнопка 📅 открывает календарь ---------- */
document.addEventListener('click', e => {
    const calBtn = e.target.closest('[data-cal-toggle]');
    if (calBtn) {
        const targetId = calBtn.dataset.calToggle;
        const cal = document.getElementById(targetId);
        if (cal) {
            cal.hidden = !cal.hidden;
        }
    }
});
