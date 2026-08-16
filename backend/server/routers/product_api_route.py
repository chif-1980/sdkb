"""Route-local error responses for enterprise assistant APIs."""

from fastapi import HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRoute


class ProductApiRoute(APIRoute):
    def get_route_handler(self):
        route_handler = super().get_route_handler()

        async def product_route_handler(request: Request) -> Response:
            try:
                return await route_handler(request)
            except RequestValidationError:
                return JSONResponse(
                    status_code=422,
                    content={
                        "error": {
                            "code": "REQUEST_VALIDATION_ERROR",
                            "message": "请求参数不合法",
                        }
                    },
                )
            except HTTPException as exc:
                detail = exc.detail if isinstance(exc.detail, dict) else {}
                code = detail.get("code", "REQUEST_FAILED")
                message = detail.get("message", "请求处理失败")
                return JSONResponse(
                    status_code=exc.status_code,
                    content={"error": {"code": code, "message": message}},
                    headers=exc.headers,
                )

        return product_route_handler
