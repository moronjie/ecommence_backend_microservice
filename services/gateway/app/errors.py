from ecom_common import AppException


class TooManyRequests(AppException):
    status_code = 429
    message = "Too many requests"


class ServiceUnavailable(AppException):
    status_code = 503
    message = "Service unavailable"


class BadGateway(AppException):
    status_code = 502
    message = "Bad gateway"


class GatewayTimeout(AppException):
    status_code = 504
    message = "Gateway timeout"
