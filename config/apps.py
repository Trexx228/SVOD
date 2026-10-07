from django.contrib.admin.apps import AdminConfig


class SvodAdminConfig(AdminConfig):
    default_site = 'config.admin_site.SvodAdminSite'
