import datetime
import pprint
import asyncio
import sys
import traceback
from functools import partial
from random import shuffle


import disnake
from disnake.ext import commands

from yt_dlp import YoutubeDL
import re


URL_REG = re.compile(r'https?://(?:www\.)?.+')
YOUTUBE_VIDEO_REG = re.compile(r"(https?://)?(www\.)?youtube\.(com|nl)/watch\?v=([-\w]+)")

filters = {
    'nightcore': 'aresample=48000,asetrate=48000*1.25'
}


def utc_time():
    return datetime.datetime.now(datetime.timezone.utc)


YDL_OPTIONS = {
    'noplaylist': True,
    'nocheckcertificate': True,
    'ignoreerrors': False,
    'logtostderr': False,
    'quiet': True,
    'no_warnings': True,
    'retries': 5,
    'extract_flat': 'in_playlist',
    'cachedir': False,
    'extractor_args': {
        'youtube': {
            'skip': [
                'hls',
                'dash'
            ],
            'player_skip': [
                'js',
                'configs',
                'webpage'
            ]
        },
        'youtubetab': ['webpage']
    }
}

FFMPEG_OPTIONS = {
    'before_options': '-nostdin'
                      ' -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn'
}


def fix_characters(text: str):
    replaces = [
        ('&quot;', '"'),
        ('&amp;', '&'),
        ('(', '\u0028'),
        (')', '\u0029'),
        ('[', '【'),
        (']', '】'),
        ("  ", " "),
        ("*", '"'),
        ("_", ' '),
        ("{", "\u0028"),
        ("}", "\u0029"),
    ]
    for r in replaces:
        text = text.replace(r[0], r[1])

    return text


ytdl = YoutubeDL(YDL_OPTIONS)


def is_requester():
    def predicate(inter):
        player = inter.bot.players.get(inter.guild.id)
        if not player:
            return True
        if inter.author.guild_permissions.manage_channels:
            return True
        if inter.author.voice and not any(
                m for m in inter.author.voice.channel.members if not m.bot and m.guild_permissions.manage_channels):
            return True
        if player.current['requester'] == inter.author:
            return True

    return commands.check(predicate)


class YTDLSource(disnake.PCMVolumeTransformer):

    def __init__(self, source):
        super().__init__(source)

    @classmethod
    async def source(cls, url, *, ffmpeg_opts):
        return cls(disnake.FFmpegPCMAudio(url, **ffmpeg_opts))


class MusicPlayer:

    def __init__(self, inter: commands.Context):
        self.inter = inter
        self.bot = inter.bot
        self.queue = []
        self.current = None
        self.event = asyncio.Event()
        self.now_playing = None
        self.timeout_task = None
        self.channel: disnake.VoiceChannel = None
        self.disconnect_timeout = 180
        self.loop = False
        self.exiting = False
        self.nightcore = False
        self.fx = []
        self.no_message = False
        self.locked = False
        self.volume = 100

    async def player_timeout(self):
        await asyncio.sleep(self.disconnect_timeout)
        self.exiting = True
        self.bot.loop.create_task(self.inter.cog.destroy_player(self.inter))

    async def process_next(self):

        self.event.clear()

        if self.locked:
            return

        if self.exiting:
            return

        try:
            self.timeout_task.cancel()
        except:
            pass

        if not self.queue:
            self.timeout_task = self.bot.loop.create_task(self.player_timeout())

            embed = disnake.Embed(
                description=f"A fila está vazia...\nIrei desligar o player em {self.disconnect_timeout/60} minuto(s) caso não seja adicionada novas músicas.",
                color=12035816)
            await self.inter.channel.send(embed=embed)
            return

        await self.start_play()

    async def renew_url(self):

        info = self.queue.pop(0)

        self.current = info

        try:
            if info['formats']:
                return info
        except KeyError:
            pass

        try:
            url = info['webpage_url']
        except KeyError:
            url = info['url']

        #if (yt_url := YOUTUBE_VIDEO_REG.match(url)):
        #    url = yt_url.group()

        to_run = partial(ytdl.extract_info, url=url, download=False)
        info = await self.bot.loop.run_in_executor(None, to_run)

        return info

    def ffmpeg_after(self, e):

        if e:
            print(f"ffmpeg error: {e}")

        self.event.set()

    async def start_play(self):

        await self.bot.wait_until_ready()

        if self.exiting:
            return

        self.event.clear()

        try:
            info = await self.renew_url()
        except Exception as e:
            traceback.print_exc()
            try:
                await self.inter.channel.send(embed=disnake.Embed(
                    description=f"**Ocorreu um erro durante a reprodução da música:\n[{self.current['title']}]({self.current['webpage_url']})** ```css\n{e}\n```",
                    color=12255232()))
            except:
                pass
            self.locked = True
            await asyncio.sleep(6)
            self.locked = False
            await self.process_next()
            return

        url = ""
        for format in info['formats']:
            if format['ext'] == 'm4a':
                url = format['url']
                break
        if not url:
            url = info['formats'][0]['url']

        ffmpg_opts = dict(FFMPEG_OPTIONS)

        self.fx = []

        if self.nightcore:
            self.fx.append(filters['nightcore'])

        if self.fx:
            ffmpg_opts['options'] += (f" -af \"" + ", ".join(self.fx) + "\"")

        try:
            if self.channel != self.inter.me.voice.channel:
                self.channel = self.inter.me.voice.channel
                await self.inter.guild.voice_client.move_to(self.channel)
        except AttributeError:
            print("teste: Bot desconectado após obter download da info.")
            return

        source = await YTDLSource.source(url, ffmpeg_opts=ffmpg_opts)
        source.volume = self.volume / 100

        self.inter.guild.voice_client.play(source, after=lambda e: self.ffmpeg_after(e))

        if self.no_message:
            self.no_message = False
        else:
            try:
                embed = disnake.Embed(
                    description=f"**Tocando agora:**\n[**{info['title']}**]({info['webpage_url']})\n\n**Duração:** `{datetime.timedelta(seconds=info['duration'])}`",
                    color=12035816,
                )

                thumb = info.get('thumbnail')

                if self.loop:
                    embed.description += " **| Repetição:** `ativada`"

                if self.nightcore:
                    embed.description += " **| Nightcore:** `Ativado`"
                    

                if thumb:
                    embed.set_thumbnail(url=thumb)

                self.now_playing = await self.inter.channel.send(embed=embed)

            except Exception:
                traceback.print_exc()

        await self.event.wait()

        source.cleanup()

        if self.loop:
            self.queue.insert(0, self.current)
            self.no_message = True

        self.current = None

        await self.process_next()


