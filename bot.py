from disnake.ext import commands
from disnake import Intents
from dotenv import dotenv_values

bot = commands.Bot(command_prefix='!', help_command=None, intents=Intents.all())

bot.load_extensions('cogs')

@bot.event
async def on_ready():
    print(f'[{bot.user}] Успешная авторизация!')
    
def LoadEnv(envPath: str):
    config = dotenv_values(envPath)
    return config

from modules.web import Web
web = Web(bot)
bot.loop.create_task(web.run())

bot.i18n.load("locale/")
bot.run(LoadEnv('tokens.env').get('DISCORD'))