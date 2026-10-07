/* Логика формы постановки задачи: чипы подразделений, два календаря, подсказки */
(function () {
  'use strict';

  const $ = id => document.getElementById(id);

  function readJson(id, fallback) {
    const el = document.getElementById(id);
    if (!el) return fallback;

    try {
      return JSON.parse(el.textContent);
    } catch (e) {
      return fallback;
    }
  }

  try {
    const USERS = readJson('users-data', []);
    const DEPTS = readJson('depts-data', []);
    const NORM = readJson('norm-data', 8) || 8;

    const idsInp = $('exec_ids');
    const chipsBox = $('dept-chips');
    const checked = new Set(((idsInp && idsInp.value) || '').split(',').filter(Boolean));

    function syncHidden() {
      if (idsInp) idsInp.value = Array.from(checked).join(',');
    }

    function membersOf(deptId) {
      return USERS.filter(u => u.dept === deptId);
    }

    /* ---------- панели выбора людей ---------- */
    let panel = null;

    function closePanel() {
      if (panel) {
        panel.remove();
        panel = null;
      }
    }

    function openPanel(dp, wrap) {
      closePanel();

      panel = document.createElement('div');
      panel.className = 'dept-panel';
      panel.innerHTML = '<input class="dp-search" placeholder="Поиск по буквам…"><div class="dp-list"></div>';

      const list = panel.querySelector('.dp-list');

      const fill = q => {
        list.innerHTML = '';

        membersOf(dp.id)
          .filter(u => !q || u.name.toLowerCase().includes(q))
          .forEach(u => {
            const lab = document.createElement('label');
            lab.className = 'dp-item';

            const cb = document.createElement('input');
            cb.type = 'checkbox';
            cb.style.width = 'auto';
            cb.checked = checked.has(String(u.id));

            cb.addEventListener('change', () => {
              if (cb.checked) {
                checked.add(String(u.id));
              } else {
                checked.delete(String(u.id));
              }

              syncHidden();
              renderChips();
            });

            lab.appendChild(cb);
            lab.appendChild(document.createTextNode(u.name));
            list.appendChild(lab);
          });
      };

      panel.querySelector('.dp-search').addEventListener('input', e =>
        fill(e.target.value.trim().toLowerCase())
      );

      panel.addEventListener('click', e => e.stopPropagation());

      fill('');
      wrap.appendChild(panel);
      panel.querySelector('.dp-search').focus();
    }

    /* ---------- чипы подразделений ---------- */
    function renderChips() {
      if (!chipsBox) return;

      chipsBox.innerHTML = '';

      DEPTS.forEach(dp => {
        const members = membersOf(dp.id);
        const sel = members.filter(m => checked.has(String(m.id))).length;

        const wrap = document.createElement('div');
        wrap.className = 'chip-wrap';

        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'chip' + (members.length && sel === members.length ? ' on' : '');

        const main = document.createElement('span');
        main.className = 'chip-main';
        main.textContent = dp.name + (sel ? ' · ' + sel : '');

        const edge = document.createElement('span');
        edge.className = 'chip-edge';
        edge.title = 'Выбрать конкретных людей';
        edge.textContent = '▾';

        main.addEventListener('click', e => {
          e.stopPropagation();

          const all = members.length && sel === members.length;

          members.forEach(m => {
            if (all) {
              checked.delete(String(m.id));
            } else {
              checked.add(String(m.id));
            }
          });

          syncHidden();
          renderChips();
          closePanel();
        });

        edge.addEventListener('click', e => {
          e.stopPropagation();
          openPanel(dp, wrap);
        });

        btn.appendChild(main);
        btn.appendChild(edge);
        wrap.appendChild(btn);
        chipsBox.appendChild(wrap);
      });
    }

    document.addEventListener('click', () => closePanel());
    renderChips();

    /* ---------- календари ---------- */
    const MONTHS = [
      'Январь',
      'Февраль',
      'Март',
      'Апрель',
      'Май',
      'Июнь',
      'Июль',
      'Август',
      'Сентябрь',
      'Октябрь',
      'Ноябрь',
      'Декабрь'
    ];

    const parseISO = s => {
      if (!s) return null;

      const p = String(s).split('-').map(Number);
      return p.length === 3 ? new Date(p[0], p[1] - 1, p[2]) : null;
    };

    const fmtISO = d =>
      d.getFullYear() + '-' +
      String(d.getMonth() + 1).padStart(2, '0') + '-' +
      String(d.getDate()).padStart(2, '0');

    const fmtRU = d =>
      String(d.getDate()).padStart(2, '0') + '.' +
      String(d.getMonth() + 1).padStart(2, '0') + '.' +
      d.getFullYear();

    function makeCalendar(cfg) {
      const cal = $(cfg.cal);
      const btn = $(cfg.btn);
      const inp = $(cfg.input);
      const view = $(cfg.view);

      if (!cal || !btn || !inp || !view) return null;

      let cur = new Date();
      let selected = parseISO(inp.value);
      const today = new Date();

      const setSelected = d => {
        selected = d;
        inp.value = d ? fmtISO(d) : '';
        view.value = d ? fmtRU(d) : '';
      };

      const render = () => {
        $(cfg.title).textContent = MONTHS[cur.getMonth()] + ' ' + cur.getFullYear();

        const grid = $(cfg.grid);
        grid.innerHTML = '';

        const first = new Date(cur.getFullYear(), cur.getMonth(), 1);
        const start = new Date(first);
        start.setDate(1 - ((first.getDay() + 6) % 7));

        for (let i = 0; i < 42; i++) {
          const d = new Date(start);
          d.setDate(start.getDate() + i);

          const b = document.createElement('button');
          b.type = 'button';
          b.textContent = d.getDate();

          b.className =
            'calday' +
            (d.getMonth() !== cur.getMonth() ? ' out' : '') +
            (selected && fmtISO(d) === fmtISO(selected) ? ' sel' : '') +
            (d.toDateString() === today.toDateString() ? ' today' : '');

          b.addEventListener('click', () => {
            setSelected(d);
            hide();
          });

          grid.appendChild(b);
        }
      };

      const show = () => {
        cal.hidden = false;
        render();
      };

      const hide = () => {
        cal.hidden = true;
      };

      btn.addEventListener('click', e => {
        e.stopPropagation();
        cal.hidden ? show() : hide();
      });

      view.addEventListener('click', e => {
        e.stopPropagation();
        show();
      });

      view.addEventListener('input', () => {
        const m = view.value.trim().match(/^(\d{1,2})\.(\d{1,2})\.(\d{4})$/);

        if (m) {
          const d = new Date(+m[3], +m[2] - 1, +m[1]);
          setSelected(d);
          cur = new Date(d);
        }
      });

      $(cfg.prev).addEventListener('click', () => {
        cur.setMonth(cur.getMonth() - 1);
        render();
      });

      $(cfg.next).addEventListener('click', () => {
        cur.setMonth(cur.getMonth() + 1);
        render();
      });

      $(cfg.today).addEventListener('click', () => {
        setSelected(new Date());
        cur = new Date();
        hide();
      });

      $(cfg.clear).addEventListener('click', () => {
        setSelected(null);
        hide();
      });

      document.addEventListener('click', e => {
        if (!cal.hidden && !cal.contains(e.target) && e.target !== btn && e.target !== view) {
          hide();
        }
      });

      render();

      return { setSelected };
    }

    const startCal = makeCalendar({
      cal: 'startcal',
      btn: 'startcalbtn',
      input: 'start_due',
      view: 'start_due_view',
      title: 'startcaltitle',
      grid: 'startcalgrid',
      prev: 'startprev',
      next: 'startnext',
      today: 'startcaltoday',
      clear: 'startcalclear'
    });

    const dueCal = makeCalendar({
      cal: 'cal',
      btn: 'calbtn',
      input: 'due',
      view: 'due_view',
      title: 'caltitle',
      grid: 'calgrid',
      prev: 'prev',
      next: 'next',
      today: 'caltoday',
      clear: 'calclear'
    });

    /* ---------- подсказка единиц плана ---------- */
    const scaleSel = $('scale');
    const planHint = $('planhint');

    if (scaleSel && planHint) {
      const UNIT_HINT = {
        xs: ['минутах', '1 = 1 мин'],
        s: ['часах', '1 = 1 ч'],
        m: ['днях', '1 = ' + NORM + ' ч (дневная норма)'],
        l: ['неделях', '1 = ' + (NORM * 5) + ' ч (5 рабочих дней)'],
        xl: ['месяцах', '1 = ' + (NORM * 21) + ' ч (21 рабочий день)']
      };

      const updHint = () => {
        const u = UNIT_HINT[scaleSel.value] || UNIT_HINT.s;

        planHint.textContent =
          'Масштаб «' + scaleSel.selectedOptions[0].textContent +
          '»: число понимается как ' + u[0] + ', ' + u[1] +
          '. Явные форматы тоже работают: «30 мин», «2 ч», «1:15», «3 д».';
      };

      scaleSel.addEventListener('change', updHint);
      updHint();
    }

    /* ---------- типовые задачи ---------- */
    const typeSel = $('type');

    if (typeSel && startCal && dueCal) {
      typeSel.addEventListener('change', () => {
        const o = typeSel.selectedOptions[0];
        if (!o || !o.value) return;

        const plan = $('plan');
        if (plan) plan.value = o.dataset.plan;

        const d0 = new Date();
        startCal.setSelected(d0);

        const d = new Date();
        d.setDate(d.getDate() + (+o.dataset.days || 0));
        dueCal.setSelected(d);
      });
    }
  } catch (err) {
    console.error('task_form init error:', err);
  }
})();
