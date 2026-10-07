"""Уведомления о приближении контрольных точек заказов.

Запускается из scheduler раз в сутки утром (после дайджеста).

Для каждого заказа проверяет 4 контрольные точки:
    contract_start, design_start, design_end, ship_due

За 7 и 3 РАБОЧИХ дня до каждой точки отправляет уведомление
ответственному за заказ (Order.owner).

Дедупликация — через OrderDeadlineNotification. Если срок точки
изменился (owner перенёс), запись создастся заново, и уведомление
уйдёт по новому сроку.
"""
import logging

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.urls import reverse
from django.utils import timezone

from comms.models import Notification
from comms.services import notify
from tasks.models import Order, OrderDeadlineNotification
from tasks.utils import add_work_days

logger = logging.getLogger(__name__)
User = get_user_model()


# ── Пороги в рабочих днях ──
# Правь здесь, если нужны другие интервалы.
THRESHOLDS = (7, 3)

# ── Какие точки проверяем ──
# (код в модели, атрибут заказа, человекочитаемое название)
ANCHORS = [
    ('contract_start', 'contract_start', 'Начало контракта'),
    ('design_start',   'design_start',   'Старт проектирования'),
    ('design_end',     'design_end',     'Конец проектирования'),
    ('ship_due',       'ship_due',       'Отгрузка'),
]


class Command(BaseCommand):
    help = 'Уведомления о контрольных точках заказов за 7 и 3 рабочих дня.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Показать, что было бы отправлено, но не отправлять.',
        )

    def handle(self, *args, **options):
        dry = options['dry_run']
        today = timezone.localdate()
        sent = skipped = 0

        for anchor_code, attr, label in ANCHORS:
            for days_before in THRESHOLDS:
                for order in self._orders_to_notify(attr, today, days_before):
                    anchor_date = getattr(order, attr)

                    # Уже отправляли именно для этой даты?
                    already = OrderDeadlineNotification.objects.filter(
                        order=order,
                        anchor=anchor_code,
                        days_before=days_before,
                        anchor_date=anchor_date,
                    ).exists()
                    if already:
                        skipped += 1
                        continue

                    recipients = self._recipients(order)
                    if not recipients:
                        skipped += 1
                        continue

                    if dry:
                        self.stdout.write(
                            f'[dry] {order.number} · {label} · '
                            f'−{days_before} д. ({anchor_date}) · '
                            f'{len(recipients)} получателей'
                        )
                        sent += 1
                        continue

                    try:
                        self._send(order, label, attr, anchor_code,
                                   anchor_date, days_before, recipients)
                        sent += 1
                    except Exception as e:
                        logger.exception(
                            'notify_order_deadlines: failed for order %s',
                            order.pk,
                        )
                        self.stderr.write(f'{order.number}: {e}')

        self.stdout.write(self.style.SUCCESS(
            f'Готово. Отправлено: {sent}, пропущено: {skipped}.'
        ))

    # ── Кого выбираем ─────────────────────────────────────────────
    def _orders_to_notify(self, attr, today, days_before):
        """Заказы, у которых до точки ровно `days_before` рабочих дней.

        Логика: если сегодня + N рабочих дней = дата точки, значит до неё
        осталось ровно N смен. Это эквивалентно «за N рабочих дней до срока».
        """
        target = add_work_days(today, days_before)
        qs = (
            Order.objects
            .filter(**{attr: target, 'owner__isnull': False})
            .select_related('owner')
        )
        return list(qs)

    def _recipients(self, order):
        """Кто получает уведомление. Пока — только owner заказа."""
        if not order.owner or not order.owner.is_active:
            return []
        return [order.owner]

    # ── Отправка ──────────────────────────────────────────────────
    def _send(self, order, label, attr, anchor_code,
              anchor_date, days_before, recipients):
        """Внутреннее уведомление + запись в дедупликатор."""
        url = reverse('settings_order_detail', args=[order.pk])
        text = (
            f'📅 Заказ {order.number}: через {days_before} раб. д. — '
            f'{label} ({anchor_date:%d.%m.%Y})'
        )

        notify(recipients, text, url, Notification.Kind.ACTION)

        OrderDeadlineNotification.objects.create(
            order=order,
            anchor=anchor_code,
            days_before=days_before,
            anchor_date=anchor_date,
        )
