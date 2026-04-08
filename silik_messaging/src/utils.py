import logging
from pydantic import (
    BaseModel,
    HttpUrl,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_extra_types.phone_numbers import PhoneNumber, PhoneNumberValidator
from typing import Literal, Union, Optional, Self
from typing_extensions import Annotated
import yaml
from uuid import UUID


def get_base_logger(name) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        file_handler = logging.FileHandler("logs/silik.log", encoding="utf-8")
        file_handler.setFormatter(logging.Formatter("%(levelname)s | %(message)s"))
        logger.addHandler(file_handler)
        logger.addHandler(stream_handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    return logger


logger = get_base_logger(__name__)

E164NumberType = Annotated[
    Union[str, PhoneNumber], PhoneNumberValidator(number_format="E164")
]


class SilikUserConfig(BaseModel):
    name: str
    kernel_name: str = "silik"
    uuid: Optional[UUID] = None
    phone_number: Optional[E164NumberType] = None

    @model_validator(mode="after")
    def check_uuid_or_phone(self) -> Self:
        if self.uuid is None and self.phone_number is None:
            raise ValueError(
                f"For user {self.name}, please specify either the `uuid` or the `phone_number` field."
            )
        return self


class SilikSignalConfig(BaseModel):
    api_url: HttpUrl
    harvest_delay: float
    whitelist: list[SilikUserConfig]
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

    def get_user_from_id(self, uid) -> SilikUserConfig:
        for each_user in self.whitelist:
            if each_user.uuid == uid:
                return each_user
        raise KeyError(f"No user was found in whitelist for uuid : {uid}")

    def check_uuid_in_whitelist(self, uid) -> bool:
        try:
            self.get_user_from_id(uid)
        except KeyError:
            return False
        return True


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
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(
            logging.Formatter("[ %(asctime)s %(levelname)s ] %(name)s | %(message)s")
        )

        file_handler = logging.FileHandler("logs/silik.log", encoding="utf-8")
        file_handler.setFormatter(
            logging.Formatter("[ %(asctime)s %(levelname)s ] %(name)s | %(message)s")
        )

        logger.addHandler(file_handler)
        logger.addHandler(stream_handler)
    logger.setLevel(config.logging_level)
    logger.propagate = False
    return logger
