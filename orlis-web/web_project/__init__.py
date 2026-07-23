from web_project.template_helpers.theme import TemplateHelper
from django.conf import settings

class TemplateLayout:
    def init(self, context):
        context = TemplateHelper.init_context(context)
        layout = context["layout"]

        user = getattr(self.request, 'user', None)

        if user and user.is_authenticated:
            if user.is_superuser:
                index_url = '/dashboard/'
                menu_data = {
                    "menu": [
                        {
                            "url": "dashboard",
                            "icon": "menu-icon tf-icons bx bx-home-circle",
                            "name": "Dashboard",
                            "slug": "dashboard"
                        },
                        {
                            "url": "table_clients",
                            "icon": "menu-icon tf-icons bx bx-table",
                            "name": "Tabela Clientes",
                            "slug": "table_clients"
                        }
                    ]
                }
            else:
                index_url = '/cliente/'
                menu_data = {
                    "menu": [
                        {
                            "url": "cliente-chillers", 
                            "icon": "menu-icon tf-icons bx bx-home-circle",
                            "name": "Meus Chillers",
                            "slug": "table_clients"
                        }
                    ]
                }
        else:
            index_url = '/login/'
            menu_data = {"menu": []}

        context.update(
            {
                "layout_path": TemplateHelper.set_layout(
                    "layout_" + layout + ".html", context
                ),
                "rtl_mode": True
                if self.request.COOKIES.get('django_text_direction') == "rtl"
                else settings.TEMPLATE_CONFIG.get("rtl_mode"),
                "index_url": index_url,
                "menu_data": menu_data
            }
        )

        TemplateHelper.map_context(context)

        return context
