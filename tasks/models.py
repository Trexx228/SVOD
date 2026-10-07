from django.conf import settings
from django.db import models
from django.utils import timezone


class Task(models.Model):
    class Status(models.TextChoices):
        NEW = 'new', 'Новая'
        IN_PROGRESS = 'in_progress', 'В работе'
        PAUSED = 'paused', 'Пауза'
        REVIEW = 'review', 'На проверке'
        REWORK = 'rework', 'На доработке'
        DONE = 'done', 'Готово'
        CANCELLED = 'cancelled', 'Отменена'
        PENDING_APPROVAL = 'pending_approval', 'Ожидает согласования'

    class Priority(models.TextChoices):
        LOW = 'low', 'Низкий'
        MEDIUM = 'medium', 'Средний'
        HIGH = 'high', 'Высокий'
        URGENT = 'urgent', 'Срочно'

    class Scale(models.TextChoices):
        XS = 'xs', 'Минуты'
        S = 's', 'Часы'
        M = 'm', 'Дни'
        L = 'l', 'Недели'
        XL = 'xl', 'Месяцы / проект'

    class Kind(models.TextChoices):
        WORK = 'work', 'Работа'
        DESIGN = 'design', 'Конструкция'
        TECH = 'tech', 'Технология'
        SUPPLY = 'supply', 'Закупка'

    title = models.CharField('Задача', max_length=300)

    priority = models.CharField(
        'Приоритет',
        max_length=10,
        choices=Priority.choices,
        default=Priority.MEDIUM,
    )
    status = models.CharField(
        'Статус',
        max_length=20,
        choices=Status.choices,
        default=Status.NEW,
    )
    scale = models.CharField(
        'Масштаб',
        max_length=2,
        choices=Scale.choices,
        default=Scale.S,
    )
    kind = models.CharField(
        'Событие',
        max_length=20,
        choices=Kind.choices,
    )

    plan_hours = models.FloatField('План, ч', default=0)
    start_due = models.DateField('Срок начала', null=True, blank=True)
    due = models.DateField('Срок', null=True, blank=True)
    original_start_due = models.DateField(
        'Исходный срок начала',
        null=True, blank=True,
        help_text='Срок начала на момент постановки задачи. Используется для пересчёта сдвига: срочные двигают сроки, но всегда от этой базовой даты, чтобы не накапливать ошибку.',
    )

    original_due = models.DateField(
        'Исходный дедлайн',
        null=True, blank=True,
        help_text='Дедлайн на момент постановки задачи. Не меняется при сдвигах.',
    )

    external_due = models.DateField(
        'Формальный срок (для заказчика)',
        null=True, blank=True,
        help_text=(
            'Обещание заказчику. Видно постановщику, руководителю '
            'отдела исполнителя и администратору. Исполнитель его '
            'не видит. Не сдвигается при срочных.'
        ),
    )

    planned_shift_days = models.PositiveIntegerField(
        'Запланировано срочной, раб. дн.',
        default=0,
        help_text=(
            'Сколько рабочих дней срочная задача «съела» на момент создания. '
            'Используется при коррекции сдвига после закрытия: '
            'факт − план = досдвиг или откат.'
        ),
    )

    parent = models.ForeignKey(
        'self',
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name='children',
        verbose_name='Родительская задача',
    )

    executor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='my_tasks',
        verbose_name='Исполнитель',
    )
    requester = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='issued_tasks',
        verbose_name='Постановщик',
    )

    body = models.TextField('Тело задачи (что считается результатом)', blank=True)

    accumulated_hours = models.FloatField('Накопленное время, ч', default=0.0)
    warehouse = models.BooleanField('Прибыло на склад', default=False)
    notified_overdue = models.BooleanField('О просрочке оповещено', default=False)

    created_at = models.DateTimeField('Создана', auto_now_add=True)
    finished_at = models.DateTimeField('Финиш', null=True, blank=True)
    session_started_at = models.DateTimeField('Текущий сеанс начат', null=True, blank=True)

    order = models.ForeignKey(
        'Order',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='tasks',
        verbose_name='Заказ',
    )

    branch = models.ForeignKey(
        'TaskBranch',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='tasks',
        verbose_name='Ветка заказа',
    )
    stage_order = models.PositiveIntegerField(
        'Этап в ветке',
        null=True,
        blank=True,
        help_text='Порядковый номер этапа. Пусто = вне цепочки.',
    )
    blocked_by_stage = models.BooleanField(
        'Заблокирована предыдущим этапом',
        default=False,
    )
    pending_shift_days = models.PositiveIntegerField(
        'Запрошенный сдвиг, дней',
        null=True,
        blank=True,
        help_text='Заполняется, когда предыдущий этап просрочен. '
                  'Руководитель решает: сдвинуть или активировать как есть.',
    )
    pending_shift_from_task = models.ForeignKey(
        'self',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='+',
        verbose_name='Просроченный этап-источник',
    )
    task_type = models.ForeignKey(
        'core.TaskType',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='tasks',
        verbose_name='Типовая задача',
    )

    class Meta:
        verbose_name = 'Задача'
        verbose_name_plural = 'Задачи'
        indexes = [
            models.Index(fields=['status', 'due'], name='task_status_due_idx'),
            models.Index(fields=['executor', 'status'], name='task_executor_status_idx'),
            models.Index(fields=['requester', 'status'], name='task_requester_status_idx'),
            models.Index(fields=['finished_at'], name='task_finished_at_idx'),
        ]

    @property
    def total_hours(self):
        return self.accumulated_hours or 0.0

    @property
    def spent_hours(self):
        total = self.total_hours

        if self.session_started_at:
            total += max(
                0,
                (timezone.now() - self.session_started_at).total_seconds() / 3600,
                )

        return total

    @property
    def children_count(self):
        return self.children.count()

    @property
    def rolled_plan(self):
        """Сумма плана по дереву. Кэш на объекте, чтобы не рекурсировать много раз."""
        cache = getattr(self, '_rolled_plan_cache', None)
        if cache is not None:
            return cache

        children = list(self.children.all())
        if children:
            total = self.plan_hours or 0
            for c in children:
                total += c.rolled_plan
        else:
            total = self.plan_hours

        self._rolled_plan_cache = total
        return total


