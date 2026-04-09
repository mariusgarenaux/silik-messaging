from pydantic import BaseModel, Field
import queue
import os
from typing import Annotated
from .utils import (
    config,
    SilikSignalConfig,
    SilikUserConfig,
    get_logger,
)
from jupyter_client.manager import AsyncKernelManager
from jupyter_client.blocking.client import BlockingKernelClient
from jupyter_client.asynchronous.client import AsyncKernelClient

import asyncio
from .signal_connector import (
    SignalChat,
    SignalConnector,
    SignalMessageCollection,
    SignalMessage,
    SignalContact,
)


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
                user_config=config.get_user_from_id(contact.uuid),
            )
            for contact in self.db.contacts
        ]

    async def run(self):
        """
        Runs the application :
            - harvest the messages,
            - assign each message to a SignalChat,
            - call pipeline method of each SilikConversation
        All pipelines are called in an asyncio Task Group.
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
        """
        Run each pipeline of each signal conversation.
        """
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
            await each_silic_conv.stop_kernel()
            logger.info(
                f"Kernel for `{each_silic_conv.user.uuid}` was successfully shutdown"
            )
            self.signal_connector.send_stop_typing_indicator(each_silic_conv.user.uuid)


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
        user_config: SilikUserConfig,
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
        self.config = user_config
        self.kernel_connection_file = self.config.kernel_connection_file
        self.kernel_name = self.config.kernel_name

        if self.kernel_connection_file is None:
            self.km: AsyncKernelManager = AsyncKernelManager(
                kernel_name=self.kernel_name
            )
        self.message_buffer: list[
            SignalMessage
        ] = []  # buffer used to store message, waiting that they are treated by the kernel

    async def stop_kernel(self):
        if self.kernel_connection_file is not None:
            # no need to stop kernel since it is not managed here
            return
        await self.km.shutdown_kernel()

    async def start_kernel(self):
        if self.kernel_connection_file is not None:
            # no need to start kernel
            return
        await self.km.start_kernel()
        logger.info(
            f"Started kernel of type {self.kernel_name} for user `{self.user.uuid}`"
        )

    async def run_code_on_kernel(self, code):
        """
        Runs code in a kernel, and sends the message back
        """
        if self.kernel_connection_file is not None and os.path.isfile(
            self.kernel_connection_file
        ):
            kc: AsyncKernelClient = AsyncKernelClient(
                connection_file=self.kernel_connection_file
            )
            kc.load_connection_file()
            logger.info(f"Loaded connection file : {self.kernel_connection_file}")
        else:
            kc = self.km.client()
        kc.start_channels()

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

            try:
                stdin_msg = await kc.get_stdin_msg(timeout=0.1)
            except Exception:
                stdin_msg = None

            if stdin_msg is not None:
                logger.debug(f"Message from stdin : {stdin_msg}.")
                self._allow_stdin = True
                # stdin_msg["content"]["prompt"]
                # TODO : implement input with user validation
                input_reply = kc.session.msg("input_reply", {"value": "out"})
                kc.stdin_channel.send(input_reply)
                continue

            if msg["parent_header"].get("msg_id") != msg_id:
                continue

            msg_type = msg["header"]["msg_type"]
            if msg_type in ["execute_result", "display_data"]:
                kc.stop_channels()
                logger.debug(f"Execute result : {msg['content']['data']['text/plain']}")
                self.send_msg_to_user(msg["content"]["data"]["text/plain"])
                return

            if msg_type == "stream":
                result += msg["content"]["text"]
                continue

            elif msg_type == "error":
                raise Exception(msg["content"]["evalue"])  # todo: custom exception

            elif msg_type == "status" and msg["content"]["execution_state"] == "idle":
                break

        kc.stop_channels()
        if len(result) > 0:
            self.send_msg_to_user(result)

    def send_msg_to_user(self, msg: str):
        """
        Sends a message to the user
        """
        self.signal_connector.send_message_to_one_user(msg, self.user.uuid)
        logger.info(f"Sent message {msg} to user {self.user.uuid}")

    async def pipeline(self):
        """
        Answer user messages.
        Fill a message buffer with unanswered messages,
        and send all of them sequentially to the kernel.
        The answer from the kernel are then sent back
        to the user.
        """
        try:
            # fill the message buffer
            for each_message in self.signal_chat.messages:
                if each_message.is_answered:
                    continue
                if each_message in self.message_buffer:
                    continue
                self.message_buffer.append(each_message)

            # answer all messages from the buffer
            self.signal_connector.send_show_typing_indicator(self.user.uuid)
            for each_message in self.message_buffer:
                if each_message.is_answered:
                    continue
                await self.run_code_on_kernel(code=each_message.message)
                each_message.is_answered = True

            self.message_buffer = []  # once all messages in buffer are answered,
            # the buffer is reset
            self.signal_connector.send_stop_typing_indicator(self.user.uuid)

        except Exception as e:
            error = (
                f"Could not deal with message because an **error** occurred : \n'{e}'"
            )
            logger.warning(error)
