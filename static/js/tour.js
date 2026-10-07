/* СВОД · обучалка. Driver.js туры по страницам. */
(function () {
  'use strict';

  function exists(sel) { return sel ? document.querySelector(sel) : null; }

  function getCookie(name) {
    var m = document.cookie.match('(^|; )' + name + '=([^;]*)');
    return m ? decodeURIComponent(m[2]) : '';
  }

  // ── Каталог туров ────────────────────────────────────────────
  var TOURS = {

    dashboard: {
      title: 'Главная: мои задачи',
      description: 'Фокусная задача, таймер, очередь приёмки.',
      steps: [
        { element: '.focus', side: 'bottom',
          title: 'Фокусная задача',
          description: 'Это ваша текущая задача — обычно та, что уже в работе. Здесь весь основной сценарий: старт, пауза, сдача на проверку.' },
        { element: '.focus .timer', side: 'bottom',
          title: 'Таймер',
          description: 'Идёт отсчёт. Всё, что потратили — автоматически попадает в учёт часов и в KPI.' },
        { element: '.focus .actions', side: 'top',
          title: 'Кнопки действий',
          description: 'Начать / Пауза / На проверку / Выход в цех / Чат. Если уходите в цех — обязательно нажмите «Выход в цех», это отдельный учёт.' },
        { element: '.cards', side: 'top',
          title: 'Мои задачи',
          description: 'Остальные открытые задачи. Цвет кромки — приоритет. Дата красная — просрочено, оранжевая — сегодня.' },
        { element: '#review, .cc-review, .card:has(.cc-review)', side: 'top',
          title: 'Ждут моей приёмки',
          description: 'Задачи, сданные исполнителями. Принять или вернуть на доработку — прямо отсюда.' },
        { element: '.side .card', side: 'left',
          title: 'KPI, уровень, ритм',
          description: 'Боковая панель: KPI за месяц, уровень, серия без просрочек, последние благодарности.' },
        { element: '#bell-badge, .ibtn[href*="notifications"]', side: 'bottom',
          title: 'Уведомления и чаты',
          description: 'Счётчики непрочитанного. Сигнал при новых событиях, если включён звук.' },
        { element: '#support-btn', side: 'left',
          title: 'Техподдержка',
          description: 'Баг или идея? Жмите сюда. Скриншот — Ctrl+V прямо в окне поддержки.' },
      ],
    },

    task_create: {
      title: 'Постановка задачи',
      description: 'Заказ, этап, срок, исполнители, оценка времени.',
      steps: [
        { element: '#tf-types', side: 'bottom',
          title: 'Типовая задача',
          description: 'Часто повторяющиеся работы — одной кнопкой. План и срок подставятся автоматически.' },
        { element: 'input[name="title"]', side: 'bottom',
          title: 'Что нужно сделать',
          description: 'Коротко и по существу. Формулировка попадёт в дерево, отчёты и рейтинг.' },
        { element: '#tf-prio', side: 'bottom',
          title: 'Приоритет',
          description: '«Срочно» сдвигает сроки всех открытых задач исполнителя. Использовать осознанно.' },
        { element: '#tf-order-wrap', side: 'bottom',
          title: 'Заказ',
          description: 'Если работа внутри заказа — выберите. Если внутренняя — «Не касается заказа».' },
        { element: '#tf-branch-wrap', side: 'bottom',
          title: 'Этап заказа',
          description: 'Если у заказа есть цепочка этапов, задача встанет в конец. Активируется после закрытия предыдущего.' },
        { element: '#tf-due', side: 'bottom',
          title: 'Срок',
          description: 'Пресеты «Сегодня / Завтра / Через неделю» или своя дата. Без срока задача не попадёт в хронологию.' },
        { element: '#tf-exec', side: 'bottom',
          title: 'Кому поручить',
          description: 'Один — обычная задача. Несколько — групповая: каждому создастся подзадача.' },
        { element: '#tf-time', side: 'bottom',
          title: 'Оценка времени',
          description: 'Сколько планируете потратить. Пресеты подстраиваются под срок. Можно ввести «1:30», «30 мин», «8».' },
        { element: '.tf-more', side: 'top',
          title: 'Дополнительно',
          description: 'Срок начала, вид работы, что считается результатом, вложения. Чем точнее — тем меньше возвратов.' },
      ],
    },

    task_detail: {
      title: 'Карточка задачи',
      description: 'Ключевое, действия, история, цепочки.',
      steps: [
        { element: '.task-head-info', side: 'bottom',
          title: 'Ключевая информация',
          description: 'Исполнитель, постановщик, статус, заказ, план/факт. Всё важное — на первом экране.' },
        { element: '.task-actions-bar', side: 'top',
          title: 'Действия',
          description: 'Таймер, чат, подзадача. Руководитель дополнительно видит «Задача решена» и «Отменить».' },
        { element: '.chain', side: 'top',
          title: 'Цепочка этапов',
          description: 'Если задача в ветке — вся последовательность. «ВЫ ЗДЕСЬ» — текущий этап.' },
      ],
    },

    task_tree: {
      title: 'Дерево задач',
      description: 'Иерархия и декомпозиция.',
      steps: [
        { element: '.periodbar', side: 'bottom',
          title: 'Период и фильтр',
          description: 'Ограничьте диапазон по дате создания. Переключайте «открытые / все / мои».' },
        { element: '.tree', side: 'right',
          title: 'Дерево',
          description: 'Клик по ▾ сворачивает ветку. Кромка узла: красная — просрочка, синяя — на проверке, зелёная — готово.' },
      ],
    },

    task_timeline: {
      title: 'Хронология',
      description: 'Диаграмма Ганта.',
      steps: [
        { element: '.pills', side: 'bottom',
          title: 'Легенда',
          description: 'Как читать цвета полос: в работе, высокий приоритет, просрочка, закрыта, отменена.' },
        { element: 'table', side: 'right',
          title: 'Полосы задач',
          description: 'Каждая строка — задача. Полоса = интервал от старта до дедлайна. Красное/штриховое — просрочка.' },
      ],
    },

    task_calendar: {
      title: 'Календарь',
      description: 'Задачи со сроком в этом месяце.',
      steps: [
        { element: '.calgrid-m', side: 'bottom',
          title: 'Сетка месяца',
          description: 'Красные задачи — просрочены. Клик открывает карточку.' },
      ],
    },

    me_home: {
      title: 'Личный кабинет',
      description: 'Цифры, задачи, часы.',
      steps: [
        { element: '.me-tabs', side: 'bottom',
          title: 'Разделы кабинета',
          description: 'Задачи, часы, KPI, действия, профиль, настройки. Всё, что касается лично вас.' },
        { element: '.me-grid', side: 'bottom',
          title: 'Мои цифры',
          description: 'Активные, закрытые, просрочки, часы работы и цеха.' },
      ],
    },

    me_kpi: {
      title: 'Мой KPI',
      description: 'Как читать показатели.',
      steps: [
        { element: '.mk-grid', side: 'bottom',
          title: 'Сводка за период',
          description: 'KPI = план/факт. Здоровая зона 90–110%. Ниже — не уложились, выше — переработка.' },
        { element: 'table', side: 'top',
          title: 'Разбивка по дням',
          description: 'Видно, в какие дни закрывали задачи и как факт соотносится с планом.' },
      ],
    },

    me_settings: {
      title: 'Настройки',
      description: 'Тема, шрифт, письма, фон.',
      steps: [
        { element: '.cs-swatches', side: 'bottom',
          title: 'Тема оформления',
          description: '7 тем на выбор. Сохраняется на вашем аккаунте.' },
        { element: '.cs-section:has(input[name="email_notifications"])', side: 'bottom',
          title: 'Email-уведомления',
          description: 'Письма о новых задачах, возвратах и утренний дайджест. Можно выключить.' },
        { element: '.cs-section:has(input[name="bg_style"])', side: 'bottom',
          title: 'Фон',
          description: 'Градиент, однотонный, «мягкие пятна», сетка или своя картинка.' },
      ],
    },

    manager_cabinet: {
      title: 'Кабинет руководителя',
      description: 'Сводка, алерты, приёмка.',
      steps: [
        { element: '.cc-digest', side: 'bottom',
          title: 'Дайджест',
          description: 'Итоги месяца одной строкой. Удобно копировать на планёрку.' },
        { element: '.cc-tiles', side: 'bottom',
          title: 'Плитки',
          description: 'Клик открывает соответствующий отчёт: месяц, приёмка, динамика, завод.' },
        { element: '.cc-two', side: 'top',
          title: 'Алерты',
          description: 'Красное — просрочки >7 дней. Оранжевое — застывшие задачи. Каждую можно открыть и разобрать.' },
        { element: '#review', side: 'top',
          title: 'Очередь приёмки',
          description: 'Принять или вернуть. Комментарий к возврату попадает в отчёт качества.' },
      ],
    },

    manager_team: {
      title: 'Команда',
      description: 'Нагрузка и метрики сотрудников.',
      steps: [
        { element: 'table', side: 'top',
          title: 'Таблица',
          description: 'Загрузка = часы / (норма × рабочие дни). 70–110% — здоровая зона. Клик по имени — карточка.' },
      ],
    },

        manager_review: {
          title: 'Очередь приёмки',
          description: 'Принять или вернуть с комментарием.',
          steps: [
            { element: 'table', side: 'top',
              title: 'Список задач на приёмке',
              description: 'Проверьте факт часов, посмотрите вложения и комментарии исполнителя. Принять — «Принять», вернуть — «На доработку».' },
            { element: '.card:has(.list-group)', side: 'top',
              title: 'Причины возвратов',
              description: 'За месяц: что чаще всего возвращали и с какими комментариями. Видно системные проблемы.' },
          ],
        },

        manager_flow: {
          title: 'Загрузка команды',
          description: 'Возраст открытых задач по бакетам.',
          steps: [
            { element: '.row', side: 'bottom',
              title: 'Корзины по возрасту',
              description: '0–3 дня — свежие. 8+ дней — кандидаты на разбор: забыты или ждут смежников. 22+ — красный флаг.' },
            { element: '.card:has([style*="align-items:flex-end"])', side: 'top',
              title: 'Динамика 8 недель',
              description: 'Сколько задач закрыто за каждую неделю. Падение — сигнал перегрузки или застоя.' },
          ],
        },

        manager_analytics: {
          title: 'Динамика и качество',
          description: 'Дельты, цикл, точность.',
          steps: [
            { element: '.row.g-3', side: 'bottom',
              title: 'Дельта к прошлому месяцу',
              description: 'Закрытые задачи и часы: рост / падение в %. Медианный цикл — сколько дней задача живёт от создания до закрытия.' },
            { element: '.card:has(table)', side: 'top',
              title: 'Точность оценок по видам работ',
              description: 'Если точность ниже 50% — норма вида работы требует ревизии. Откройте нормы, чтобы поправить.' },
          ],
        },

        manager_orders: {
          title: 'Паспорта заказов',
          description: 'Все заказы, прогресс, узкие места.',
          steps: [
            { element: '.card', side: 'bottom',
              title: 'Форма нового заказа',
              description: 'Номер, изделие, срок отгрузки через календарь.' },
            { element: '.card:has(table)', side: 'top',
              title: 'Список заказов',
              description: 'Прогресс, дни до отгрузки, узкие места по подразделениям. Клик — план заказа.' },
          ],
        },

        order_plan: {
          title: 'План заказа',
          description: 'Направления, этапы, drag-n-drop.',
          steps: [
            { element: '.op-now', side: 'bottom',
              title: 'Что делать сейчас',
              description: 'Первый незавершённый этап. Это то, на что смотрит руководитель, открывая заказ.' },
            { element: '.dir', side: 'top',
              title: 'Направления работ',
              description: 'Каждое направление — цепочка последовательных этапов. Этап запускается автоматически после закрытия предыдущего.' },
            { element: '.drag-handle', side: 'right',
              title: 'Перетаскивание',
              description: 'Тяните за ☰ — можно менять порядок и переносить между направлениями. Порядок сохраняется автоматически.' },
            { element: '.add-stage', side: 'top',
              title: 'Добавить этап',
              description: 'Раскройте, заполните строку. Этап встанет в конец цепочки направления.' },
          ],
        },

        manager_plant: {
          title: 'Свод по заводу',
          description: 'Все подразделения одним экраном.',
          steps: [
            { element: '.alert', side: 'bottom',
              title: 'Как читать',
              description: 'KPI = план/факт (90–110% — здоровая зона). Загрузка = факт / (норма × рабочие дни × люди).' },
            { element: 'table', side: 'top',
              title: 'Таблица',
              description: 'Просрочки, отложенные срочные, «впустую» часы, возвраты. Красное — проблемные зоны.' },
          ],
        },

        manager_plant_live: {
          title: 'Кто чем занят',
          description: 'Живой экран на 30 секунд.',
          steps: [
            { element: '.live-tiles', side: 'bottom',
              title: 'Плитки-фильтры',
              description: 'Онлайн / в работе / в цеху / невзятые срочные. Клик — фильтр таблицы.' },
            { element: '.live-grid', side: 'top',
              title: 'Карточки сотрудников',
              description: 'Текущая задача, время в цеху, просрочки, невзятые срочные. Клик по имени — карточка.' },
          ],
        },

        manager_online: {
          title: 'Кто онлайн',
          description: 'Активность за 15 минут.',
          steps: [
            { element: '.on-grid', side: 'top',
              title: 'Онлайн и недавние',
              description: 'Слева — активные сейчас с текущей задачей. Справа — заходили за час и давно не заходили.' },
          ],
        },

        manager_task_logs: {
          title: 'Логи задач отдела',
          description: 'Кто, что, когда менял.',
          steps: [
            { element: '.tl-stats', side: 'bottom',
              title: 'Статистика по типам',
              description: 'Сколько возвратов, приёмок, сдвигов. Клик — фильтр.' },
            { element: '.tl-filter', side: 'bottom',
              title: 'Фильтры',
              description: 'По автору, задаче, датам. Полезно при разборе инцидентов.' },
          ],
        },

        settings_home: {
          title: 'Админка портала',
          description: 'Обзор, счётчики, онбординг-чеклист.',
          steps: [
            { element: '.dash-grid', side: 'bottom',
              title: 'Счётчики',
              description: 'Пользователи, админы, задачи, отказы входа, обращения. Красное — требует внимания.' },
            { element: '.dash-cols', side: 'top',
              title: 'Два столбца',
              description: 'Слева — последние действия из аудита. Справа — «Требует внимания» с быстрыми ссылками.' },
            { element: '.set-tabs', side: 'bottom',
              title: 'Навигация',
              description: 'Группы: Персонал, Справочники, Безопасность, Система. Поиск — справа.' },
          ],
        },

        settings_users: {
          title: 'Пользователи',
          description: 'Список, фильтры, массовые операции.',
          steps: [
            { element: '.user-filter', side: 'bottom',
              title: 'Фильтры',
              description: 'Поиск по ФИО и email, отбор по подразделению, роли, активности.' },
            { element: '.bulk-bar', side: 'bottom',
              title: 'Массовые действия',
              description: 'Отметьте чекбоксы → выберите действие: активировать, деактивировать, назначить роль или отдел.' },
          ],
        },

        settings_backups: {
          title: 'Бэкапы',
          description: 'Создание, скачивание, удаление.',
          steps: [
            { element: '.bk-warn', side: 'bottom',
              title: 'Важно',
              description: 'Папка не раздаётся через веб. Копируйте на отдельный диск.' },
            { element: '.btn.green', side: 'bottom',
              title: 'Создать бэкап',
              description: 'Консистентный снимок SQLite через VACUUM INTO. Работает без блокировки.' },
          ],
        },

        settings_health: {
          title: 'Здоровье системы',
          description: 'Проверки БД, диска, бэкапов, AD.',
          steps: [
            { element: '.h-banner', side: 'bottom',
              title: 'Общий статус',
              description: 'Критично — БД или диск. Форматы: JSON, текст для Zabbix.' },
            { element: '.h-grid', side: 'top',
              title: 'Детали',
              description: 'Каждая карточка — отдельная проверка. Зелёная — ok, красная — fail.' },
          ],
        },

        settings_maintenance: {
          title: 'Пульт обслуживания',
          description: 'Одной страницей всё для эксплуатации.',
          steps: [
            { element: '.mt-grid', side: 'top',
              title: 'Операции',
              description: 'Health-check, пересчёт простоя, бэкап, очистка, экспорт ZIP, мониторинг.' },
          ],
        },

        settings_integrity: {
          title: 'Целостность данных',
          description: '16 проверок на битые связи.',
          steps: [
            { element: '.ic-grid', side: 'bottom',
              title: 'Плитки проверок',
              description: 'Клик по плитке — переход к проблемным записям. Красное — критично.' },
          ],
        },

        settings_audit: {
          title: 'Журнал аудита',
          description: 'Кто, что, когда делал.',
          steps: [
            { element: 'form', side: 'bottom',
              title: 'Фильтры',
              description: 'Поиск по объекту, ФИО или email автора.' },
            { element: 'table', side: 'top',
              title: 'Записи',
              description: 'Каждое изменение фиксируется. JSON-изменения, IP, модель.' },
          ],
        },

        settings_alerts: {
          title: 'Уведомления админа',
          description: 'Срабатывания: серия отказов, один админ.',
          steps: [
            { element: '.card', side: 'bottom',
              title: 'Карточки',
              description: 'Severity: info / warn / danger. Кнопка «Прочитано» — снять с учёта.' },
          ],
        },

    dialogs: {
      title: 'Чаты',
      description: 'Личные и по задачам.',
      steps: [
        { element: '.tg-new', side: 'bottom',
          title: 'Новый диалог',
          description: 'Личный чат с любым сотрудником. Чат задачи открывается из карточки задачи кнопкой «Чат задачи».' },
        { element: '.tg-list', side: 'right',
          title: 'Список',
          description: 'Красный счётчик — непрочитанные. 📋 — чат по задаче.' },
        { element: '.tg-input', side: 'top',
          title: 'Поле ввода',
          description: 'Enter — отправить. Shift+Enter — новая строка. 📎 или Ctrl+V — вложить файл.' },
      ],
    },

    notifications: {
      title: 'Уведомления',
      description: 'Личные события.',
      steps: [
        { element: 'table', side: 'top',
          title: 'Список',
          description: 'Непрочитанные — с подсветкой. Клик отмечает прочитанным и ведёт к задаче.' },
      ],
    },

    rating: {
      title: 'Рейтинг',
      description: 'Топ-20 за месяц.',
      steps: [
        { element: 'table', side: 'top',
          title: 'Таблица',
          description: 'По числу закрытых задач и объёму плана. KPI — для контекста.' },
      ],
    },

    motivation: {
      title: 'Мотивация',
      description: 'KPI, уровень, серия, достижения.',
      steps: [
        { element: '#kpi', side: 'bottom',
          title: 'KPI за месяц',
          description: 'План/факт по закрытым задачам. Работает при факте ≥ 1 ч.' },
        { element: '#level', side: 'bottom',
          title: 'Уровень',
          description: 'Стажёр → Специалист → Профи → Мастер. По закрытым часам.' },
        { element: '#streak', side: 'bottom',
          title: 'Ритм',
          description: 'Дни подряд без просрочек. Рекорд хранится.' },
        { element: '#achievements', side: 'bottom',
          title: 'Достижения',
          description: 'Снайпер, Надёжный, Скоростной, Месяц на ритме. 🔒 подсказывает, за что дадут.' },
        { element: '#kudos', side: 'top',
          title: 'Благодарности',
          description: 'Полученные и отправленные кудосы.' },
      ],
    },

    // ── Админка портала ──
    settings_home: {
      title: 'Админка портала',
      description: 'Обзор, счётчики, онбординг-чеклист.',
      steps: [
        { element: '.dash-grid', side: 'bottom',
          title: 'Счётчики',
          description: 'Пользователи, админы, задачи, отказы входа, обращения. Красное — требует внимания.' },
        { element: '.dash-cols', side: 'top',
          title: 'Два столбца',
          description: 'Слева — последние действия из аудита. Справа — блок «Требует внимания» с быстрыми ссылками.' },
        { element: '.set-tabs', side: 'bottom',
          title: 'Навигация',
          description: 'Группы: Персонал, Справочники, Безопасность, Система. Поиск — справа.' },
      ],
    },

    settings_users: {
      title: 'Пользователи',
      description: 'Список, фильтры, массовые операции.',
      steps: [
        { element: '.user-filter', side: 'bottom',
          title: 'Фильтры',
          description: 'Поиск по ФИО и email, отбор по подразделению, роли, активности.' },
        { element: '.bulk-bar', side: 'bottom',
          title: 'Массовые действия',
          description: 'Отметьте чекбоксы → выберите действие: активировать, деактивировать, назначить роль или отдел.' },
        { element: '.btn.gray', side: 'bottom',
          title: 'Экспорт / импорт',
          description: 'CSV с BOM для Excel. Импорт — с двухстадийной проверкой файла.' },
      ],
    },

    settings_backups: {
      title: 'Бэкапы',
      description: 'Создание, скачивание, удаление резервных копий.',
      steps: [
        { element: '.bk-warn', side: 'bottom',
          title: 'Важно',
          description: 'Папка не раздаётся через веб. Скачивание доступно только админам. Копируйте на отдельный диск.' },
        { element: '.btn.green', side: 'bottom',
          title: 'Создать бэкап',
          description: 'Консистентный снимок SQLite через VACUUM INTO. Работает без блокировки.' },
      ],
    },

    settings_health: {
      title: 'Здоровье системы',
      description: 'Проверки БД, диска, бэкапов, AD.',
      steps: [
        { element: '.h-banner', side: 'bottom',
          title: 'Общий статус',
          description: 'Критично — БД или диск. Внимание — остальное. Форматы: JSON, текст для Zabbix.' },
        { element: '.h-grid', side: 'top',
          title: 'Детали',
          description: 'Каждая карточка — отдельная проверка. Зелёная — ok, красная — fail.' },
      ],
    },

    settings_maintenance: {
      title: 'Пульт обслуживания',
      description: 'Одной страницей всё, что нужно для эксплуатации.',
      steps: [
        { element: '.mt-grid', side: 'top',
          title: 'Операции',
          description: 'Health-check, пересчёт простоя, бэкап, очистка старых записей, экспорт ZIP, мониторинг.' },
      ],
    },

    settings_integrity: {
      title: 'Целостность данных',
      description: '16 проверок на битые связи и аномалии.',
      steps: [
        { element: '.ic-grid', side: 'bottom',
          title: 'Плитки проверок',
          description: 'Клик по плитке — переход к списку проблемных записей. Красное — критично.' },
      ],
    },

    settings_audit: {
      title: 'Журнал аудита',
      description: 'Кто, что, когда делал.',
      steps: [
        { element: 'form', side: 'bottom',
          title: 'Фильтры',
          description: 'Поиск по объекту, ФИО или email автора. Отбор по действию и модели.' },
        { element: 'table', side: 'top',
          title: 'Записи',
          description: 'Каждое изменение фиксируется. JSON-изменения, IP, модель, объект.' },
      ],
    },

    settings_alerts: {
      title: 'Уведомления админа',
      description: 'Срабатывания: серия отказов, один админ, отсутствие нормы.',
      steps: [
        { element: '.card', side: 'bottom',
          title: 'Карточки',
          description: 'Severity: info / warn / danger. Кнопка «Прочитано» — снять с учёта.' },
      ],
    },
  };

    // ── Куда вести, если элементов тура нет на текущей странице ──
    var TOUR_URLS = {
      dashboard:            '/',
      task_create:          '/tasks/create/',
      task_tree:            '/tasks/tree/',
      task_timeline:        '/tasks/timeline/',
      task_calendar:        '/tasks/calendar/',

      me_home:              '/me/',
      me_kpi:               '/me/kpi/',
      me_settings:          '/me/settings/',

      manager_cabinet:      '/manager/cabinet/',
      manager_team:         '/manager/team/',
      manager_review:       '/manager/review/',
      manager_flow:         '/manager/flow/',
      manager_analytics:    '/manager/analytics/',
      manager_orders:       '/manager/orders/',
      manager_plant:        '/manager/plant/',
      manager_plant_live:   '/manager/plant/live/',
      manager_online:       '/manager/online/',
      manager_task_logs:    '/manager/logs/',

      dialogs:              '/comms/',
      notifications:        '/notifications/',
      rating:               '/rating/',
      motivation:           '/motivation/',

      settings_home:        '/settings/',
      settings_users:       '/settings/users/',
      settings_backups:     '/settings/backups/',
      settings_health:      '/settings/health/',
      settings_maintenance: '/settings/maintenance/',
      settings_integrity:   '/settings/integrity/',
      settings_audit:       '/settings/audit/',
      settings_alerts:      '/settings/alerts/',
    };

  // ── Инициализация ─────────────────────────────────────────────
  var cfg = document.getElementById('tour-cfg');
  if (!cfg) return;
  if (typeof window.driver === 'undefined' || !window.driver.js) return;

  var driverFactory = window.driver.js.driver;

  var CURRENT       = cfg.dataset.current || '';
  var PENDING       = cfg.dataset.pending === '1';
  var AUTOSTART     = cfg.dataset.autostart === '1';
  var SEEN_URL_TPL  = cfg.dataset.seenUrlTemplate || '';
  var RESET_URL     = cfg.dataset.resetUrl || '';

  function resolveSteps(tourId) {
    var tour = TOURS[tourId];
    if (!tour) return [];
    var out = [];
    tour.steps.forEach(function (s) {
      if (!exists(s.element)) return;
      out.push({
        element: s.element,
        popover: {
          title: s.title,
          description: s.description,
          side: s.side || 'bottom',
          align: s.align || 'start',
        },
      });
    });
    return out;
  }

  function markSeen(tourId) {
    if (!tourId || !SEEN_URL_TPL) return;
    var url = SEEN_URL_TPL.replace('__tour__', encodeURIComponent(tourId));
    fetch(url, {
      method: 'POST',
      headers: {
        'X-CSRFToken': getCookie('csrftoken'),
        'X-Requested-With': 'XMLHttpRequest',
      },
      credentials: 'same-origin',
    }).catch(function () {});
  }

  var activeDriver = null;

  function startTour(tourId, opts) {
    opts = opts || {};
    var tour = TOURS[tourId];
    if (!tour) return false;

    var steps = resolveSteps(tourId);
    if (steps.length < 1) return false;

    if (activeDriver) {
      try { activeDriver.destroy(); } catch (e) {}
      activeDriver = null;
    }

    var markOnClose = (opts.markSeen !== false) && (tourId === CURRENT || opts.forceMark);

    activeDriver = driverFactory({
      showProgress: true,
      progressText: 'Шаг {{current}} из {{total}}',
      nextBtnText: 'Дальше',
      prevBtnText: 'Назад',
      doneBtnText: 'Понятно',
      allowClose: true,
      overlayColor: 'rgba(0,0,0,.72)',
      stagePadding: 6,
      stageRadius: 12,
      smoothScroll: true,
      steps: steps,
      onDestroyed: function () {
        if (markOnClose) markSeen(tourId);
        activeDriver = null;
        closeMenu();
        document.dispatchEvent(new CustomEvent('svod:tourClosed', {
          detail: { tourId: tourId }
        }));
      },
    });

    activeDriver.drive();
    return true;
  }

  var menuEl = null;

  function buildMenu() {
    var wrap = document.createElement('div');
    wrap.className = 'tour-menu';
    wrap.hidden = true;

    var head = document.createElement('div');
    head.className = 'tour-menu-head';
    head.innerHTML = '<b>Обучение</b>' +
      '<button type="button" class="tour-menu-close" aria-label="Закрыть">✕</button>';
    wrap.appendChild(head);

    var list = document.createElement('div');
    list.className = 'tour-menu-list';

    Object.keys(TOURS).forEach(function (id) {
      var b = document.createElement('button');
      b.type = 'button';
      b.className = 'tour-menu-item';
      b.dataset.tour = id;
      b.innerHTML =
        '<span class="tour-menu-item-title">' + TOURS[id].title + '</span>' +
        '<span class="tour-menu-item-desc">' + TOURS[id].description + '</span>';
      list.appendChild(b);
    });

    wrap.appendChild(list);

    var foot = document.createElement('div');
    foot.className = 'tour-menu-foot';
    foot.innerHTML = '<button type="button" class="tour-menu-reset">Пройти все туры заново</button>';
    wrap.appendChild(foot);

    document.body.appendChild(wrap);
    return wrap;
  }

  function openMenu(anchor) {
    if (!menuEl) menuEl = buildMenu();
    menuEl.hidden = false;
    if (anchor) {
      var r = anchor.getBoundingClientRect();
      menuEl.style.position = 'fixed';
      menuEl.style.top = (r.bottom + 8) + 'px';
      menuEl.style.right = Math.max(8, window.innerWidth - r.right) + 'px';
    }
    setTimeout(function () {
      document.addEventListener('click', outsideClick, { once: true });
    }, 0);
  }

  function closeMenu() {
    if (menuEl) menuEl.hidden = true;
  }

  function outsideClick(e) {
    if (!menuEl || menuEl.hidden) return;
    if (menuEl.contains(e.target)) return;
    var btn = document.getElementById('tour-btn');
    if (btn && (e.target === btn || btn.contains(e.target))) return;
    closeMenu();
  }

  document.addEventListener('click', function (e) {
    var t = e.target.closest('[data-tour-open]');
    if (t) {
      e.preventDefault();
      openMenu(t);
      return;
    }

    var item = e.target.closest('.tour-menu-item');
    if (item) {
      var id = item.dataset.tour;
      closeMenu();

      // 1. Пробуем запустить на текущей странице
      if (startTour(id, { forceMark: true })) {
        return;
      }

      // 2. Не получилось — идём на нужную страницу с флагом
      var url = TOUR_URLS[id];
      if (url) {
        var sep = url.indexOf('?') === -1 ? '?' : '&';
        window.location.href = url + sep + 'auto_tour=' + encodeURIComponent(id);
        return;
      }

      // 3. Совсем некуда — сообщение
      var title = (TOURS[id] && TOURS[id].title) || id;
      alert('Для тура «' + title + '» нужен контекст страницы. Откройте раздел вручную и нажмите «?» там.');
      return;
    }

    if (e.target.closest('.tour-menu-close')) { closeMenu(); return; }

    if (e.target.closest('.tour-menu-reset')) {
      if (!RESET_URL) return;
      if (!confirm('Сбросить прогресс обучения? Туры снова будут предлагаться.')) return;
      fetch(RESET_URL, {
        method: 'POST',
        headers: {
          'X-CSRFToken': getCookie('csrftoken'),
          'X-Requested-With': 'XMLHttpRequest',
        },
        credentials: 'same-origin',
      }).then(function () { location.reload(); });
    }
  });

  var btn = document.getElementById('tour-btn');
  if (btn) {
    btn.addEventListener('click', function (e) {
      e.stopPropagation();
      if (menuEl && !menuEl.hidden) closeMenu();
      else openMenu(btn);
    });
  }

  function maybeAutostart() {
    if (window.innerWidth < 520) return;

    // 1. Приоритет — auto_tour из URL (переход из меню «?»)
    var params = new URLSearchParams(window.location.search);
    var autoTour = params.get('auto_tour');

    if (autoTour && TOURS[autoTour]) {
      // Почистим URL, чтобы при обновлении не запускался заново
      params.delete('auto_tour');
      var cleanQs = params.toString();
      var cleanUrl = window.location.pathname + (cleanQs ? '?' + cleanQs : '');
      window.history.replaceState({}, '', cleanUrl);

      setTimeout(function () {
        startTour(autoTour, { forceMark: true });
      }, 400);
      return;
    }

    // 2. Иначе — обычный автостарт для новичков
    if (!CURRENT || !PENDING || !AUTOSTART) return;
    setTimeout(function () {
      startTour(CURRENT, { forceMark: true });
    }, 700);
  }

  window.SVOD_tour = {
    start: function (id) { return startTour(id, { forceMark: true }); },
    openMenu: function () { openMenu(document.getElementById('tour-btn')); },
    list: function () { return Object.keys(TOURS); },
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', maybeAutostart);
  } else {
    maybeAutostart();
  }
})();