class TimeSession(models.Model):
    task = models.ForeignKey(
        Task,
        on_delete=models.CASCADE,
        related_name='sessions',
    )
    executor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='sessions',
    )

    started_at = models.DateTimeField('Старт')
    finished_at = models.DateTimeField('Финиш')
    duration_hours = models.FloatField('Длительность, ч')

    status_at_close = models.CharField(
        'Статус на закрытии',
        max_length=20,
        choices=Task.Status.choices,
    )

    comment = models.TextField(blank=True)

    class Meta:
        verbose_name = 'Сеанс работы'
        verbose_name_plural = 'Сеансы работы'
        ordering = ['-finished_at']

    def __str__(self):
        return f'{self.task} · {self.duration_hours:.2f} ч'


class TaskProgress(models.Model):
    task = models.ForeignKey(
        Task,
        on_delete=models.CASCADE,
        related_name='progress',
        verbose_name='Задача',
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='progress_notes',
        verbose_name='Автор',
    )

    text = models.TextField('Текст', max_length=2000)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Запись о ходе задачи'
        verbose_name_plural = 'Записи о ходе задач'
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.task}: {self.text[:40]}'


class TaskLog(models.Model):
    class Kind(models.TextChoices):
        REWORK = 'rework', 'Возврат на доработку'
        ACCEPT = 'accept', 'Принято'
        CLOSE = 'close', 'Закрыто'
        PLAN = 'plan', 'Изменён план'
        DUE = 'due', 'Изменён срок'
        PRIO = 'prio', 'Изменён приоритет'
        CANCEL = 'cancel', 'Отменена'
        SHIFT_REQUESTED = 'shift_requested', 'Запрошен сдвиг сроков'
        SHIFT_APPROVED  = 'shift_approved',  'Сдвиг одобрен'
        SHIFT_SQUEEZED  = 'shift_squeezed',  'Ужали сроки'
        SHIFT_REJECTED  = 'shift_rejected',  'Сдвиг отклонён'

    source_task = models.ForeignKey(
        'Task',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='shift_source_logs',
        verbose_name='Источник сдвига',
    )
    shift_days = models.IntegerField(
        'Дней сдвига',
        null=True,
        blank=True,
        help_text='Для логов сдвига сроков: +N или −N',
    )

    task = models.ForeignKey(
        Task,
        on_delete=models.CASCADE,
        related_name='logs',
        verbose_name='Задача',
    )
    kind = models.CharField(
        'Событие',
        max_length=20,
        choices=Kind.choices,
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='task_logs',
        verbose_name='Автор',
    )

    comment = models.CharField('Комментарий', max_length=500, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Журнал задачи'
        verbose_name_plural = 'Журналы задач'
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.task} · {self.get_kind_display()}'


