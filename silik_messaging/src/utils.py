import logging
from pydantic import (
    BaseModel,
    HttpUrl,
    model_validator,
    Field,
)
from pydantic_extra_types.phone_numbers import PhoneNumber, PhoneNumberValidator
from typing import Literal, Union, Optional, Self, List
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
    name: Annotated[
        Optional[str], Field(description="Optional username. Used only for Silik.")
    ] = ""
    kernel_name: Annotated[
        Optional[str],
        Field(
            description="Name of the kernel to start for this user. See kernel_connection_file for connecting an existing kernel. Run `jupyter kernelspec list` for the list of kernels available on your machine."
        ),
    ] = None
    kernel_connection_file: Annotated[
        Optional[str],
        Field(
            description="Specify this to connect to an existing kernel. Must be the path to the kernel connection file. See https://jupyter-client.readthedocs.io/en/stable/kernels.html#connection-files. Run `jupyter --runtime-dir` to see where connection files are stored on your machine. You can also run a kernel with jupyter console, and specify where its connection file is stored with command : `jupyter console --ConnectionFileMixin.connection_file ./kernel_connection_file_test.json`"
        ),
    ] = None
    uuid: Annotated[
        Optional[UUID],
        Field(
            description="The uuidv4 of the signal user. Either this or the phone number must be specified."
        ),
    ] = None
    phone_number: Annotated[
        Optional[E164NumberType],
        Field(
            description="Phone number of the user. Can be replaced by uuid, but one of the two must be specified."
        ),
    ] = None

    @model_validator(mode="after")
    def check_uuid_or_phone(self) -> Self:
        if self.uuid is None and self.phone_number is None:
            raise ValueError(
                f"For user {self.name}, please specify either the `uuid` or the `phone_number` field."
            )
        return self


class SilikSignalConfig(BaseModel):
    api_url: Annotated[HttpUrl, Field(description="URL towards the signal REST API")]
    harvest_delay: Annotated[
        float,
        Field(
            description="Number of seconds between each harvest of the Signal Messages"
        ),
    ]
    whitelist: Annotated[
        List[SilikUserConfig], Field(description="The list of users whitelisted.")
    ]
    logging_level: Annotated[
        Literal[
            "NOTSET", "DEBUG", "INFO", "WARN", "WARNING", "ERROR", "CRITICAL", "FATAL"
        ],
        Field(
            description="Logging level for the application", default_factory=lambda: []
        ),
    ]

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


if __name__ == "__main__":
    import json

    json_schema = SilikSignalConfig.model_json_schema()
    with open("config_scheme.json", "wt") as f:
        json.dump(json_schema, f)
