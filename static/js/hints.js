/* СВОД · пульсирующие подсказки (Hints). Показываются новичкам один раз. */
(function () {
  'use strict';

  function getCookie(name) {
    var m = document.cookie.match('(^|; )' + name + '=([^;]*)');
    return m ? decodeURIComponent(m[2]) : '';
  }

  var cfg = document.getElementById('hints-cfg');
  if (!cfg) return;

  // Если Hints-модуль Driver.js не загружен — тихо выходим
  if (typeof window.driver === 'undefined' || !window.driver.hints) {
    return;
  }

  var SEEN       = cfg.dataset.seen === '1';
  var MARK_URL   = cfg.dataset.markUrl || '';
  var ONBOARDING = cfg.dataset.onboardingDone === '1';
  var IS_NEW     = cfg.dataset.isNew === '1';

  // Показываем, если: не видел и (новичок или онбординг не пройден)
  if (SEEN) return;
  if (!IS_NEW && ONBOARDING) return;

  var HINTS = [
    {
      element: '#tour-btn',
      hint: 'Здесь подсказки по любому разделу — можно пройти обучение за 5 минут.',
      hintPosition: 'bottom-middle',
    },
    {
      element: '#support-btn',
      hint: 'Жалоба, ошибка или идея? Нажмите сюда. Скриншот — Ctrl+V прямо в окне.',
      hintPosition: 'top-middle',
    },
    {
      element: '.ibtn[href*="notifications"]',
      hint: 'Уведомления о новых задачах, возвратах и приёмке.',
      hintPosition: 'bottom-middle',
    },
    {
      element: '.ibtn[href*="dialogs"]',
      hint: 'Чаты с коллегами и чат по каждой задаче.',
      hintPosition: 'bottom-middle',
    },
  ];

  // Фильтруем — оставляем только те, чей элемент реально есть на странице
  var valid = HINTS.filter(function (h) {
    return h.element && document.querySelector(h.element);
  });

  if (valid.length === 0) return;

  var hints = window.driver.hints({
    hints: valid,
    hintButtonLabel: 'Понятно',
    onHintClose: function () {
      if (!MARK_URL) return;
      fetch(MARK_URL, {
        method: 'POST',
        headers: {
          'X-CSRFToken': getCookie('csrftoken'),
          'X-Requested-With': 'XMLHttpRequest',
        },
        credentials: 'same-origin',
      }).catch(function () {});
    },
  });

  setTimeout(function () {
    hints.show();
  }, 1500);

  window.SVOD_hints = hints;
})();
