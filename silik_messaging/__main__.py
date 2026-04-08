import asyncio
from .src.utils import get_logger, config
from .src.app import SilikMessagingApp, SignalConnector, SignalMessageCollection

logger = get_logger(__name__)


async def main():
    logger.info("Starting Silik Messaging")
    signal = SignalConnector()  # connects to signal REST API
    logger.info("Listening to Signal at : %s", signal.account_phone_number)
    message_collection = SignalMessageCollection(signal, config.whitelist)  # database
    silik_app = SilikMessagingApp(
        signal_connector=signal,
        signal_database=message_collection,
        config=config,
    )  # application
    await silik_app.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Interrupted by user. Stopping.")
