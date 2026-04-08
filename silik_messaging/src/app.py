from pydantic import BaseModel, Field
import queue
import random
from typing import Annotated
from .utils import (
    config,
    SilikSignalConfig,
    get_logger,
)
from jupyter_client.manager import AsyncKernelManager
import asyncio
from .signal_connector import (
    SignalChat,
    SignalConnector,
    SignalMessageCollection,
    SignalMessage,
    SignalContact,
)


class TerminateTaskGroup(Exception): ...


logger = get_logger(__name__)


class SilikMessagingApp:
    """
    Application that interfaces the Signal REST API with a jupyter kernel,
    in order to use tools _via_ Signal.

    Each whitelisted phone number is assigned a SilikConversation,
    which processes the messages. Each conversation has its own
    kernel.
    """

    def __init__(
        self,
        signal_connector: SignalConnector,
        signal_database: SignalMessageCollection,
        config: SilikSignalConfig,
    ):
        self.signal_connector = signal_connector
        self.db = signal_database
        self.config = config
        self.silik_conversations = [
            SilikConversation(
                signal_chat=self.db.all_chats[contact.uuid],
                user=contact,
                signal_connector=self.signal_connector,
                kernel_name=config.get_user_from_id(contact.uuid).kernel_name,
            )
            for contact in self.db.contacts
        ]

    async def run(self):
        """
        Runs the application :
            - harvest the messages,
            - assign each message to a SignalChat,
            - call pipeline method of each SilikConversation
        """
        try:
            for each_conversation in self.silik_conversations:
                await each_conversation.start_kernel()

            while True:
                last_messages = self.db.harvest_and_distribute()
                if last_messages is None:
                    await asyncio.sleep(config.harvest_delay)
                    continue
                await self.all_pipelines()

        except asyncio.CancelledError:
            logger.info("Cancelling main task. Shutting down kernels.")
            await self.graceful_shutdown()
            raise
        except Exception as e:
            logger.warning(f"Exception during main loop : {e}")
            await self.graceful_shutdown()
        finally:
            logger.info("Closing the application")

    async def all_pipelines(self):
        try:
            async with asyncio.TaskGroup() as tg:
                # sending all user requests to pipelines
                for each_silic_conv in self.silik_conversations:
                    tg.create_task(each_silic_conv.pipeline())
        except* Exception:
            logger.info("Catched all exceptions of all pipelines. Starting a new loop.")

    async def graceful_shutdown(self):
        """
        Shutdown each kernel of each conversation.
        """
        for each_silic_conv in self.silik_conversations:
            await each_silic_conv.km.shutdown_kernel()
            logger.info(
                f"Kernel for `{each_silic_conv.user.uuid}` was successfully shutdown"
            )


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

    Each number in the config whitelist has a SilikConversation attached to it, and
    hence its own KernelManager.
    """

    def __init__(
        self,
        signal_chat: SignalChat,
        user: SignalContact,
        signal_connector: SignalConnector,
        kernel_name: str,
    ):
        r"""
        Parameters:
        ---
            - signal_chat: stores signal chat messages
        """
        try:
            config.get_user_from_id(user.uuid)
        except KeyError:
            raise ValueError(
                f"User with uuid : `{user.uuid}` is not in the whitelist. Hence, no SilikConversation should be created for this user."
            )

        self.signal_chat = signal_chat
        self.signal_connector = signal_connector
        self.user = user
        self.km: AsyncKernelManager = AsyncKernelManager(kernel_name=config.kernel_name)
        self.message_buffer: list[
            SignalMessage
        ] = []  # buffer used to store message, waiting that they are treated by the kernel

    async def start_kernel(self):
        await self.km.start_kernel()
        logger.info(f"Started kernel for user `{self.user.uuid}`")

    async def run_code_on_kernel(self, code) -> str:
        """
        Runs code in a kernel, and returns the output
        """
        kc = self.km.client()
        kc.start_channels()

        # Always wait for readiness
        await kc.wait_for_ready(timeout=10)

        # Execute code
        msg_id = kc.execute(code)

        # Collect outputs
        result = ""
        while True:
            try:
                msg = await kc.get_iopub_msg(timeout=5)
            except queue.Empty:
                logger.debug("Could not get IOPub MSG")
                break

            if msg["parent_header"].get("msg_id") != msg_id:
                continue

            msg_type = msg["header"]["msg_type"]

            if msg_type == "execute_result":
                result = msg["content"]["data"]["text/plain"]
            elif msg_type == "display_data":
                result = msg["content"]["data"]["text/plain"]
            elif msg_type == "error":
                result = msg["content"]["evalue"]
            elif msg_type == "status" and msg["content"]["execution_state"] == "idle":
                break

        kc.stop_channels()
        return result

    def send_msg_to_user(self, msg: str):
        """
        Sends a message to the user
        """
        self.signal_connector.send_message_to_one_user(msg, self.user.uuid)

    async def pipeline(self):
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

            # fill the message buffer
            for each_message in self.signal_chat.messages:
                if each_message.is_answered:
                    continue
                if each_message in self.message_buffer:
                    continue
                self.message_buffer.append(each_message)

            # answer all messages from the buffer
            for each_message in self.message_buffer:
                if each_message.is_answered:
                    # should not happened, but in case
                    return
                each_message.is_answered = True
                answer = await self.run_code_on_kernel(code=each_message.message)
                self.send_msg_to_user(answer)
                logger.info(f"Sent message {answer} to user {self.user.uuid}")
        except Exception as e:
            error = (
                f"Could not deal with message because an **error** occurred : \n'{e}'"
            )
            logger.warning(error)
            self.send_msg_to_user(error)
