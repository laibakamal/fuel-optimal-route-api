"""RFC 7807 problem details for every error this API can produce.

One shape for every failure, so a client never has to guess. RFC 7807 rather than
a bespoke envelope because it is a published standard with a registered media
type (application/problem+json), which means tooling already understands it.

Beyond the standard members (type, title, status, detail) each response carries:

    code         a stable machine-readable string, so clients branch on this
                 rather than on prose that we might reword.
    assumptions  the same assumptions block the success response carries. An
                 infeasible route is only infeasible *relative to* a 500 mi range
                 and a 10 mi detour tolerance, so returning the error without
                 them would be unactionable.
"""
from __future__ import annotations

import logging

from django.conf import settings
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

logger = logging.getLogger(__name__)

PROBLEM_CONTENT_TYPE = "application/problem+json"
#: Stable, documented URIs. They do not have to resolve to be useful as
#: identifiers, but they namespace our error types away from anyone else's.
PROBLEM_TYPE_BASE = "https://github.com/laibakamal/fuel-optimal-route-api/errors"


def problem_response(
    *,
    code: str,
    title: str,
    detail: str,
    http_status: int,
    extra: dict | None = None,
    assumptions: dict | None = None,
) -> Response:
    body = {
        "type": f"{PROBLEM_TYPE_BASE}/{code.replace('_', '-')}",
        "title": title,
        "status": http_status,
        "detail": detail,
        "code": code,
    }
    if extra:
        body.update(extra)
    if assumptions is not None:
        body["assumptions"] = assumptions
    return Response(body, status=http_status, content_type=PROBLEM_CONTENT_TYPE)


def validation_problem(errors: dict) -> Response:
    """Serializer errors as a problem document, keeping per-field detail."""
    return problem_response(
        code="invalid_request",
        title="Request validation failed",
        detail="One or more parameters are invalid. See `errors` for details.",
        http_status=status.HTTP_400_BAD_REQUEST,
        extra={"errors": errors},
    )


def problem_detail_handler(exc, context):
    """DRF exception hook: render DRF's own errors as problem documents too.

    Without this, a 405 or a parse error would come back in DRF's default shape
    while our own errors used RFC 7807 - two formats for one API.
    """
    response = drf_exception_handler(exc, context)
    if response is None:
        return None

    detail = response.data
    if isinstance(detail, dict) and "detail" in detail:
        message = str(detail["detail"])
        extra = None
    elif isinstance(detail, dict):
        message = "One or more parameters are invalid. See `errors` for details."
        extra = {"errors": detail}
    else:
        message = str(detail)
        extra = None

    code = getattr(exc, "default_code", "error") or "error"
    body = {
        "type": f"{PROBLEM_TYPE_BASE}/{str(code).replace('_', '-')}",
        "title": getattr(exc, "__class__").__name__,
        "status": response.status_code,
        "detail": message,
        "code": str(code),
    }
    if extra:
        body.update(extra)
    response.data = body
    response.content_type = PROBLEM_CONTENT_TYPE
    return response
