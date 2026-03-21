"""Entry point: python -m discord_bot"""

import logging

from discord_bot.bot import AgentScribeBot
from discord_bot.config import DISCORD_BOT_TOKEN

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

AgentScribeBot().run(DISCORD_BOT_TOKEN)
