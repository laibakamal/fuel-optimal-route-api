from rest_framework.views import exception_handler as _drf_handler


def problem_detail_handler(exc, context):
    return _drf_handler(exc, context)
