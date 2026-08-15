"""Citation endpoints for the enterprise assistant."""

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse

from server.utils.auth_middleware import get_product_user
from yuxi.product_chat.citation_service import CitationResolutionError, CitationService
from yuxi.product_chat.schemas import CitationResponse
from yuxi.storage.postgres.manager import pg_manager
from yuxi.storage.postgres.models_business import User
from yuxi.storage.postgres.models_product import MessageCitation
from yuxi.utils.datetime_utils import format_utc_datetime

product_citation = APIRouter()


def _citation_response(citation: MessageCitation) -> CitationResponse:
    return CitationResponse(
        id=citation.citation_id,
        kind=citation.kind,
        title=citation.title,
        path=citation.path_text,
        locator=citation.locator,
        excerpt=citation.excerpt,
        version_at=format_utc_datetime(citation.source_version_at),
    )


def _http_error(error: CitationResolutionError) -> HTTPException:
    return HTTPException(
        status_code=error.status_code,
        detail={"code": error.code, "message": error.message},
    )


@product_citation.get(
    "/citations/{citation_id}",
    response_model=CitationResponse,
)
async def get_citation(
    citation_id: str,
    current_user: User = Depends(get_product_user),
) -> CitationResponse:
    try:
        async with pg_manager.get_async_session_context() as db:
            citation = await CitationService(db).resolve(citation_id, current_user)
            return _citation_response(citation)
    except CitationResolutionError as exc:
        raise _http_error(exc) from None


@product_citation.get("/citations/{citation_id}/open")
async def open_citation(
    citation_id: str,
    current_user: User = Depends(get_product_user),
) -> RedirectResponse:
    try:
        async with pg_manager.get_async_session_context() as db:
            citation = await CitationService(db).resolve(citation_id, current_user)
            return RedirectResponse(url=citation.source_url, status_code=307)
    except CitationResolutionError as exc:
        raise _http_error(exc) from None