class music(commands.Cog):
    def __init__(self, bot):

        if not hasattr(bot, 'players'):
            bot.players = {}

        self.bot = bot

    def get_player(self, inter):
        try:
            player = inter.bot.players[inter.guild.id]
        except KeyError:
            player = MusicPlayer(inter)
            self.bot.players[inter.guild.id] = player

        return player

    async def destroy_player(self, inter):

        inter.player.exiting = True
        inter.player.loop = False

        try:
            inter.player.timeout_task.cancel()
        except:
            pass

        del self.bot.players[inter.guild.id]

        if inter.me.voice:
            await inter.guild.voice_client.disconnect()
        elif inter.guild.voice_client:
            inter.guild.voice_client.cleanup()

    # searching the item on youtube
    async def search_yt(self, item):

        if (yt_url := YOUTUBE_VIDEO_REG.match(item)):
            item = yt_url.group()

        elif not URL_REG.match(item):
            item = f"ytsearch:{item}"

        to_run = partial(ytdl.extract_info, url=item, download=False)
        info = await self.bot.loop.run_in_executor(None, to_run)

        try:
            entries = info["entries"]
        except KeyError:
            entries = [info]

        if info["extractor_key"] == "YoutubeSearch":
            entries = entries[:1]

        tracks = []

        for t in entries:

            if not (duration:=t.get('duration')):
                continue

            url = t.get('webpage_url') or t['url']

            if not URL_REG.match(url):
                url = f"https://www.youtube.com/watch?v={url}"

            tracks.append(
                {
                    'url': url,
                    'title': fix_characters(t['title']),
                    'uploader': t['uploader'],
                    'duration': duration
                }
            )

        return tracks



    @commands.slash_command()
    async def music(self, inter: disnake.ApplicationCommandInteraction):
      
     pass
     

    @music.sub_command(name="play", description="「🎶 Minji Sound」Toca uma música do YouTube")
    async def p(
            self,
            inter: disnake.ApplicationCommandInteraction,
            query: str = commands.Param(name="input", description="Nome ou link da música")
    ):

        if not inter.author.voice:
            # if voice_channel is None:
            # you need to be connected so that the bot knows where to go
            embedvc = disnake.Embed(
                colour=12255232,  # red
                description='🚫 | Para tocar uma música, primeiro se conecte a um canal de voz.'
            )
            await inter.send(embed=embedvc)
            return

        query = query.strip("<>")

        try:
            await inter.response.defer(ephemeral=False)
            songs = await self.search_yt(query)
        except Exception as e:
            traceback.print_exc()
            embedvc = disnake.Embed(
                colour=12255232,  # red
                description=f'**Algo deu errado ao processar sua busca:**\n```css\n{repr(e)}```'
            )
            await inter.edit_original_message(embed=embedvc)
            return

        if not songs:
            embedvc = disnake.Embed(
                colour=12255232,  # red
                description=f'Não houve resultados para sua busca: **{query}**'
            )
            await inter.edit_original_message(embed=embedvc)
            return

        if not inter.player:
            inter.player = self.get_player(inter)

        player = inter.player

        vc_channel = inter.author.voice.channel

        if (size := len(songs)) > 1:
            txt = f"Wow! {size} músicas!"
        else:
            txt = f"🎶 - {songs[0]['title']}"

        for song in songs:
            song['requester'] = inter.author
            player.queue.append(song)

        embedvc = disnake.Embed(
            colour=12035816,  #minji color
            title="Fila de reprodução:",
            description=f"Pode deixar! Vou adicionar seu pedido na minha lista! 💜")

        embedvc.add_field(name="Você pediu para tocar:", value=f"{txt}", inline=True)
        embedvc.set_thumbnail(url="https://64.media.tumblr.com/435cf517e2940f7525d4c33a10d92890/5a54d42126692741-18/s400x600/ecd5ba5bb2efc6e30430bf2abf2864d68e3cbcc9.png")

        await inter.edit_original_message(embed=embedvc)

        if not inter.guild.voice_client or not inter.guild.voice_client.is_connected():
            player.channel = vc_channel
            await vc_channel.connect(timeout=None, reconnect=False)

        if not inter.guild.voice_client.is_playing() or inter.guild.voice_client.is_paused():
            await player.process_next()

    @music.sub_command(name="queue", description="「🎶 Minji Sound」Mostra as atuais músicas da fila.")
    async def q(self, inter: disnake.ApplicationCommandInteraction):

        player = inter.player

        if not player:
      
            embedvc = disnake.Embed(
                colour=12255232,
                title="Aí fica difícil, amigo!",
                description='💔 | Minji não está ativa no momento...')
            await inter.send(embed=embedvc)
            return

        if not player.queue:
            embedvc = disnake.Embed(
                colour=12255232,
                description='🚫 | Não existe músicas na fila no momento.'
            )
            await inter.send(embed=embedvc)
            return

        retval = ""

        def limit(text):
            if len(text) > 30:
                return text[:28] + "..."
            return text

        for n, i in enumerate(player.queue[:20]):
            retval += f'**{n + 1} | `{datetime.timedelta(seconds=i["duration"])}` - ** [{limit(i["title"])}]({i["url"]}) | {i["requester"].mention}\n'

        if (qsize := len(player.queue)) > 20:
            retval += f"\nE mais **{qsize - 20}** música(s)"

        embedvc = disnake.Embed(
            colour=12035816,
            description=f"{retval}"
        )
        await inter.send(embed=embedvc)

    @is_requester()
    @music.sub_command(name="skip", description="「🎶 Minji Sound」Pula a música atual que está tocando.")
    async def skip(self, inter: disnake.ApplicationCommandInteraction):

        player = inter.player

        if not player:

            embedvc = disnake.Embed(
              colour=12255232,
              title="Aí fica difícil, amigo!",
              description='💔 | Minji não está ativa no momento...'
            )
            await inter.send(embed=embedvc)
            return

        if not inter.guild.voice_client or not inter.guild.voice_client.is_playing():

            embedvc = disnake.Embed(
              colour=12255232,
              title="Aí fica difícil, amigo!",
              description='💔 | Minji não está ativa no momento...'
            )

            await inter.send(embed=embedvc)
            return

        embedvc = disnake.Embed(description="**Música pulada.**", color=12255232)

        await inter.send(embed=embedvc)
        player.loop = False
        inter.guild.voice_client.stop()

    @skip.error  # Erros para kick
    async def skip_error(self, inter: disnake.ApplicationCommandInteraction, error):
        if isinstance(error, commands.CheckFailure):
            embedvc = disnake.Embed(
                colour=12255232,
                description=f"Você deve ser dono da música adicionada ou ter a permissão de **Gerenciar canais** para pular músicas."
            )
            await inter.send(embed=embedvc)
        else:
            raise error

    @music.sub_command(name="shuffle", description="「🎶 Minji Sound」Misturar as músicas da fila, quando a fila tiver mais de três músicas.")
    async def shuffle_(self, inter: disnake.ApplicationCommandInteraction):

        player = inter.player

        embed = disnake.Embed(color=12255232)

        if not player:
              embed = disnake.Embed(
                colour=12255232,
                title="Aí fica difícil, amigo!",
                description='💔 | Minji não está ativa no momento...')

              await inter.send(embed=embed)

        if len(player.queue) < 3:
            embed.description = "A fila tem que ter no mínimo 3 músicas para ser misturada."
            await inter.send(embed=embed)
            return

        shuffle(player.queue)

        embed.description = f"Você misturou as músicas da fila."
        embed.colour =12035816
        await inter.send(embed=embed)

    @music.sub_command(description="「🎶 Minji Sound」Ativar/Desativar a repetição da música atual")
    async def repeat(self, inter: disnake.ApplicationCommandInteraction):

        player = inter.player

        embed = disnake.Embed(color=12255232)

        if not player:
            embed = disnake.Embed(
                colour=12255232,
                title="Aí fica difícil, amigo!",
                description='💔 | Minji não está ativa no momento...'
            )
            await inter.send(embed=embed)
            return

        player.loop = not player.loop

        embed.colour =12035816
        embed.description = f"**Repetição {'ativada para a música atual' if player.loop else 'desativada'}.**"

        await inter.send(embed=embed)

    @music.sub_command(description="「🎶 Minji Sound」Ativar/Desativar o efeito nightcore (Música acelerada com tom mais agudo.)")
    async def nightcore(self, inter: disnake.ApplicationCommandInteraction):

        player = inter.player

        embed = disnake.Embed(color=12255232)

        if not player:
             embed = disnake.Embed(
                colour=12255232,
                title="Aí fica difícil, amigo!",
                description='💔 | Minji não está ativa no momento...'
            )
             await inter.send(embed=embed)
             return

        player.nightcore = not player.nightcore
        player.queue.insert(0, player.current)
        player.no_message = True

        inter.guild.voice_client.stop()

        embed.description = f"**Efeito nightcore {'ativado' if player.nightcore else 'desativado'}.**"
        embed.colour =12035816

        await inter.send(embed=embed)

    @music.sub_command(description="「🎶 Minji Sound」Parar o player e me desconectar do canal de voz.")
    async def stop(self, inter: disnake.ApplicationCommandInteraction):

        embedvc = disnake.Embed(colour=12255232)

        player = inter.player

        if not player:
            embedvc.title="Aí fica difícil, amigo!"
            embedvc.description = "💔 | Minji não está ativa no momento..."
            await inter.send(embed=embedvc)
            return

        if not inter.me.voice:
            embedvc.title="Aí fica difícil, amigo!"
            embedvc.description = "💔 | Não estou conectada em um canal de voz."
            await inter.send(embed=embedvc)
            return

        if not inter.author.voice or inter.author.voice.channel != inter.me.voice.channel:
            embedvc.description = "🚫 | Você precisa estar no meu canal de voz atual para usar esse comando."
            await inter.send(embed=embedvc)
            return

        if any(m for m in inter.me.voice.channel.members if
               not m.bot and m.guild_permissions.manage_channels) and not inter.author.guild_permissions.manage_channels:
            embedvc.description = "🚫 | No momento você não tem permissão para usar esse comando."
            await inter.send(embed=embedvc)
            return

        await self.destroy_player(inter)

        embedvc.colour = 12035816
        embedvc.description = "Você parou o player"
        await inter.send(embed=embedvc)


    @commands.Cog.listener("on_voice_state_update")
    async def player_vc_disconnect(self, member: disnake.Member, before: disnake.VoiceState, after: disnake.VoiceState):

        if member.id != self.bot.user.id:
            return

        if after.channel:
            return

        player: MusicPlayer = self.bot.players.get(member.guild.id)

        if not player:
            return

        if player.exiting:
            return

        embed = disnake.Embed(description="**Desligando player por desconexão do canal.**", color=12255232)

        await player.inter.channel.send(embed=embed)

        await self.destroy_player(player.inter)


    @is_requester()
    @music.sub_command(description="「🎶 Minji Sound」Alterar volume da música")
    async def volume(
            self,
            inter: disnake.ApplicationCommandInteraction, *,
            value: int = commands.Param(name="nível", description="nível entre 5 a 100", min_value=5.0, max_value=100.0)
    ):

        vc = inter.guild.voice_client

        if not vc or not vc.is_connected():
            embedvc.title="Aí fica difícil, amigo!"
            embedvc.description = "💔 | Não estou conectada em um canal de voz."
            await inter.send(embed=embedvc)

        player = self.get_player(inter)

        if vc.source:
            vc.source.volume = value / 100

        player.volume = value / 100
        embed = disnake.Embed(description=f"**Volume alterado para {value}%**", color=12255232)
        await inter.send(embed=embed)


    async def cog_slash_command_error(self, inter: disnake.ApplicationCommandInteraction, error: Exception):

        error = getattr(error, 'original', error)

        traceback.print_exception(type(error), error, error.__traceback__, file=sys.stderr)

        if isinstance(error, commands.CommandNotFound):
            return

        embed = disnake.Embed(
            description=f"**Ocorreu um erro ao executar o comando:** ```py\n{repr(error)[:1920]}```",
            color=12255232
        )

        await inter.send(embed=embed)

    async def cog_before_slash_command_invoke(self, inter):

        inter.player = self.bot.players.get(inter.guild.id)


def setup(client):
    client.add_cog(music(client))
