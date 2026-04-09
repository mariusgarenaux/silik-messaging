# Contains all objects needed to dialog with Signal REST API :
#   - pydantic base model that can validate and parse the raw messages
#   - HTTP connector
#   - database of all messages from a Signal Account
#   - chat objects, containing a conversation with a user

import requests
from pydantic import (
    BaseModel,
    UUID4,
    Field,
    NonNegativeInt,
    TypeAdapter,
    field_validator,
)
from pydantic_extra_types.phone_numbers import PhoneNumber
from requests import HTTPError
import os
from uuid import UUID
from typing import Any
from .utils import get_logger, config, E164NumberType, SilikUserConfig

logger = get_logger(__name__)

SIGNAL_URL = str(config.api_url)

# ---------------------------------------------------
# ------------ Pydantic Validation Models -----------
# ---------------------------------------------------


class SignalAttachmentsModel(BaseModel):
    contentType: str
    filename: str | None = None
    id: str
    size: NonNegativeInt
    width: NonNegativeInt | None = None
    height: NonNegativeInt | None = None
    caption: str | None = None
    uploadTimestamp: NonNegativeInt


class SignalSentMessageModel(BaseModel):
    destination: str | None = None
    destinationNumber: E164NumberType | None = None
    destinationUuid: UUID4 | None = None
    timestamp: NonNegativeInt | None = None
    message: str | None = None
    expiresInSeconds: NonNegativeInt | None = None
    viewOnce: bool | None = None
    attachments: list[SignalAttachmentsModel] | None = None


class SignalReceiptMessageModel(BaseModel):
    when: NonNegativeInt
    isDelivery: bool
    isRead: bool
    isViewed: bool
    timestamps: list[NonNegativeInt]


class SignalReactionModel(BaseModel):
    emoji: str
    targetAuthor: str
    targetAuthorNumber: PhoneNumber | None = None
    targetAuthorUuid: UUID4
    targetSentTimestamp: NonNegativeInt
    isRemove: bool


class SignalGroupInfoModel(BaseModel):
    groupId: str
    groupName: str
    revision: NonNegativeInt
    type: str


class SignalDataMessageModel(BaseModel):
    timestamp: NonNegativeInt
    message: str | None = None
    expiresInSeconds: NonNegativeInt
    viewOnce: bool
    reaction: SignalReactionModel | None = None
    groupInfo: SignalGroupInfoModel | None = None


class SignalReadMessageModel(BaseModel):
    sender: str
    senderNumber: E164NumberType | None = None
    senderUuid: UUID4
    timestamp: NonNegativeInt


class SignalSyncMessageModel(BaseModel):
    readMessages: list[SignalReadMessageModel] | None = None
    sentMessage: SignalSentMessageModel | None = None


class SignalEnvelopeModel(BaseModel):
    source: str
    sourceNumber: E164NumberType | None = None
    sourceUuid: UUID4 | None = None
    sourceName: str = Field(max_length=25)
    sourceDevice: NonNegativeInt
    timestamp: NonNegativeInt
    serverReceivedTimestamp: NonNegativeInt
    serverDeliveredTimestamp: NonNegativeInt
    syncMessage: SignalSyncMessageModel | None = None
    receiptMessage: SignalReceiptMessageModel | None = None
    dataMessage: SignalDataMessageModel | None = None


class SignalMessageModel(BaseModel):
    envelope: SignalEnvelopeModel
    account: E164NumberType


class SignalContactProfile(BaseModel):
    given_name: str | None = None
    lastname: str | None = None
    about: str | None = None
    has_avatar: bool | None = None
    last_updated_timestamp: int | None = None


class SignalNickname(BaseModel):
    name: str | None = None
    given_name: str | None = None
    family_name: str | None = None


class SignalContact(BaseModel):
    uuid: UUID
    number: E164NumberType | None = None
    name: str | None = None
    profile_name: str | None = None
    username: str | None = None
    color: str | None = None
    blocked: bool | None = None
    message_expiration: str | None = None
    note: str | None = None
    profile: SignalContactProfile
    given_name: str | None = None
    nickname: SignalNickname

    @field_validator("number", mode="before")
    @classmethod
    def empty_number(cls, number: Any):
        if isinstance(number, str) and len(number) == 0:
            return
        else:
            return number


# ----------------------------------------------------------
# --------------------- Database Elements ------------------
# ----------------------------------------------------------


class SignalMessage:
    """
    Shortcut towards signal message model object. Ensure
    this is a message that needs to be treated :
        - data message or message from yourself
    """

    def __init__(self, raw_message: SignalMessageModel):
        self.envelope = raw_message.envelope
        validate_result = self.validate_message_type(raw_message)
        if validate_result is None:
            raise ValueError(
                "Please initialize SignalMessage class with either dataMessage unempty syncMessage from your number"
            )
        message = validate_result
        if message is None:
            raise ValueError(
                "Please initialize SignalMessage class with either dataMessage unempty syncMessage from your number"
            )

        self.message = message
        self.sender_uuid = self.envelope.sourceUuid
        self.is_new = True  # whether the message is new (has been seen by the app)
        self.is_answered = False  # whether the question has been answered

    def validate_message_type(self, raw_message) -> str | None:
        if self.envelope.syncMessage is not None:
            if self.envelope.syncMessage.sentMessage is not None:
                if (
                    self.envelope.syncMessage.sentMessage.destinationNumber
                    == raw_message.account
                ):
                    message = self.envelope.syncMessage.sentMessage.message
                    # source_number = self.envelope.sourceNumber
                    return message

        if self.envelope.dataMessage is None:
            return
        if self.envelope.dataMessage is None:
            message = ""
        else:
            message = self.envelope.dataMessage.message
        return message


