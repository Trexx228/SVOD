/* СВОД · онбординг-кампания. Карточка «следующий шаг» + финальный экран. */
(function () {
  'use strict';

  function getCookie(name) {
    var m = document.cookie.match('(^|; )' + name + '=([^;]*)');
    return m ? decodeURIComponent(m[2]) : '';
  }

  var cfg = document.getElementById('onboarding-cfg');
  if (!cfg) return;

  var OFFER    = cfg.dataset.offer === '1';
  var DONE_CNT = parseInt(cfg.dataset.done || '0', 10);
  var TOTAL    = parseInt(cfg.dataset.total || '0', 10);
  var NEXT_URL = cfg.dataset.nextUrl || '';
  var NEXT_URL_NAME = cfg.dataset.nextUrlName || '';
  var CURRENT_URL_NAME = cfg.dataset.currentUrlName || '';
  var FINISH_URL = cfg.dataset.finishUrl || '';
  var NEXT_URL_TPL = cfg.dataset.nextJsonUrl || '';

  // ── Карточка «Следующий шаг» после тура ─────────────────────
  function buildNextCard(data) {
    var el = document.createElement('div');
    el.className = 'onb-toast';
    el.innerHTML =
      '<div class="onb-toast-head">' +
        '<span class="onb-toast-badge">Шаг ' + data.done + ' из ' + data.total + '</span>' +
        '<button type="button" class="onb-toast-close" aria-label="Закрыть">✕</button>' +
      '</div>' +
      '<div class="onb-toast-title">' + (data.next ? data.next.title : 'Обучение пройдено') + '</div>' +
      '<div class="onb-toast-hint">' + (data.next ? data.next.hint : 'Нажмите «Готово», чтобы закрыть маршрут.') + '</div>' +
      '<div class="onb-toast-actions">' +
        (data.is_done
          ? '<button type="button" class="btn green" data-onb-finish>Готово</button>'
          : '<a class="btn blue" href="' + (data.next ? data.next.url : '#') + '" data-onb-go>Дальше →</a>') +
        '<button type="button" class="btn gray" data-onb-later>Позже</button>' +
      '</div>';

    el.querySelector('.onb-toast-close').onclick = function () { el.remove(); };
    el.querySelector('[data-onb-later]').onclick = function () {
      el.remove();
      fetch('/accounts/onboarding/skip/', {
        method: 'POST',
        headers: { 'X-CSRFToken': getCookie('csrftoken'), 'X-Requested-With': 'XMLHttpRequest' },
        credentials: 'same-origin',
      }).catch(function () {});
    };
    var go = el.querySelector('[data-onb-go]');
    if (go) go.addEventListener('click', function () { el.remove(); });

    var fin = el.querySelector('[data-onb-finish]');
    if (fin) fin.addEventListener('click', function () {
      fetch(FINISH_URL, {
        method: 'POST',
        headers: { 'X-CSRFToken': getCookie('csrftoken'), 'X-Requested-With': 'XMLHttpRequest' },
        credentials: 'same-origin',
      }).then(function () {
        el.remove();
        showFinalScreen();
      });
    });

    document.body.appendChild(el);
  }

  // ── Финальный экран ──────────────────────────────────────────
  function showFinalScreen() {
    var ov = document.createElement('div');
    ov.className = 'onb-final-overlay';
    ov.innerHTML =
      '<div class="onb-final">' +
        '<div class="onb-final-emoji">🎓</div>' +
        '<h2>Ты готов!</h2>' +
        '<p>Основные разделы СВОД пройдены. Дальше — работа: ставьте задачи, ведите таймер, сдавайте в срок.</p>' +
        '<div class="onb-final-tip">' +
          '💡 Подсказки всегда доступны: иконка <b>?</b> в шапке.' +
        '</div>' +
        '<div class="onb-final-actions">' +
          '<a href="/" class="btn green">Перейти к задачам</a>' +
        '</div>' +
      '</div>';
    document.body.appendChild(ov);
    ov.addEventListener('click', function (e) {
      if (e.target === ov) ov.remove();
    });
  }

  // ── Обработка закрытия тура ──────────────────────────────────
  // Слушаем кастомное событие от tour.js
  document.addEventListener('svod:tourClosed', function (e) {
    var tourId = e.detail && e.detail.tourId;
    if (!tourId) return;

    // Запрашиваем актуальный прогресс
    fetch('/accounts/onboarding/next/', {
      headers: { 'X-Requested-With': 'XMLHttpRequest' },
      credentials: 'same-origin',
    })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        // Если мы прошли все — показываем финальный экран, иначе карточку «Дальше»
        if (data.is_done && !data.already_done) {
          buildNextCard(data);
        } else if (!data.is_done) {
          buildNextCard(data);
        }
      })
      .catch(function () {});
  });

  // ── Кнопка «Начать обучение» (из карточки-оффера) ────────────
  document.addEventListener('click', function (e) {
    if (e.target.closest('[data-onb-start]')) {
      e.preventDefault();
      window.location.href = '/accounts/onboarding/start/';
    }
  });
})();
