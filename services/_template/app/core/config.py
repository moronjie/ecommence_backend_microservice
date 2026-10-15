from ecom_common.config import BaseServiceSettings


class Settings(BaseServiceSettings):
    service_name: str = "template-service"


settings = Settings()