# ----------------------------------------------------------
# --------------------- Signal Connector -------------------
# ----------------------------------------------------------
class SignalConnector:
    """
    Connects to the Signal Http Rest API
    """

    def __init__(self, url=SIGNAL_URL):
        self.base_url = url
        self.account_phone_number = self.init_phone_number()
        logger.info(f"Account phone number : {self.account_phone_number}")
        self.messages = []

    def init_phone_number(self):
        """
        Return the first phone number of the signal rest API
        I.e. the first account (not recipient)
        """
        try:
            result = requests.get(os.path.join(SIGNAL_URL, "v1/accounts"))
        except HTTPError as e:
            logger.info("Could not access signal account phone number : %s", e)
            raise e
        result_json = result.json()
        if not isinstance(result_json, list):
            raise Exception(
                f"Wrong body from v1/accounts : `{result_json}`. Expected list of phone numbers."
            )
        if len(result_json) == 0:
            raise Exception(
                "No account was found on route v1/accounts. Please check the Signal API is running, and is initialized with at least one account."
            )
        if len(result_json) > 1:
            logger.info("Several numbers were found on signal, using the first.")
        return result_json[0]

    def get(self, route: str, **kwargs) -> requests.Response | None:
        """
        Makes a GET request at the given route.

        Parameters :
        ---
            - route: the route after the base url. Must NOT start with '/'
            - **kwargs: any named parameter that will be given to requests.get
                method.

        Returns:
        ---
            The requests Response if the status code is between 200 and 400
            else None.
        """
        url = os.path.join(self.base_url, route)
        try:
            result = requests.get(url, **kwargs)
        except HTTPError as e:
            logger.info("Error while accessing %s : '%s'", url, e)
            return None
        else:
            if not result:
                logger.info("Could not GET content at %s, %s", url, result.content)

            return result

    def post(self, route: str, json: dict, **kwargs) -> requests.Response | None:
        """
        Makes a POST request at the given route.

        Parameters :
        ---
            - route: the route after the base url. Must NOT start with '/'
            - json: the body of the request
            - **kwargs: any named parameter that will be given to requests.get
                method.

        Returns:
        ---
            The requests Response if the status code is between 200 and 400
            else None.
        """
        url = os.path.join(self.base_url, route)
        try:
            result = requests.post(url, json=json, **kwargs)
        except HTTPError as e:
            logger.info("Error while accessing %s : '%s'", url, e)
            return None
        else:
            if not result:
                logger.info("Could not POST content at %s, %s", url, result.content)

            return result

    def retrieve_messages(self) -> dict | None:
        """
        Uses the signal-cli-rest container to retrieve all (unread)
        messages of the account.

        Returns:
        ---
            The list of messages not already retrieved. Returns None
            if no new message was found or if request failed.
        """
        result = self.get(f"v1/receive/{self.account_phone_number}")
        if result is None:
            logger.info("Could not retrieve messages")
            return
        last_messages = result.json()
        logger.debug("Successfully retrieved %s messages", len(last_messages))
        if len(last_messages) == 0:
            return
        return last_messages

    def send_message_to_one_user(self, message, user_id: E164NumberType | UUID):
        """
        Sends a signal message to the number.

        Parameters :
            - message: the message to be send
            - user_id: a phone number, or an uuid, to who send
                the message. If phone number, must be in the format
                '+336786773870'.
        """
        body = {
            "message": message,
            "number": self.account_phone_number,
            "recipients": [str(user_id)],
            "text_mode": "styled",
        }
        url = os.path.join(SIGNAL_URL, "v2/send")
        logger.debug("Signal url %s", url)
        res = requests.post(url=url, json=body)
        if res:
            logger.info("Send message to %s", user_id)
        else:
            logger.info("Failed to send message to %s : `%s`", user_id, res.content)

    def send_message_to_group(self, message, group):
        raise NotImplementedError("Will be implemented soon")

    def send_show_typing_indicator(self, user_id):
        body = {
            "recipient": str(user_id),
        }
        url = os.path.join(
            self.base_url, "v1/typing-indicator", self.account_phone_number
        )
        logger.debug("Signal url %s", url)
        res = requests.put(url=url, json=body)
        if res:
            logger.info("Send message to %s", user_id)
        else:
            logger.info("Failed to send message to %s : `%s`", user_id, res.content)

    def send_stop_typing_indicator(self, user_id):
        body = {
            "recipient": str(user_id),
        }
        url = os.path.join(
            self.base_url, "v1/typing-indicator", self.account_phone_number
        )
        logger.debug("Signal url %s", url)
        res = requests.delete(url=url, json=body)
        if res:
            logger.info("Send message to %s", user_id)
        else:
            logger.info("Failed to send message to %s : `%s`", user_id, res.content)


