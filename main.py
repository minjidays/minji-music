import disnake
from disnake.ext import commands
import os
from keep_alive import keep_alive

intents = disnake.Intents.default()
intents.members = True

keep_alive()

testing = False

client = commands.Bot(
    command_prefix = "m!",
    case_insensitive = True,
    intents=intents,
)

client.remove_command('help')

@client.event
async def on_ready():
    print(f'Entramos como {client.user}')

    await client.change_presence(activity=disnake.Activity(type=disnake.ActivityType.listening, name="minji sound"))


for filename in os.listdir('./cogs'):
    if filename.endswith('.py'):
        client.load_extension(f'cogs.{filename[:-3]}')
        print(f"{filename} Carregado.")

TOKEN = os.environ.get("TOKEN")

if not TOKEN:
    TOKEN = 'seu token aqui, caso são use os secrets do replit ou env'

client.run(TOKEN)