class Order(models.Model):
    number = models.CharField('Номер заказа', max_length=60)
    product = models.CharField('Изделие', max_length=200)

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='owned_orders',
        verbose_name='Ответственный за заказ',
        help_text='Получает уведомления о приближении контрольных точек.',
    )

    # ── Контрольные точки ──
    contract_start = models.DateField(
        'Дата начала контракта',
        null=True, blank=True,
    )
    design_start = models.DateField(
        'Срок начала проектирования',
        null=True, blank=True,
    )
    design_end = models.DateField(
        'Срок окончания проектирования',
        null=True, blank=True,
    )
    ship_due = models.DateField(
        'Срок отгрузки',
        null=True, blank=True,
    )

    comment = models.TextField('Комментарий', blank=True)

    class Meta:
        verbose_name = 'Заказ'
        verbose_name_plural = 'Заказы'
        ordering = ['ship_due']

    def __str__(self):
        return f'{self.number} · {self.product}'

    # ── Рабочие дни ──

    @property
    def work_days_to_ship(self):
        """Сколько рабочих дней от начала контракта до отгрузки."""
        from .utils import work_days_between
        if not self.contract_start or not self.ship_due:
            return None
        return work_days_between(self.contract_start, self.ship_due)

    @property
    def work_days_design(self):
        """Сколько рабочих дней выделено на проектирование."""
        from .utils import work_days_between
        if not self.design_start or not self.design_end:
            return None
        return work_days_between(self.design_start, self.design_end)

    @property
    def work_days_remaining(self):
        """Сколько рабочих дней осталось до отгрузки (от сегодня)."""
        from .utils import work_days_between
        from django.utils import timezone
        if not self.ship_due:
            return None
        today = timezone.localdate()
        if self.ship_due < today:
            return -work_days_between(self.ship_due, today)
        return work_days_between(today, self.ship_due)

    @property
    def stage_label(self):
        """Текущий этап заказа по датам."""
        from django.utils import timezone
        today = timezone.localdate()

        if self.ship_due and today > self.ship_due:
            return {'code': 'shipped_late', 'label': 'Просрочена отгрузка', 'cls': 'danger'}
        if self.design_end and today > self.design_end:
            return {'code': 'in_prod', 'label': 'В производстве', 'cls': 'info'}
        if self.design_start and today >= self.design_start:
            return {'code': 'in_design', 'label': 'Проектирование', 'cls': 'info'}
        if self.contract_start and today >= self.contract_start:
            return {'code': 'before_design', 'label': 'Ожидает проектирования', 'cls': 'warn'}
        return {'code': 'unknown', 'label': '—', 'cls': ''}


class TaskBranch(models.Model):
    """Ветка этапов внутри заказа. Например, «Проектирование», «Производство».

    Внутри ветки задачи идут последовательно по stage_order:
    закрыл этап 1 → активируется этап 2 → и т.д.
    """
    order = models.ForeignKey(
        Order,
        on_delete=models.CASCADE,
        related_name='branches',
        verbose_name='Заказ',
    )
    name = models.CharField('Название ветки', max_length=150)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Ветка заказа'
        verbose_name_plural = 'Ветки заказа'
        ordering = ['order', 'name']
        constraints = [
            models.UniqueConstraint(
                fields=['order', 'name'],
                name='unique_branch_per_order',
            ),
        ]

    def __str__(self):
        return f'{self.order.number} · {self.name}'


