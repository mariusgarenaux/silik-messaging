import re
from pydantic import BaseModel, Field
import queue
import time
from typing import Annotated
from .utils import (
    config,
    SilikSignalConfig,
    get_logger,
)
from jupyter_client.manager import KernelManager

from .signal_connector import (
    SignalChat,
    SignalConnector,
    SignalMessageCollection,
)

logger = get_logger(__name__)


class SilikMessagingApp:
    """
    Application that interfaces the Signal REST API with a jupyter kernel,
    in order to use tools _via_ Signal.

    Each whitelisted phone number is assigned a SilicConversation,
    which processes the messages. Each conversation has its own
    kernel.
    """

    def __init__(
        self,
        signal_connector: SignalConnector,
        signal_database: SignalMessageCollection,
        config: SilikSignalConfig,
    ):
        self.signal = signal_connector
        self.db = signal_database
        self.config = config

        self.silic_conversations = {
            contact.uuid: SilikConversation(signal_chat=self.db.all_chats[contact.uuid])
            for contact in self.db.contacts
        }

    async def run(self):
        """
        Runs the application :
            - harvest the messages,
            - assign each message to a SignalChat,
            - call pipeline method of each SilicConversation
            - sends the answer of the pipeline
        """
        while True:
            last_messages = self.db.harvest_and_distribute()
            if last_messages is None:
                time.sleep(config.harvest_delay)
                continue
            for user_uuid, each_silic_conv in self.silic_conversations.items():
                answer = each_silic_conv.pipeline()
                if answer is None:
                    logger.debug("No answer provided, not sending.")
                    continue
                logger.info("Sending answer")
                if user_uuid not in self.config.whitelist:
                    logger.warning(
                        "User with uuid %s is not in whitelist, but a message was nearly sent to him. This could be explained by the fact that his number is whitelisted but not its uuid. Please check this user.",
                        user_uuid,
                    )
                self.signal.send_message_to_one_user(answer, user_uuid)


class ParsedUserRequest(BaseModel):
    """
    Validation model for commands and tool
    calls of user messages.
    """

    command: Annotated[
        str,
        Field(
            description="The command of the message. Either the type of the kernel (`python3`, `octave`, ...); or the label of an existing kernel. It can also be 'help', or any other command to interact with kernels"
        ),
    ]
    code: Annotated[
        str,
        Field(description="The code that will run on the kernel - at your own risk"),
    ]


class SilikConversation:
    """
    Main object, interfaces between the SignalChat objects (list of messages)
    and a jupyter kernel.

    Each number in the config whitelist has a SilicConversation attached to it, and
    hence its own KernelManager.
    """

    def __init__(
        self,
        signal_chat: SignalChat,
    ):
        r"""
        Parameters:
        ---
            - signal_chat: stores signal chat messages
        """
        self.signal_chat = signal_chat
        self.km = KernelManager(kernel_name=config.kernel_name)
        self.km.start_kernel()

        # km.shutdown_kernel()

    def parse_user_request(self, input_string: str) -> ParsedUserRequest | None:
        r"""
        _Partially generated with duck.ai - GPT-4o mini_

        Parses the raw text of the message to find a command.
            - command starts with / (e.g. /start, /help)

        Parameters :
        ---
            - input_string: a string containing the question

        Returns :
        ---
            - a ParsedUserRequest object, or None if request could not be parsed
        """
        # Matches commands starting with "/" at the beginning
        command_pattern = r"^(/[^ ]+)"
        command_match = re.match(command_pattern, input_string)
        if not command_match:
            return
        command = command_match.group(0)
        code = input_string[len(command) :].strip()  # Update remaining string
        return ParsedUserRequest(
            command=command[1:],
            code=code,
        )

    def exec_in_kernel(self, code) -> str:
        """
        Runs code in a kernel.
        """
        kc = self.km.client()
        kc.start_channels()

        # Always wait for readiness
        kc.wait_for_ready(timeout=10)

        # Execute code
        msg_id = kc.execute(code)

        # Collect outputs
        result = ""
        while True:
            try:
                msg = kc.get_iopub_msg(timeout=5)
            except queue.Empty:
                break

            if msg["parent_header"].get("msg_id") != msg_id:
                continue

            msg_type = msg["header"]["msg_type"]

            if msg_type == "execute_result":
                result = msg["content"]["data"]["text/plain"]
            elif msg_type == "error":
                result = msg["content"]["evalue"]
            elif msg_type == "status" and msg["content"]["execution_state"] == "idle":
                break

        # Clean shutdown
        kc.stop_channels()
        return result

    def pipeline(self):
        """
        Process the last message of the signal_chat object if not in chat,
        else continue the conversation.

        Returns :
        ---
            The answer of the model
        """
        try:
            if len(self.signal_chat.messages) == 0:
                return

            last_message = self.signal_chat.messages[-1]
            if last_message.is_answered:
                return

            last_message.is_answered = True
            question = self.signal_chat.messages[-1].message
            answer = self.exec_in_kernel(code=question)
            return answer

        except Exception as e:
            error = (
                f"Could not deal with message because an **error** occurred : \n'{e}'"
            )
            logger.warning(error)
            return error
