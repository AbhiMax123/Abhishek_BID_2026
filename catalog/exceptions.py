"""One consistent error envelope for every API error:

    {"error": {"code": "validation_error", "message": "...", "details": {...}}}
"""
import logging

from django.db import IntegrityError
from django.db.models import ProtectedError
from rest_framework import status
from rest_framework.exceptions import APIException, ValidationError
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

logger = logging.getLogger(__name__)

CODE_BY_STATUS = {
    400: "bad_request",
    401: "unauthorized",
    403: "permission_denied",
    404: "not_found",
    405: "method_not_allowed",
    406: "not_acceptable",
    409: "conflict",
    413: "payload_too_large",
    415: "unsupported_media_type",
    429: "throttled",
}


class FileTooLarge(APIException):
    status_code = status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
    default_detail = "The uploaded file is too large."
    default_code = "payload_too_large"


class Conflict(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_detail = "The request conflicts with the current state of the resource."
    default_code = "conflict"


def error_response(http_status, code, message, details=None):
    return Response(
        {"error": {"code": code, "message": message, "details": details or {}}},
        status=http_status,
    )


def api_exception_handler(exc, context):
    if isinstance(exc, ProtectedError):
        exc = Conflict("This object is still referenced by other records (e.g. a category that has products).")
    elif isinstance(exc, IntegrityError):
        exc = Conflict("The data conflicts with an existing record or a database constraint.")

    response = drf_exception_handler(exc, context)
    if response is None:
        logger.exception("Unhandled exception in API view", exc_info=exc)
        return error_response(500, "server_error", "An unexpected error occurred.")

    if isinstance(exc, ValidationError):
        details = response.data
        if not isinstance(details, dict):
            details = {"non_field_errors": details}
        response.data = {"error": {"code": "validation_error", "message": "Invalid input.", "details": details}}
    else:
        data = response.data
        message = data.get("detail") if isinstance(data, dict) else data
        response.data = {
            "error": {
                "code": CODE_BY_STATUS.get(response.status_code, "error"),
                "message": str(message),
                "details": {},
            }
        }
    return response
