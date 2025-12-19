import logging
from pydantic import (
    BaseModel,
    HttpUrl,
    ValidationError,
    field_validator,
)
from pydantic_extra_types.phone_numbers import PhoneNumber, PhoneNumberValidator
from typing import Literal, Union
from typing_extensions import Annotated
import yaml
from uuid import UUID


def get_base_logger(name) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    return logger


logger = get_base_logger(__name__)

E164NumberType = Annotated[
    Union[str, PhoneNumber], PhoneNumberValidator(number_format="E164")
]


class SilikSignalConfig(BaseModel):
    api_url: HttpUrl
    harvest_delay: float
    whitelist: list[E164NumberType | UUID]
    kernel_name: str = "silik"
    logging_level: Literal[
        "NOTSET", "DEBUG", "INFO", "WARN", "WARNING", "ERROR", "CRITICAL", "FATAL"
    ]

    @field_validator("whitelist", mode="before")
    @classmethod
    def whitelist_check(cls, whitelist: list | None):
        if whitelist is None:
            whitelist = []
        if not isinstance(whitelist, list):
            raise ValidationError("Config whitelist must be a list")
        return whitelist


def get_config() -> SilikSignalConfig:
    """
    Return config located at name. Validate with pydantic class.
    """
    with open("config.yaml", "rt") as f:
        d = yaml.safe_load(f)
    config = SilikSignalConfig.model_validate(d)
    logger.debug("Loaded config.")
    return config


config = get_config()


def get_logger(name) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        logger.addHandler(handler)
    logger.setLevel(config.logging_level)
    logger.propagate = False
    return logger