class OrderTemplate(models.Model):
    """Шаблон маршрута заказа. Например: «Стандартный ГТ», «Опытный образец»."""

    name = models.CharField('Название', max_length=200, unique=True)
    description = models.TextField('Описание', blank=True)
    is_default = models.BooleanField(
        'По умолчанию', default=False,
        help_text='Подставляется первым при генерации плана нового заказа.',
    )
    is_active = models.BooleanField('Активен', default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Шаблон заказа'
        verbose_name_plural = 'Шаблоны заказов'
        ordering = ['name']

    def __str__(self):
        return self.name


class OrderTemplateStage(models.Model):
    """Один этап внутри шаблона: что делаем, когда, в какой ветке."""

    class Anchor(models.TextChoices):
        CONTRACT_START = 'contract_start', 'Начало контракта'
        DESIGN_START   = 'design_start',   'Старт проектирования'
        DESIGN_END     = 'design_end',     'Конец проектирования'
        SHIP_DUE       = 'ship_due',       'Отгрузка'

    template = models.ForeignKey(
        OrderTemplate,
        on_delete=models.CASCADE,
        related_name='stages',
        verbose_name='Шаблон',
    )
    order = models.PositiveIntegerField(
        'Порядок', default=1,
        help_text='Внутри одной ветки этапы идут по возрастанию.',
    )

    title = models.CharField('Название этапа', max_length=200)
    branch_name = models.CharField(
        'Ветка', max_length=150, blank=True,
        help_text='Пусто — задача вне ветки.',
    )
    task_type = models.ForeignKey(
        'core.TaskType',
        null=True, blank=True,
        on_delete=models.SET_NULL,
        verbose_name='Типовая задача',
        help_text='Из неё берётся план часов и вид работы, если не задано явно.',
    )

    executor_role_code = models.CharField(
        'Роль исполнителя (код)', max_length=50, blank=True,
        help_text='Если задано — ищем активного сотрудника этой роли в отделе заказа.',
    )

    offset_anchor = models.CharField(
        'Точка отсчёта', max_length=20,
        choices=Anchor.choices, default=Anchor.CONTRACT_START,
    )
    offset_days = models.IntegerField(
        'Смещение, раб. дней', default=0,
        help_text='Плюс — позже точки, минус — раньше. Считается по рабочим дням.',
    )
    duration_days = models.PositiveIntegerField(
        'Длительность, раб. дней', default=5,
    )

    plan_hours = models.FloatField(
        'План, ч', default=0.0,
        help_text='0 — если задана типовая, берём часы из неё.',
    )
    use_task_type_hours = models.BooleanField(
        'Брать часы из типовой', default=True,
    )

    kind = models.CharField(
        'Вид работы', max_length=20, blank=True,
        choices=Task.Kind.choices,
        help_text='Пусто — унаследуется от типовой или work.',
    )
    priority = models.CharField(
        'Приоритет', max_length=10,
        choices=Task.Priority.choices, default=Task.Priority.MEDIUM,
    )

    class Meta:
        verbose_name = 'Этап шаблона'
        verbose_name_plural = 'Этапы шаблона'
        ordering = ['template', 'order']
        constraints = [
            models.UniqueConstraint(
                fields=['template', 'order'],
                name='unique_template_stage_order',
            ),
        ]

    def __str__(self):
        return f'{self.template.name} · {self.order}. {self.title}'


class WeekCommit(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='week_commits',
        verbose_name='Сотрудник',
    )
    task = models.ForeignKey(
        Task,
        on_delete=models.CASCADE,
        related_name='week_commits',
        verbose_name='Задача',
    )
    week_start = models.DateField('Неделя с')

    class Meta:
        verbose_name = 'Обязательство на неделю'
        verbose_name_plural = 'Обязательства на неделю'
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'task', 'week_start'],
                name='unique_week_commit',
            ),
        ]

    def __str__(self):
        return f'{self.user} · {self.task} · {self.week_start}'


