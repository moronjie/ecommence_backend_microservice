from ecom_common import AppException


class ServiceUnavailable(AppException):
    status_code = 503
    message = "Service unavailable"
