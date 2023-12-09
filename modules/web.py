from disnake.ext import commands
from aiohttp import web
import os


class Web:
    def __init__(self, bot: commands.Bot):
        self.app = app = web.Application()
        path = 'temp'
        if not os.path.exists(path):
            os.makedirs(path)
        app.router.add_static('/temp/', path=path)

    async def run(self):
        runner = web.AppRunner(self.app)
        await runner.setup()
        site = web.TCPSite(runner, '0.0.0.0', 9298)
        await site.start()
