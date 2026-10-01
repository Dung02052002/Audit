"""HTTP helpers shared by the API routers (Prompt Pack v8, prompt #043), X1.

As the user approved on 2026-10-01:

- ``provide(interface)`` resolves a service from the app's container.
- ``current_actor`` is the one place that says who sends a request. There is
  no login yet, so every request is ``LOCAL_USER``; replacing this dependency
  is all a real login needs to change.
- Every error leaves the API in one envelope,
  ``{"error": {"code", "category", "message", "retryable", "fields"?}}``,
  built from ``PublicError`` (#008). ``install_error_handlers`` registers it:

  - an ``AppError`` uses its own code and HTTP status;
  - a request the schema refuses is 422 ``validation.invalid_request``, with
    one ``{"field", "message"}`` entry per problem;
  - a Starlette HTTP error (unknown route, wrong method) keeps its status,
    with a ``request.*`` code;
  - anything else is a 500 ``application.internal`` whose detail only goes
    to the log.
"""

from collections.abc import Sequence
from http import HTTPStatus
from typing import Any, TypeVar

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from ai_youtube_agent.core.audit import Actor, ActorKind
from ai_youtube_agent.core.errors import AppError, ErrorCategory, to_public
from ai_youtube_agent.core.log import get_logger

T = TypeVar("T")
logger = get_logger(__name__)

LOCAL_USER = Actor(ActorKind.USER, "local-user")
VALIDATION_CODE = "validation.invalid_request"
VALIDATION_MESSAGE = "Some fields are missing or not valid."


def provide(interface: type[T]) -> Any:
    """FastAPI dependency that resolves ``interface`` from the app's container.

    Use it as ``service: Annotated[Service, provide(Service)]``.
    """

    def resolve(request: Request) -> T:
        return request.app.state.container.resolve(interface)

    return Depends(resolve)


def current_actor() -> Actor:
    """Who sends the request. Every request is the local user until login."""
    return LOCAL_USER


def error_body(
    code: str,
    category: ErrorCategory,
    message: str,
    *,
    retryable: bool = False,
    fields: Sequence[dict[str, str]] | None = None,
) -> dict[str, Any]:
    error: dict[str, Any] = {
        "code": code,
        "category": category.value,
        "message": message,
        "retryable": retryable,
    }
    if fields is not None:
        error["fields"] = list(fields)
    return {"error": error}


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, _app_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_exception_handler(Exception, _unexpected_error)


async def _app_error(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, AppError)
    public = exc.to_public()
    level = logger.error if public.http_status >= 500 else logger.info
    level("request refused", extra={"fields": exc.log_fields()})
    return JSONResponse(
        error_body(
            public.code, public.category, public.message, retryable=public.retryable
        ),
        status_code=public.http_status,
    )


async def _validation_error(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    fields = [_field(error) for error in exc.errors()]
    return JSONResponse(
        error_body(
            VALIDATION_CODE, ErrorCategory.DOMAIN, VALIDATION_MESSAGE, fields=fields
        ),
        status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
    )


async def _http_error(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    status = HTTPStatus(exc.status_code)
    code = "request." + status.phrase.lower().replace(" ", "_").replace("-", "_")
    category = ErrorCategory.DOMAIN if status < 500 else ErrorCategory.APPLICATION
    return JSONResponse(
        error_body(code, category, status.phrase + "."),
        status_code=status,
        headers=getattr(exc, "headers", None),
    )


async def _unexpected_error(_: Request, exc: Exception) -> JSONResponse:
    logger.exception("unexpected error", exc_info=exc)
    public = to_public(exc)
    return JSONResponse(
        error_body(public.code, public.category, public.message),
        status_code=public.http_status,
    )


def _field(error: dict[str, Any]) -> dict[str, str]:
    location = [str(part) for part in error.get("loc", ()) if part != "body"]
    message = str(error.get("msg", "Not valid."))
    if message.startswith("Value error, "):
        message = message[len("Value error, ") :]
    return {"field": ".".join(location) or "body", "message": message}