class SignalMessageCollection:
    """
    Database that stores and harvest signal messages with the signal
    connector. This object is connected to ONE SignalConnector.
    It can harvest all messages, and store them in appropriate
    SignalChat objects.
    """

    def __init__(
        self,
        signal: SignalConnector,
        whitelist: list[SilikUserConfig],
    ):
        self.signal = signal
        self.all_messages: list[SignalMessage] = []
        self.contacts = self.create_contacts(whitelist)
        self.all_chats: dict[UUID, SignalChat] = {
            contact.uuid: SignalChat(contact) for contact in self.contacts
        }  # all chats with peoples

    def create_contacts(
        self,
        whitelist: list[SilikUserConfig],
    ) -> list[SignalContact]:
        """
        Get all contacts from the signal connector, and keep only the whitelisted
        ones.
        """

        req_res = self.signal.get(f"v1/contacts/{self.signal.account_phone_number}")
        if req_res is None:
            raise ValueError(
                "Could not find contacts of the account, maybe there is no declared contacts."
            )
        available_contacts_raw = req_res.json()
        contacts = TypeAdapter(list[SignalContact]).validate_python(
            available_contacts_raw
        )
        whitelisted_contacts = []
        logger.debug("Available contacts %s", contacts)
        logger.info("Whitelist %s", whitelist)
        for each_whitelisted_contact in whitelist:
            for each_contact in contacts:
                if each_whitelisted_contact.uuid is not None:
                    if each_contact.uuid == each_whitelisted_contact.uuid:
                        whitelisted_contacts.append(each_contact)
                elif each_whitelisted_contact.phone_number is not None:
                    if each_contact.number == each_whitelisted_contact.phone_number:
                        whitelisted_contacts.append(each_contact)

        logger.info("Whitelisted contacts : %s", whitelisted_contacts)
        return whitelisted_contacts

    def harvest_messages(self) -> list[SignalMessage] | None:
        last_messages = self.signal.retrieve_messages()
        if last_messages is None:
            logger.debug("No new messages to harvest")
            return
        logger.debug("New messages %s", last_messages)

        # validate message structure
        messages = TypeAdapter(list[SignalMessageModel]).validate_python(last_messages)

        # we keep only messages that are dataMessages
        data_messages: list[SignalMessage] = []
        for each_message in messages:
            try:
                each_data_message = SignalMessage(each_message)
            except ValueError:
                continue
            else:
                data_messages.append(each_data_message)

        if len(data_messages) == 0:
            return
        logger.info("Harvested %s messages", len(data_messages))
        for k, each_message in enumerate(data_messages):
            if not config.check_uuid_in_whitelist(each_message.sender_uuid):
                data_messages.pop(k)
                logger.debug(f"Skipped message from user `{each_message.sender_uuid}`")

        logger.info("Keeping %s messages", len(data_messages))
        logger.debug(
            "Message contents : %s",
            [each_data_message.message for each_data_message in data_messages],
        )
        self.all_messages += data_messages
        return data_messages

    def assign_messages_to_chat(self, messages: list[SignalMessage]):
        """
        For a list of messages, assign each one to the appropriate SignalChat
        object.
        """
        for each_message in messages:
            source_uuid = each_message.sender_uuid
            if not config.check_uuid_in_whitelist(source_uuid):
                logger.debug(
                    "User with uuid %s is not registered in whitelist, hence skipped",
                    source_uuid,
                )
                continue
            if source_uuid is None:
                logger.info(
                    "The sender of message %s don't have an uuid, and hence message is not treated.",
                    each_message,
                )
                continue
            this_chat = self.all_chats[source_uuid]
            this_chat.add_message(each_message)
            logger.debug(
                "Added message %s to chat with uuid %s", each_message, source_uuid
            )

    def harvest_and_distribute(self) -> list[SignalMessage] | None:
        """
        Combines self.harvest_messages and self.assign_messages_to_chat

        Returns :
        ---
            The list of harvested messages, or None if no message was harvested.
        """
        messages = self.harvest_messages()
        if messages is None:
            return
        self.assign_messages_to_chat(messages)
        logger.debug("Successfully harvested and assigned messages to chat")
        return messages


class SignalChat:
    """
    Stores conversations (i.e. list of messages)
    """

    def __init__(self, signal_contact: SignalContact):
        self.source = signal_contact
        self.messages: list[SignalMessage] = []

    def add_message(self, message: SignalMessage):
        if message.envelope.sourceUuid is not None and self.source.uuid is not None:
            assert message.envelope.sourceUuid == self.source.uuid, (
                f"UUID of sender {message.envelope.sourceUuid} is different from expected {self.source.uuid}"
            )

        self.messages.append(message)
