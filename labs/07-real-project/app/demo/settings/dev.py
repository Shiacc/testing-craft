from .base import *

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = True

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = "django-insecure-g3_m8h^)6@hw(4=**9an=#qrb4o((y+gy8)hd7i=1rerp(d(oo"

# SECURITY WARNING: define the correct hosts in production!
ALLOWED_HOSTS = ["*"]

# 本地自动化测试通过 127.0.0.1:8099 访问，管理后台的表单 POST 需要可信源
CSRF_TRUSTED_ORIGINS = [
    "http://127.0.0.1:8099",
    "http://localhost:8099",
]

EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"


try:
    from .local import *
except ImportError:
    pass