class ShopSession(models.Model):
    task = models.ForeignKey(
        Task,
        on_delete=models.CASCADE,
        related_name='shop_sessions',
        verbose_name='Задача',
    )
    executor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='shop_sessions',
        verbose_name='Сотрудник',
    )

    started_at = models.DateTimeField('Выход в цех', auto_now_add=True)
    finished_at = models.DateTimeField('Возврат из цеха', null=True, blank=True)
    duration_hours = models.FloatField('Длительность, ч', default=0.0)

    class Meta:
        verbose_name = 'Сеанс в цехе'
        verbose_name_plural = 'Сеансы в цехе'
        ordering = ['-started_at']
        constraints = [
            models.UniqueConstraint(
                fields=['executor'],
                condition=models.Q(finished_at__isnull=True),
                name='unique_open_shop_session_per_user',
            ),
        ]

    def __str__(self):
        return f'{self.executor} · цех · {self.task}'


class TimelineSnapshot(models.Model):
    """Ежедневный снимок задачи — для диаграммы Ганта и динамики."""

    snapshot_date = models.DateField('Дата снимка', db_index=True)

    task = models.ForeignKey(
        Task,
        on_delete=models.CASCADE,
        related_name='snapshots',
    )
    executor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='timeline_snapshots',
    )

    start_date = models.DateField('Начало')
    due_date = models.DateField('Дедлайн', null=True, blank=True)

    status = models.CharField(
        'Статус',
        max_length=20,
        choices=Task.Status.choices,
    )
    priority = models.CharField(
        'Приоритет',
        max_length=10,
        choices=Task.Priority.choices,
    )

    plan_hours = models.FloatField('План, ч', default=0)

    # ── Метрики дня ──
    accumulated_hours = models.FloatField(
        'Накоплено на конец дня, ч', default=0,
    )
    hours_today = models.FloatField(
        'Списано за день, ч', default=0,
    )
    status_changed = models.BooleanField(
        'Статус изменился с прошлого снимка', default=False,
    )
    due_before = models.DateField(
        'Дедлайн до сдвига', null=True, blank=True,
    )

    class Meta:
        verbose_name = 'Снимок хронологии'
        verbose_name_plural = 'Снимки хронологии'
        ordering = ['-snapshot_date', 'start_date']
        constraints = [
            models.UniqueConstraint(
                fields=['snapshot_date', 'task'],
                name='unique_timeline_snapshot',
            ),
        ]

    def __str__(self):
        return f'{self.snapshot_date} · {self.task.title}'


class PlanShiftRequest(models.Model):
    """Заявка на сдвиг плана из-за срочной/неадекватной задачи.

    Висит у заместителей отдела исполнителя, пока кто-то не отреагирует.
    При молчании — эскалация на руководителя отдела, потом на начальника завода.
    Пока не решена — source_task имеет статус 'pending_approval'.
    """
    class Level(models.TextChoices):
        DEPUTY  = 'deputy',  'Заместители отдела'
        MANAGER = 'manager', 'Руководитель отдела'
        PLANT   = 'plant',   'Начальник завода'

    class Status(models.TextChoices):
        PENDING   = 'pending',   'Ожидает решения'
        APPROVED  = 'approved',  'Одобрен сдвиг'
        SQUEEZED  = 'squeezed',  'Ужали сроки'
        REJECTED  = 'rejected',  'Отклонено'

    # ── Кто и что ──
    source_task = models.ForeignKey(
        Task,
        on_delete=models.CASCADE,
        related_name='shift_requests',
        verbose_name='Задача-виновник',
    )
    initiated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='initiated_shift_requests',
        verbose_name='Инициатор (постановщик)',
    )
    reason = models.TextField(
        'Обоснование срочности',
        help_text='Почему задача требует сдвига других планов. Обязательно.',
    )
    shift_days = models.PositiveIntegerField(
        'Запрошенный сдвиг, дней',
        default=0,
    )

    # ── Чей план ломается ──
    department = models.ForeignKey(
        'accounts.Department',
        on_delete=models.CASCADE,
        related_name='shift_requests',
        verbose_name='Отдел исполнителя',
    )
    affected_tasks = models.ManyToManyField(
        Task,
        related_name='planned_shifts',
        blank=True,
        verbose_name='Задачи, которые сдвинутся',
    )

    # ── Состояние ──
    status = models.CharField(
        'Статус',
        max_length=15,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    current_level = models.CharField(
        'Текущий уровень',
        max_length=15,
        choices=Level.choices,
        default=Level.DEPUTY,
    )
    recipients = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        related_name='shift_requests_to_review',
        blank=True,
        verbose_name='Текущие согласующие',
    )

    # ── Тайминги ──
    created_at = models.DateTimeField('Создано', auto_now_add=True)
    escalate_at = models.DateTimeField(
        'Эскалировать в',
        help_text='Если никто не отреагирует до этого времени — следующий уровень.',
        db_index=True,
    )
    resolved_at = models.DateTimeField('Решено', null=True, blank=True)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='resolved_shift_requests',
        verbose_name='Кто решил',
    )
    decision_note = models.TextField('Комментарий к решению', blank=True)

    class Meta:
        verbose_name = 'Заявка на сдвиг плана'
        verbose_name_plural = 'Заявки на сдвиг плана'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['status', 'escalate_at']),
            models.Index(fields=['department', 'status']),
        ]

    def __str__(self):
        return f'#{self.pk} сдвиг на {self.shift_days} дн — {self.source_task.title[:50]}'


