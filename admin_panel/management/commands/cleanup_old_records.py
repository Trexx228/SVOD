from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone


DEFAULTS = {
    'audit_days': 180,
    'logins_days': 90,
    'alerts_days': 30,
    'snapshots_days': 400,
}


class Command(BaseCommand):
    help = (
        'Удаляет устаревшие записи: аудит, события входов, прочитанные алерты, '
        'старые снимки хронологии.'
    )

    def add_arguments(self, parser):
        for name, default in DEFAULTS.items():
            parser.add_argument(
                f'--{name.replace("_", "-")}',
                type=int,
                default=default,
                help=f'Срок хранения в днях (по умолчанию {default}).',
            )
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Показать, что было бы удалено, без удаления.',
        )

    def handle(self, *args, **options):
        now = timezone.now()

        from admin_panel.models import AuditLog, LoginEvent, AdminAlert
        from tasks.models import TimelineSnapshot

        audit_cutoff = now - timedelta(days=options['audit_days'])
        logins_cutoff = now - timedelta(days=options['logins_days'])
        alerts_cutoff = now - timedelta(days=options['alerts_days'])
        snapshots_cutoff = (now - timedelta(days=options['snapshots_days'])).date()

        qs_audit = AuditLog.objects.filter(created_at__lt=audit_cutoff)
        qs_logins = LoginEvent.objects.filter(created_at__lt=logins_cutoff)
        qs_alerts = AdminAlert.objects.filter(
            seen=True, last_seen_at__lt=alerts_cutoff,
        )
        qs_snapshots = TimelineSnapshot.objects.filter(
            snapshot_date__lt=snapshots_cutoff,
        )

        cnt_audit = qs_audit.count()
        cnt_logins = qs_logins.count()
        cnt_alerts = qs_alerts.count()
        cnt_snapshots = qs_snapshots.count()

        if options['dry_run']:
            self.stdout.write(self.style.WARNING('[dry-run]'))
            self.stdout.write(f'  AuditLog         < {options["audit_days"]} дн: {cnt_audit}')
            self.stdout.write(f'  LoginEvent       < {options["logins_days"]} дн: {cnt_logins}')
            self.stdout.write(f'  AdminAlert(seen) < {options["alerts_days"]} дн: {cnt_alerts}')
            self.stdout.write(f'  TimelineSnapshot < {options["snapshots_days"]} дн: {cnt_snapshots}')
            return

        if cnt_audit:
            qs_audit.delete()
        if cnt_logins:
            qs_logins.delete()
        if cnt_alerts:
            qs_alerts.delete()
        if cnt_snapshots:
            qs_snapshots.delete()

        total = cnt_audit + cnt_logins + cnt_alerts + cnt_snapshots
        self.stdout.write(self.style.SUCCESS(
            f'Удалено записей: {total} '
            f'(аудит {cnt_audit}, входы {cnt_logins}, '
            f'алерты {cnt_alerts}, снимки {cnt_snapshots}).'
        ))
