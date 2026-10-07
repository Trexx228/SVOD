from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

admin.site.site_header = 'СВОД · администрирование портала'
admin.site.site_title = 'СВОД'
admin.site.index_title = 'Справочники, роли, задачи и заказы'

urlpatterns = [
    path('admin/', admin.site.urls),
    path('accounts/', include('accounts.urls')),
    path('tasks/', include('tasks.urls')),
    path('manager/', include('manager.urls')),
    path('comms/', include('comms.urls')),
    path('motivation/', include('gamify.urls')),
    path('me/', include('cabinet.urls')),
    path('', include('dashboard.urls')),
    path('settings/', include('admin_panel.urls')),
]

# Раздача media: в dev — всегда, в проде — только при явном флаге
# (пилот; в 7.3 это заберёт nginx).
import os as _os
if settings.DEBUG or _os.environ.get('DJANGO_SERVE_MEDIA') == '1':
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