class PlanShiftStep(models.Model):
    """История шагов согласования. Позволяет видеть, кто проигнорировал заявку."""

    class Decision(models.TextChoices):
        PENDING  = 'pending',   'Ожидает'
        APPROVED = 'approved',  'Одобрил'
        REJECTED = 'rejected',  'Отклонил'
        SQUEEZED = 'squeezed',  'Ужал сроки'
        TIMEOUT  = 'timeout',   'Проигнорировал'

    request = models.ForeignKey(
        PlanShiftRequest,
        on_delete=models.CASCADE,
        related_name='steps',
        verbose_name='Заявка',
    )
    level = models.CharField(
        'Уровень',
        max_length=15,
        choices=PlanShiftRequest.Level.choices,
    )
    reviewer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name='shift_steps',
        verbose_name='Согласующий',
    )
    sent_at = models.DateTimeField('Отправлено', auto_now_add=True)
    decision = models.CharField(
        'Решение',
        max_length=15,
        choices=Decision.choices,
        default=Decision.PENDING,
    )
    decided_at = models.DateTimeField('Решено в', null=True, blank=True)
    note = models.TextField('Комментарий', blank=True)

    class Meta:
        verbose_name = 'Шаг согласования'
        verbose_name_plural = 'Шаги согласования'
        ordering = ['request', 'sent_at']
        indexes = [
            models.Index(fields=['request', 'level']),
        ]

    def __str__(self):
        return f'{self.request_id} · {self.get_level_display()} · {self.get_decision_display()}'


class DailyDeptMetrics(models.Model):
    """Ежедневный срез по подразделению.

    Используется для Ганта и динамики: одна строка в день на отдел.
    Наполняется командой capture_daily_metrics (через scheduler, 23:50).
    """

    date = models.DateField('Дата', db_index=True)
    department = models.ForeignKey(
        'accounts.Department',
        on_delete=models.CASCADE,
        related_name='daily_metrics',
        verbose_name='Подразделение',
    )

    # Задачи на конец дня
    tasks_open = models.PositiveIntegerField('Открытых', default=0)
    tasks_done_today = models.PositiveIntegerField('Закрыто за день', default=0)
    tasks_overdue = models.PositiveIntegerField('Просрочено', default=0)

    # Часы
    plan_hours_open = models.FloatField('План открытых, ч', default=0)
    fact_hours_today = models.FloatField('Факт за день, ч', default=0)
    shop_hours_today = models.FloatField('Цех за день, ч', default=0)

    # Активность
    sessions_count = models.PositiveIntegerField('Сеансов', default=0)
    employees_active = models.PositiveIntegerField('Сотрудников работало', default=0)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Дневной срез отдела'
        verbose_name_plural = 'Дневные срезы отделов'
        ordering = ['-date', 'department__name']
        constraints = [
            models.UniqueConstraint(
                fields=['date', 'department'],
                name='unique_daily_dept_metrics',
            ),
        ]
        indexes = [
            models.Index(fields=['department', '-date']),
        ]

    def __str__(self):
        return f'{self.date} · {self.department.name}'


