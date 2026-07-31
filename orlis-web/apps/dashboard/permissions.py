"""Autorização partilhada entre o dashboard interno e a área de cliente.

Está num módulo próprio, e não em views.py, porque `apps/cliente/views.py` precisa das
mesmas verificações: as duas áreas tinham o mesmo problema — views que escolhiam o cliente
pelo `?cliente_id=` da query string, um valor controlado por quem chama — e importar views
de views para partilhar isto seria pior do que resolver.

A autenticação já estava tratada (quase toda). O que faltava era a autorização: o
LoginRequiredMixin responde "quem és tu?" e ninguém perguntava "e podes mexer nisto?".
"""
from django.contrib.auth.mixins import UserPassesTestMixin
from django.http import Http404


def cliente_do_utilizador(user):
    """O Cliente a que este utilizador está associado, ou None. Um superutilizador não está
    associado a nenhum — vê todos, e quem chama tem de tratar esse caso primeiro."""
    return getattr(getattr(user, 'clienteprofile', None), 'cliente', None)


def pode_ver_cliente(user, cliente_id):
    """Um superutilizador vê qualquer cliente; um utilizador de cliente só o seu. Sem
    associação, não vê nenhum."""
    if user.is_superuser:
        return True
    cliente = cliente_do_utilizador(user)
    if cliente is None or cliente_id in (None, ''):
        return False
    return str(cliente.idCliente) == str(cliente_id)


class SuperuserRequiredMixin(UserPassesTestMixin):
    """Restringe uma view aos utilizadores internos. O login já encaminha os
    superutilizadores para /dashboard/ e os restantes para /cliente/ (ver
    auth/login/views.py: redirect_by_role), mas as views de escrita são chamadas por POST a
    partir de JavaScript e nada impediria um utilizador de cliente autenticado de lhes
    chamar diretamente."""

    def test_func(self):
        return self.request.user.is_superuser


class ClienteQueryStringMixin:
    """Valida o ?cliente_id= da query string antes de a view o usar.

    Tem de vir DEPOIS do LoginRequiredMixin na lista de bases: assim o dispatch do login
    corre primeiro e um anónimo é encaminhado para o /login/ em vez de levar um 404.

    Responde 404 e não 403 de propósito, para não confirmar a existência do cliente a quem
    não lhe pertence — o mesmo critério da SensorDataAPIView."""

    def dispatch(self, request, *args, **kwargs):
        cliente_id = request.GET.get('cliente_id')
        if cliente_id and not pode_ver_cliente(request.user, cliente_id):
            raise Http404("Cliente não existe")
        return super().dispatch(request, *args, **kwargs)
