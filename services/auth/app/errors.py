from ecom_common import AppException


class TooManyAttempts(AppException):
    status_code = 429
    message = "Too many attempts"
