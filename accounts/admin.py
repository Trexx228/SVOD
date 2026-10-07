from django.contrib import admin, messages
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.db.models import Count, Q
from django.shortcuts import render

from .models import Department, Role, SupportTicket, User


@admin.register(Department)
class DepartmentAdmin(admin.ModelAdmin):
    list_display = ('name', 'created_at', 'users_count')
    search_fields = ('name',)
    ordering = ('name',)
    list_per_page = 25

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.annotate(
            users_count=Count(
                'users',
                filter=Q(users__is_active=True),
                distinct=True,
            )
        )

    @admin.display(description='Людей', ordering='users_count')
    def users_count(self, obj):
        return obj.users_count


@admin.register(Role)
class RoleAdmin(admin.ModelAdmin):
    list_display = ('name', 'code', 'can_manage', 'can_admin', 'can_plant', 'user_count')
    list_editable = ('can_manage', 'can_admin', 'can_plant')
    search_fields = ('name', 'code')
    ordering = ('name',)

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.annotate(user_count=Count('users', distinct=True))

    @admin.display(description='Людей с ролью', ordering='user_count')
    def user_count(self, obj):
        return obj.user_count


@admin.register(SupportTicket)
class SupportTicketAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'kind', 'author', 'text_short', 'done')
    list_filter = ('kind', 'done')
    list_editable = ('done',)
    search_fields = ('text', 'author__full_name')
    date_hierarchy = 'created_at'
    list_per_page = 25
    list_select_related = ('author',)

    @admin.display(description='Сообщение')
    def text_short(self, obj):
        return (obj.text or '')[:60]


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    ordering = ('email',)
    list_display = ('email', 'full_name', 'department', 'role', 'is_active')
    list_filter = ('role', 'is_active', 'department')
    search_fields = ('email', 'full_name', 'department_hint')
    list_per_page = 25
    list_select_related = ('department', 'role')
    date_hierarchy = 'date_joined'
    readonly_fields = ('last_login', 'date_joined', 'tour_seen')

    actions = (
        'confirm_emails',
        'set_manager',
        'set_admin',
        'set_staff',
        'deactivate',
        'activate_users',
        'assign_department',
    )

    fieldsets = (
        (None, {'fields': ('email', 'password')}),
        ('Личное', {'fields': ('full_name',)}),
        ('Оргструктура', {'fields': ('department_hint', 'department', 'role')}),
        ('Доступ', {'fields': ('is_active', 'is_staff', 'is_superuser')}),
        ('Обучение', {'fields': ('tour_seen',), 'classes': ('collapse',)}),
    )

    add_fieldsets = (
        (
            None,
            {
                'classes': ('wide',),
                'fields': (
                    'email',
                    'full_name',
                    'password1',
                    'password2',
                    'role',
                    'department',
                ),
            },
        ),
    )

    @admin.action(description='Активировать (почта подтверждена вручную)')
    def confirm_emails(self, request, queryset):
        queryset.update(is_active=True)

    @admin.action(description='Отключить учётные записи')
    def deactivate(self, request, queryset):
        queryset.exclude(pk=request.user.pk).update(is_active=False)

    @admin.action(description='Включить учётные записи')
    def activate_users(self, request, queryset):
        queryset.update(is_active=True)

    def _set_role(self, queryset, code, name):
        role, _ = Role.objects.get_or_create(
            code=code,
            defaults={
                'name': name,
                'can_manage': code in ('manager', 'admin'),
                'can_admin': code == 'admin',
            },
        )
        queryset.update(role=role)

    @admin.action(description='Назначить руководителем')
    def set_manager(self, request, queryset):
        self._set_role(queryset, 'manager', 'Руководитель подразделения')

    @admin.action(description='Назначить администратором')
    def set_admin(self, request, queryset):
        self._set_role(queryset, 'admin', 'Администратор')

    @admin.action(description='Вернуть роль инженера')
    def set_staff(self, request, queryset):
        self._set_role(queryset, 'staff', 'Инженер-конструктор')

    @admin.action(description='Распределить по подразделению…')
    def assign_department(self, request, queryset):
        if request.POST.get('apply'):
            raw_ids = request.POST.get('ids') or ''
            ids = [int(pk) for pk in raw_ids.split(',') if pk.strip().isdigit()]

            if not ids:
                ids = list(queryset.values_list('pk', flat=True))

            if not ids:
                self.message_user(
                    request,
                    'Не выбраны пользователи.',
                    messages.WARNING,
                )
                return None

            department_value = (request.POST.get('department') or '').strip()
            department = None

            if department_value:
                if not department_value.isdigit():
                    self.message_user(
                        request,
                        'Некорректное подразделение.',
                        messages.ERROR,
                    )
                    return None

                department = Department.objects.filter(pk=department_value).first()
                if department is None:
                    self.message_user(
                        request,
                        'Подразделение не найдено.',
                        messages.ERROR,
                    )
                    return None

            updated = self.get_queryset(request).filter(pk__in=ids).update(
                department=department
            )
            self.message_user(
                request,
                f'Подразделение назначено: {updated} чел.',
                messages.SUCCESS,
            )
            return None

        ids = list(queryset.values_list('pk', flat=True))
        if not ids:
            self.message_user(
                request,
                'Не выбраны пользователи.',
                messages.WARNING,
            )
            return None

        return render(
            request,
            'admin/assign_department.html',
            {
                'ids': ','.join(map(str, ids)),
                'ids_list': ids,
                'departments': Department.objects.all(),
                'count': len(ids),
            },
        )