class OrderDeadlineNotification(models.Model):
    """Факт отправки уведомления о приближении контрольной точки заказа.

    Нужна, чтобы scheduler не спамил повторно. Одна запись = одно
    отправленное уведомление для (заказ, точка, за сколько дней).
    """

    class Anchor(models.TextChoices):
        CONTRACT_START = 'contract_start', 'Начало контракта'
        DESIGN_START   = 'design_start',   'Старт проектирования'
        DESIGN_END     = 'design_end',     'Конец проектирования'
        SHIP_DUE       = 'ship_due',       'Отгрузка'

    order = models.ForeignKey(
        Order,
        on_delete=models.CASCADE,
        related_name='deadline_notifications',
        verbose_name='Заказ',
    )
    anchor = models.CharField(
        'Контрольная точка', max_length=20, choices=Anchor.choices,
    )
    days_before = models.PositiveIntegerField(
        'За сколько раб. дней',
        help_text='7 или 3. Может быть больше, если добавишь свои пороги.',
    )
    anchor_date = models.DateField(
        'Дата точки на момент отправки',
        help_text='Фиксируем — чтобы если срок переехал, уведомление отправилось заново.',
    )
    sent_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'Уведомление о точке заказа'
        verbose_name_plural = 'Уведомления о точках заказов'
        ordering = ['-sent_at']
        constraints = [
            models.UniqueConstraint(
                fields=['order', 'anchor', 'days_before', 'anchor_date'],
                name='unique_order_deadline_notification',
            ),
        ]
        indexes = [
            models.Index(fields=['order', '-sent_at']),
        ]

    def __str__(self):
        return f'{self.order.number} · {self.get_anchor_display()} · −{self.days_before} д.'


class OvertimeRecord(models.Model):
    """Запись о сверхурочной работе одного сотрудника за один день.

    Фиксирует руководитель отдела исполнителя (или админ портала).
    Сотрудник сам себе сверхурочную не создаёт.

    Упрощённая модель: без статусов, без таймеров, без режимов.
    Одна запись = один день = сколько часов сверхурочно.

    department — снапшот на момент записи: если сотрудника переведут
    в другой отдел, старая история останется за прежним отделом.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='overtime_records',
        verbose_name='Сотрудник',
    )
    department = models.ForeignKey(
        'accounts.Department',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='overtime_records',
        verbose_name='Отдел (снапшот)',
        help_text='Фиксируется на момент записи, чтобы перевод не менял историю.',
    )
    task = models.ForeignKey(
        Task,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='overtime_records',
        verbose_name='Задача',
        help_text='Опционально. Оставьте пустым для работ «на общие нужды».',
    )

    date = models.DateField('Дата', db_index=True)
    hours = models.FloatField(
        'Часы',
        help_text='Сколько часов сверхурочно в этот день.',
    )
    reason = models.CharField(
        'Причина', max_length=500, blank=True,
        help_text='Например: ремонт станка, аварийный пуск, инвентаризация.',
    )
    order_file = models.FileField(
        'Приказ / основание',
        upload_to='overtime/%Y/%m/',
        null=True, blank=True,
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='overtime_created',
        verbose_name='Кто внёс',
    )
    created_at = models.DateTimeField('Когда внесено', auto_now_add=True)

    class Meta:
        verbose_name = 'Сверхурочная'
        verbose_name_plural = 'Сверхурочные'
        ordering = ['-date', '-created_at']
        indexes = [
            models.Index(fields=['user', '-date']),
            models.Index(fields=['department', '-date']),
        ]

    def __str__(self):
        return f'{self.user} · {self.date} · {self.hours} ч'
