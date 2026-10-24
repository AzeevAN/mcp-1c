"""Вход и завершение cookie-сессии дашборда."""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse

from ..dashboard_backend import (
    COOKIE,
    LEVEL_ADMIN,
    SESSION_TTL_SECONDS,
    admin_token,
    api_token,
    check_token,
    create_session,
    csrf_denied,
    drop_session,
    login_from_foreign_site,
)
from ..runtime_config import ACCESS_HTTPS_PROXY, AccessModeError, access_mode
from .context import DashboardContext, Endpoint


def _secure_cookie(request: Request) -> bool:
    """`Secure` нужен и за TLS proxy, даже если до backend дошла схема http.

    Схема запроса за proxy верна, только когда ему доверены X-Forwarded-*;
    режим `https-proxy` сам по себе означает, что браузер видит HTTPS.
    """
    if request.url.scheme == "https":
        return True
    try:
        return access_mode() == ACCESS_HTTPS_PROXY
    except AccessModeError:
        return False


def handlers(context: DashboardContext) -> dict[str, Endpoint]:
    async def login(request: Request):
        """Обменять токен на серверную cookie-сессию SPA."""
        if not admin_token() and not api_token():
            return PlainTextResponse(
                "Вход выключен: не задан ни ADMIN_TOKEN, ни API_TOKEN.", 404
            )
        if login_from_foreign_site(request):
            # До чтения формы и сверки: такая попытка не тратит лимит
            # перебора, общий у всех клиентов за одним адресом.
            return PlainTextResponse(
                "Вход принимается только со страницы этого сервера.", 403
            )
        form = await request.form()
        given = str(form.get("token", ""))
        # Лимит проверяется после чтения формы и вместе со сверкой: иначе
        # запросы, придержанные на теле, прошли бы проверку все сразу.
        level, retry_after = check_token(request, given)
        if retry_after is not None:
            return PlainTextResponse(
                "Слишком много неудачных попыток входа. Повторите позже.",
                status_code=429,
                headers={"Retry-After": str(retry_after)},
            )
        if level is None:
            return PlainTextResponse("Неверный токен.", status_code=403)

        session = create_session(level)
        response = RedirectResponse(
            "/sources" if level == LEVEL_ADMIN else "/", status_code=303
        )
        response.set_cookie(
            COOKIE,
            session,
            max_age=SESSION_TTL_SECONDS,
            httponly=True,
            samesite="strict",
            secure=_secure_cookie(request),
            path="/",
        )
        return response

    async def logout(request: Request):
        """Закрыть cookie-сессию с обязательной same-origin проверкой."""
        denied = csrf_denied(request)
        if denied is not None:
            return denied
        drop_session(request.cookies.get(COOKIE, ""))
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(
            COOKIE,
            path="/",
            secure=_secure_cookie(request),
            httponly=True,
            samesite="strict",
        )
        return response

    return {
        "login": login,
        "logout": logout,
    }
