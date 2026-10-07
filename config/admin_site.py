from django.contrib.admin import AdminSite

from .version import __version__


SECTION_ORDER = [
    # (заголовок секции, [список app_label])
    ('Учётные записи',  ['accounts']),
    ('Нормативы',      ['core']),
    ('Заказы',         ['tasks']),
    ('Мотивация',      ['gamify']),
    ('Обращения',      ['accounts']),   # SupportTicket тоже в accounts
    ('Коммуникации',   ['comms']),
]


class SvodAdminSite(AdminSite):
    site_header = 'СВОД · администрирование портала'
    site_title = 'СВОД'
    index_title = 'Справочники, роли, задачи и заказы'

    def each_context(self, request):
        context = super().each_context(request)
        context['app_version'] = __version__
        return context

    def get_app_list(self, request, app_label=None):
        """Перегруппировать приложения по секциям и отсортировать по смыслу."""
        app_list = super().get_app_list(request, app_label)

        # Сортируем модели внутри каждого приложения по их verbose_name.
        for app in app_list:
            app['models'].sort(key=lambda m: m.get('name', ''))

        # Свой порядок по app_label.
        custom_order = ['accounts', 'core', 'tasks', 'gamify', 'comms']
        app_list.sort(
            key=lambda a: custom_order.index(a['app_label'])
            if a['app_label'] in custom_order else 99
        )

        # Заголовки приложений на русском (если ещё не заданы)
        renaming = {
            'accounts': 'Учётные записи и обращения',
            'core': 'Нормативы и типовые задачи',
            'tasks': 'Заказы и журналы',
            'gamify': 'Мотивация и достижения',
            'comms': 'Коммуникации (просмотр)',
        }
        for app in app_list:
            if app['app_label'] in renaming:
                app['name'] = renaming[app['app_label']]

        return app_list
