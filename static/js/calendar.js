/* calendar.js — универсальный календарь для СВОД.
 * Использование:
 *   makeCalendar({
 *     trigger: '#calbtn',   input: '#due',   view: '#due_view',
 *     popup:   '#cal',      title: '#caltitle', grid: '#calgrid',
 *     prev:    '#prev',     next:  '#next',
 *     today:   '#caltoday', clear: '#calclear',
 *   });
 */
(function () {
  'use strict';

  const MONTHS = ['Январь','Февраль','Март','Апрель','Май','Июнь',
                  'Июль','Август','Сентябрь','Октябрь','Ноябрь','Декабрь'];

  function fmtISO(d){ return d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0')+'-'+String(d.getDate()).padStart(2,'0'); }
  function fmtRU(d){ return String(d.getDate()).padStart(2,'0')+'.'+String(d.getMonth()+1).padStart(2,'0')+'.'+d.getFullYear(); }
  function parseISO(s){
    if (!s || !/^\d{4}-\d{2}-\d{2}$/.test(s)) return null;
    const [y,m,d] = s.split('-').map(Number);
    const dt = new Date(y, m-1, d);
    return isNaN(dt.getTime()) ? null : dt;
  }

  const openPopups = new Set();

  function closeAll(except){
    openPopups.forEach(e => {
      if (e !== except){ e.popup.hidden = true; openPopups.delete(e); }
    });
  }

  function makeCalendar(opts){
    const trigger = document.querySelector(opts.trigger);
    const input   = document.querySelector(opts.input);
    const view    = opts.view ? document.querySelector(opts.view) : null;
    const popup   = document.querySelector(opts.popup);
    const title   = document.querySelector(opts.title);
    const grid    = document.querySelector(opts.grid);
    const prev    = opts.prev  ? document.querySelector(opts.prev)  : null;
    const next    = opts.next  ? document.querySelector(opts.next)  : null;
    const todayBt = opts.today ? document.querySelector(opts.today) : null;
    const clearBt = opts.clear ? document.querySelector(opts.clear) : null;

    if (!popup || !grid) return;

    const entry = { popup };
    const now = new Date();
    let cur = new Date(now.getFullYear(), now.getMonth(), 1);
    let selected = parseISO(input ? input.value : '');

    function render(){
      if (title) title.textContent = MONTHS[cur.getMonth()] + ' ' + cur.getFullYear();
      grid.innerHTML = '';
      const first = new Date(cur.getFullYear(), cur.getMonth(), 1);
      const start = new Date(first);
      start.setDate(1 - ((first.getDay() + 6) % 7));
      for (let i = 0; i < 42; i++){
        const d = new Date(start);
        d.setDate(start.getDate() + i);
        const b = document.createElement('button');
        b.type = 'button';
        b.textContent = d.getDate();
        b.className = 'cal-day';
        if (d.getMonth() !== cur.getMonth()) b.classList.add('out');
        if (selected && fmtISO(d) === fmtISO(selected)) b.classList.add('sel');
        if (d.toDateString() === now.toDateString()) b.classList.add('today');
        b.addEventListener('click', () => {
          selected = d;
          if (input) input.value = fmtISO(d);
          if (view)  view.value  = fmtRU(d);
          close();
        });
        grid.appendChild(b);
      }
    }

    function open(){ closeAll(entry); popup.hidden = false; openPopups.add(entry); render(); }
    function close(){ popup.hidden = true; openPopups.delete(entry); }

    if (trigger) trigger.addEventListener('click', e => {
      e.stopPropagation(); e.preventDefault();
      popup.hidden ? open() : close();
    });

    if (view) view.addEventListener('click', e => {
      e.stopPropagation();
      popup.hidden ? open() : close();
    });

    if (prev)    prev.addEventListener('click',    e => { e.stopPropagation(); cur.setMonth(cur.getMonth()-1); render(); });
    if (next)    next.addEventListener('click',    e => { e.stopPropagation(); cur.setMonth(cur.getMonth()+1); render(); });
    if (todayBt) todayBt.addEventListener('click', e => {
      e.stopPropagation();
      selected = new Date();
      cur = new Date(selected.getFullYear(), selected.getMonth(), 1);
      if (input) input.value = fmtISO(selected);
      if (view)  view.value  = fmtRU(selected);
      close();
    });
    if (clearBt) clearBt.addEventListener('click', e => {
      e.stopPropagation();
      selected = null;
      if (input) input.value = '';
      if (view)  view.value  = '';
      close();
    });

    if (selected && view) view.value = fmtRU(selected);
    popup.addEventListener('click', e => e.stopPropagation());
  }

  document.addEventListener('click', () => closeAll());
  window.makeCalendar = makeCalendar;
})();
