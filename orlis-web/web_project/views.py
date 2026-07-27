from django.views.generic import TemplateView
from web_project import TemplateLayout
from web_project.template_helpers.theme import TemplateHelper


class SystemView(TemplateView):
    template_name = "pages/system/not-found.html"
    status = ""

    # Esta view serve os handler400/403/404/500 declarados em config/urls.py, e nesse
    # papel tem de responder a qualquer método: um erro levantado durante um POST — uma
    # permissão negada numa view de escrita, por exemplo — chegava aqui e o TemplateView,
    # que só trata GET, respondia 405 em vez do código real.
    #
    # O "status" também só ia para o contexto do template e nunca para a resposta: as
    # páginas de erro saíam todas com 200 OK, portanto o browser e o JavaScript liam uma
    # página de "não autorizado" como se fosse sucesso.
    def dispatch(self, request, *args, **kwargs):
        return self.get(request, *args, **kwargs)

    def render_to_response(self, context, **response_kwargs):
        if self.status:
            response_kwargs.setdefault("status", int(self.status))
        return super().render_to_response(context, **response_kwargs)

    def get_context_data(self, **kwargs):
        # A function to init the global layout. It is defined in web_project/__init__.py file
        context = TemplateLayout.init(self, super().get_context_data(**kwargs))

        # Define the layout for this module
        # _templates/layout/system.html
        context.update(
            {
                "layout_path": TemplateHelper.set_layout("system.html", context),
                "status": self.status,
            }
        )

        return context
