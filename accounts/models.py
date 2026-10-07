from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.core.validators import MinValueValidator
from django.db import models


class WorkSchedule(models.Model):
    """График работы: паттерн рабочих/выходных дней + часы в смене.

    Используется для сменных режимов (5/2, 2/2, 7/7 и т.п.).
    Приоритет при определении графика сотрудника:
      1. User.schedule (личный график)
      2. Department.default_schedule (график отдела)
      3. None — старая логика: пн–пт, праздники РФ, глобальная норма.

    Праздники при заданном графике игнорируются: сменный режим
    существует как раз потому, что «пн–пт и 1 января — выходной»
    к нему не применяется.
    """

    name = models.CharField('Название', max_length=100)
    pattern = models.JSONField(
        'Паттерн',
        help_text=(
            'Список 0/1. 1 — рабочий день, 0 — выходной. '
            'Например, [1,1,1,1,1,0,0] — пятидневка, '
            '[1,1,0,0] — 2/2, [1,1,1,1,1,1,1,0,0,0,0,0,0,0] — 7/7.'
        ),
    )
    hours_per_shift = models.FloatField(
        'Часов в смене',
        default=8.0,
        validators=[
            MinValueValidator(0.1, 'Часов в смене должно быть больше нуля.'),
        ],
    )
    anchor_date = models.DateField(
        'Начало цикла',
        help_text=(
            'День, в который цикл начинался «с первого элемента паттерна». '
            'От него отсчитывается позиция в цикле. '
            'Для стандартной пятидневки — любой понедельник.'
        ),
    )
    is_active = models.BooleanField('Активен', default=True)
    use_calendar = models.BooleanField(
        'Учитывать производственный календарь',
        default=True,
        help_text=(
            'Включено — праздники РФ (core.Holiday и гос. календарь) '
            'переопределяют паттерн: праздник → выходной, перенос → рабочий. '
            'Подходит для 5/2 и офисных сотрудников. '
            'Выключите для сменных графиков (2/2, 7/7), где работа в праздник — '
            'норма и оплачивается х2.'
        ),
    )
    description = models.CharField('Описание', max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'График работы'
        verbose_name_plural = 'Графики работы'
        ordering = ['name']

    def __str__(self):
        return f'{self.name} ({self.hours_per_shift:g} ч)'

    @property
    def cycle_length(self):
        return len(self.pattern or [])

    def is_working_on(self, d):
        """Рабочий ли день d по этому графику.

        Считается от anchor_date: (d − anchor_date).days % cycle_length.
        """
        if not self.pattern:
            return False
        n = self.cycle_length
        if n == 0:
            return False
        offset = (d - self.anchor_date).days % n
        return bool(self.pattern[offset])


class Department(models.Model):
    name = models.CharField('Название', max_length=150, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    hours_per_day = models.FloatField(
        'Часов в день',
        null=True, blank=True,
        validators=[
            MinValueValidator(0.1, 'Норма часов должна быть больше нуля.'),
        ],
        help_text=(
            'Если задано — переопределяет глобальную норму для всех '
            'сотрудников этого подразделения. Например, 12 для цеха '
            'со сменным графиком. Оставьте пустым, чтобы использовать '
            'глобальную норму.'
        ),
    )
    default_schedule = models.ForeignKey(
        WorkSchedule,
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='departments',
        verbose_name='График по умолчанию',
        help_text=(
            'Применяется ко всем сотрудникам отдела без личного графика. '
            'Если не задан — используется встроенная логика: пн–пт, 8 ч, '
            'праздники РФ.'
        ),
    )

    class Meta:
        verbose_name = 'Подразделение'
        verbose_name_plural = 'Подразделения'
        ordering = ['name']

    def __str__(self):
        return self.name


class Role(models.Model):
    name = models.CharField('Название роли', max_length=100, unique=True)
    code = models.SlugField('Код', unique=True)

    # ── Уровень доступа ──
    can_manage = models.BooleanField('Управление задачами и приёмкой', default=False)
    can_admin = models.BooleanField('Права администратора', default=False)
    can_plant = models.BooleanField('Свод по заводу', default=False)

    # ── Права на разделы кабинета ──
    can_view_kpi = models.BooleanField(
        'Видеть KPI', default=False,
        help_text='Доступ к отчёту KPI в кабинете (с учётом области видимости).',
    )
    can_view_sessions = models.BooleanField(
        'Видеть сессии сотрудников', default=False,
        help_text='Доступ к журналу сессий работы и цеха.',
    )
    can_view_online = models.BooleanField(
        'Видеть кто онлайн', default=False,
        help_text='Доступ к странице «Кто онлайн» и активным сессиям.',
    )
    can_view_problems = models.BooleanField(
        'Видеть проблемные задачи', default=False,
        help_text='Доступ к диагностике аномалий в задачах.',
    )
    can_view_task_logs = models.BooleanField(
        'Видеть логи задач', default=False,
        help_text='Доступ к журналу событий по задачам.',
    )
    can_view_orders = models.BooleanField(
        'Видеть заказы', default=False,
        help_text='Просмотр списка заказов и веток. Редактирование — только у админа.',
    )
    can_view_analytics = models.BooleanField(
        'Видеть аналитику по отделам', default=False,
        help_text='Сводные отчёты по всем подразделениям.',
    )
    can_export = models.BooleanField(
        'Экспорт отчётов', default=False,
        help_text='Выгрузка отчётов в CSV/XLSX.',
    )
    is_developer = models.BooleanField(
        'Разработчик', default=False,
        help_text=(
            'Доступ к техническому разделу: логи, состояние планировщика, '
            'список моделей, дампы. Не даёт прав руководителя или админа — '
            'только инструменты диагностики.'
        ),
    )

    description = models.CharField('Описание', max_length=200, blank=True)

    class Meta:
        verbose_name = 'Роль'
        verbose_name_plural = 'Роли'

    def __str__(self):
        return self.name


class UserManager(BaseUserManager):
    use_in_migrations = True

    def _create_user(self, email, password, **extra):
        if not email:
            raise ValueError('Email обязателен')

        user = self.model(email=email.strip().lower(), **extra)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email, password=None, **extra):
        extra.setdefault('is_active', False)
        extra.setdefault('is_staff', False)
        extra.setdefault('is_superuser', False)
        return self._create_user(email, password, **extra)

    def create_superuser(self, email, password=None, **extra):
        extra.setdefault('is_active', True)
        extra.setdefault('is_staff', True)
        extra.setdefault('is_superuser', True)

        if 'role' not in extra:
            try:
                extra['role'] = Role.objects.filter(code='admin').first()
            except Exception:
                extra['role'] = None

        return self._create_user(email, password, **extra)


class User(AbstractUser):
    username = None

    email = models.EmailField('Корпоративная почта', unique=True)
    full_name = models.CharField('ФИО', max_length=150)
    department_hint = models.CharField(
        'Подразделение при регистрации',
        max_length=150,
        blank=True,
    )

    department = models.ForeignKey(
        Department,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='users',
        verbose_name='Подразделение',
    )
    role = models.ForeignKey(
        Role,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='users',
        verbose_name='Роль',
    )
    last_department = models.ForeignKey(
        Department,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='+',
        verbose_name='Последнее подразделение',
    )
    last_executor = models.ForeignKey(
        'self',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='+',
        verbose_name='Последний исполнитель',
    )

    # ── Интерфейс ──
    theme = models.CharField(
        'Тема',
        max_length=10,
        default='light',
        choices=[
            ('light', 'Светлая'),
            ('dark', 'Тёмная'),
            ('graphite', 'Графит'),
            ('ocean', 'Океан'),
            ('forest', 'Тайга'),
            ('sand', 'Песок'),
            ('contrast', 'Контрастная'),
        ],
    )
    font_size = models.CharField(
        'Размер шрифта',
        max_length=1,
        default='m',
        choices=[
            ('s', 'Компактный'),
            ('m', 'Обычный'),
            ('l', 'Крупный'),
        ],
    )
    sound_on = models.BooleanField('Звук уведомлений', default=True)

    bg_style = models.CharField(
        'Фон',
        max_length=10,
        default='grad',
        choices=[
            ('grad', 'Градиент темы'),
            ('solid', 'Однотонный'),
            ('mesh', 'Мягкие пятна'),
            ('grid', 'Сетка'),
            ('custom', 'Свой цвет'),
            ('image', 'Своя картинка'),
        ],
    )
    bg_color = models.CharField('Свой цвет фона', max_length=7, blank=True, default='')
    bg_image = models.FileField('Картинка фона', upload_to='bg/', null=True, blank=True)

    # ── Уведомления ──
    email_notifications = models.BooleanField(
        'Email-уведомления', default=True,
        help_text='Присылать письма о новых задачах, возвратах на доработку и приёмке.',
    )
    email_digest_daily = models.BooleanField(
        'Утренний дайджест', default=False,
        help_text='Присылать одно письмо утром со списком задач на день.',
    )

    # ── Обучалка ──
    tour_seen = models.JSONField(
        'Пройденные туры',
        default=dict,
        blank=True,
        help_text='Словарь {tour_id: ISO-время прохождения}.',
    )
    onboarding_done = models.BooleanField(
        'Онбординг завершён',
        default=False,
        help_text='True после финального экрана «Ты готов».',
    )

    # ── Активность ──
    last_activity_at = models.DateTimeField('Последняя активность', null=True, blank=True)
    idle_since = models.DateTimeField('Простой с', null=True, blank=True)
    shift_credit_days = models.IntegerField(
        'Срочная нагрузка, раб. дн.',
        default=0,
        help_text=(
            'Сколько рабочих дней исполнитель занят срочными задачами '
            'сверх плановых. Используется при пересчёте сдвига остальных '
            'его задач.'
        ),
    )
    schedule = models.ForeignKey(
        WorkSchedule,
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='users',
        verbose_name='Личный график работы',
        help_text=(
            'Если задан — перебивает график отдела. Оставьте пустым, '
            'чтобы использовать график подразделения или встроенный 5/2×8.'
        ),
    )

    # ── Статус занятости ──
    class EmploymentStatus(models.TextChoices):
        ACTIVE    = 'active',    'Работает'
        VACATION  = 'vacation',  'Отпуск / больничный'
        MATERNITY = 'maternity', 'Декрет'
        DISMISSED = 'dismissed', 'Уволен'
        ARCHIVED  = 'archived',  'Архив'

    employment_status = models.CharField(
        'Статус занятости',
        max_length=15,
        choices=EmploymentStatus.choices,
        default=EmploymentStatus.ACTIVE,
        db_index=True,
    )
    dismissed_at = models.DateField(
        'Дата увольнения',
        null=True,
        blank=True,
    )
    dismissed_reason = models.CharField(
        'Причина увольнения',
        max_length=200,
        blank=True,
    )
    vacation_from = models.DateField(
        'Отпуск с',
        null=True,
        blank=True,
        help_text=(
            'Первый день отпуска / больничного. Используется для отображения '
            'статуса и для проверок при постановке задач.'
        ),
    )
    vacation_to = models.DateField(
        'Отпуск по',
        null=True,
        blank=True,
        help_text=(
            'Последний день отпуска. Если пусто — считается бессрочным '
            'до ручного снятия.'
        ),
    )

    # ── Вход ──
    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = ['full_name']
    objects = UserManager()

    # ── Роль по умолчанию ──

    @staticmethod
    def get_default_role():
        """Роль «Пользователь». Создаётся через init_roles."""
        try:
            return Role.objects.filter(code='user').first()
        except Exception:
            return None

    # ── Уровни доступа ──

    @property
    def is_boss(self):
        """Руководитель или админ: видит постановку, приёмку, отчёты."""
        return self.is_superuser or bool(
            self.role and (self.role.can_manage or self.role.can_admin)
        )

    @property
    def is_admin_role(self):
        return self.is_superuser or bool(self.role and self.role.can_admin)

    @property
    def can_plant(self):
        """Свод по заводу: суперадмин, админ или роль с галочкой «Свод по заводу»."""
        return self.is_superuser or bool(
            self.role and (self.role.can_admin or self.role.can_plant)
        )

    @property
    def is_developer(self):
        """Доступ к техническому разделу «Разработка».

        Включается автоматически для суперюзера и админа портала,
        либо явным флагом Role.is_developer. Отдельная роль для тех,
        кто занимается эксплуатацией, но не должен быть админом.
        """
        return self.is_superuser or self.is_admin_role or bool(
            self.role and self.role.is_developer
        )

    # ── Права на разделы кабинета ──

    def _has_flag(self, flag_name):
        if self.is_superuser or self.is_admin_role:
            return True
        return bool(self.role and getattr(self.role, flag_name, False))

    @property
    def can_view_kpi(self):
        return self._has_flag('can_view_kpi')

    @property
    def can_view_sessions(self):
        return self._has_flag('can_view_sessions')

    @property
    def can_view_online(self):
        return self._has_flag('can_view_online')

    @property
    def can_view_problems(self):
        return self._has_flag('can_view_problems')

    @property
    def can_view_task_logs(self):
        return self._has_flag('can_view_task_logs')

    @property
    def can_view_orders(self):
        return self._has_flag('can_view_orders')

    @property
    def can_view_analytics(self):
        return self._has_flag('can_view_analytics')

    @property
    def can_export(self):
        return self._has_flag('can_export')

    @property
    def access_level(self):
        """Строковый уровень для шаблонов: 'admin', 'director', 'boss', 'staff'."""
        if self.is_admin_role:
            return 'admin'
        if self.can_plant:
            return 'director'
        if self.is_boss:
            return 'boss'
        return 'staff'

    @property
    def scope(self):
        """Кого видит пользователь в отчётах и кабинете.

        'admin'      — всё, включая настройки
        'plant'      — весь завод (директор)
        'department' — свой отдел
        'self'       — только свои задачи
        """
        if self.is_admin_role:
            return 'admin'
        if self.can_plant:
            return 'plant'
        if self.is_boss and self.department_id:
            return 'department'
        return 'self'

    # ── Статус занятости (логика) ──

    @property
    def is_working(self):
        """Активен и не в архиве."""
        return (
                self.is_active
                and self.employment_status in (
                    self.EmploymentStatus.ACTIVE,
                    self.EmploymentStatus.VACATION,
                    self.EmploymentStatus.MATERNITY,
                )
        )

    @property
    def is_dismissed(self):
        return self.employment_status == self.EmploymentStatus.DISMISSED

    @property
    def is_archived(self):
        return self.employment_status == self.EmploymentStatus.ARCHIVED

    @property
    def is_on_vacation(self):
        """Сотрудник сегодня в отпуске / больничном / декрете.

        True если:
          - employment_status == VACATION (ручная установка без дат), либо
          - сегодня попадает в период vacation_from..vacation_to.

        Maternity отдельно не учитывается здесь: если он в декрете,
        руководитель сам видит статус, а is_on_vacation про даты.
        """
        from django.utils import timezone

        if self.employment_status == self.EmploymentStatus.VACATION:
            return True

        if not (self.vacation_from and self.vacation_to):
            return False

        today = timezone.localdate()
        return self.vacation_from <= today <= self.vacation_to

    @property
    def vacation_label(self):
        """Человекочитаемая подпись периода отпуска.

        'с 05.11.2026 по 15.11.2026', 'до 15.11.2026', 'с 05.11.2026',
        '' если ничего не задано.
        """
        if not (self.vacation_from or self.vacation_to):
            return ''
        if self.vacation_from and self.vacation_to:
            return (
                f'с {self.vacation_from:%d.%m.%Y} '
                f'по {self.vacation_to:%d.%m.%Y}'
            )
        if self.vacation_from:
            return f'с {self.vacation_from:%d.%m.%Y}'
        return f'по {self.vacation_to:%d.%m.%Y}'

    @property
    def employment_status_label(self):
        return self.get_employment_status_display()

    def dismiss(self, reason='', dismissed_at=None):
        """Уволить: is_active=False, статус=dismissed, дата=сегодня если не задана."""
        from django.utils import timezone
        self.is_active = False
        self.employment_status = self.EmploymentStatus.DISMISSED
        self.dismissed_at = dismissed_at or timezone.localdate()
        self.dismissed_reason = (reason or '')[:200]
        self.save(update_fields=[
            'is_active', 'employment_status', 'dismissed_at', 'dismissed_reason',
        ])

    def restore_from_dismissal(self):
        """Вернуть из увольнения: активировать, статус=active."""
        self.is_active = True
        self.employment_status = self.EmploymentStatus.ACTIVE
        self.dismissed_at = None
        self.dismissed_reason = ''
        self.save(update_fields=[
            'is_active', 'employment_status', 'dismissed_at', 'dismissed_reason',
        ])

    def open_tasks(self):
        """Открытые задачи (не done/cancelled), где этот сотрудник исполнитель."""
        from tasks.models import Task
        return Task.objects.filter(executor=self).exclude(
            status__in=[Task.Status.DONE, Task.Status.CANCELLED]
        )

    # ── Save / str ──

    def save(self, *args, **kwargs):
        if self.email:
            self.email = self.email.strip().lower()

        # Синхронизируем is_staff от актуальной роли/суперюзера
        self.is_staff = self.is_admin_role

        # Если save(update_fields=[...]) — добавляем is_staff в список,
        # иначе Django не сохранит его, и флаг останется устаревшим.
        update_fields = kwargs.get('update_fields')
        if update_fields is not None and 'is_staff' not in update_fields:
            kwargs['update_fields'] = list(update_fields) + ['is_staff']

        super().save(*args, **kwargs)

    def __str__(self):
        return f'{self.full_name} <{self.email}>'


class SupportTicket(models.Model):
    class Kind(models.TextChoices):
        BUG = 'bug', 'Жалоба / ошибка'
        IDEA = 'idea', 'Предложение по развитию'
        QUESTION = 'question', 'Вопрос'

    author = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='tickets',
        verbose_name='Автор',
    )
    kind = models.CharField('Тип', max_length=10, choices=Kind.choices, default=Kind.BUG)
    text = models.TextField('Сообщение', max_length=2000)
    page = models.CharField('Страница', max_length=200, blank=True)
    created_at = models.DateTimeField('Когда', auto_now_add=True)
    done = models.BooleanField('Обработано', default=False)

    class Meta:
        verbose_name = 'Обращение в техподдержку'
        verbose_name_plural = 'Обращения в техподдержку'
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.get_kind_display()}: {self.text[:40]}'


class ActivityAlert(models.Model):
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='activity_alerts',
        verbose_name='Сотрудник',
    )
    idle_since = models.DateTimeField('Простой начался')
    minutes = models.PositiveIntegerField('Минут простоя', default=0)
    created_at = models.DateTimeField('Сформировано', auto_now_add=True)

    class Meta:
        verbose_name = 'Отчёт по активности'
        verbose_name_plural = 'Отчёты по активности'
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.user}: простой {self.minutes} мин'
