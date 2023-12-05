import datetime
import asyncio
import traceback
from functools import partial
from random import shuffle
from yandex_music import ClientAsync
import yandex_music
from dotenv import dotenv_values
import disnake
from disnake.ext import commands
from disnake import Localised
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from yt_dlp import YoutubeDL
import re
from pydub import AudioSegment
import os

URL_REG = re.compile(r'https?://(?:www\.)?.+')
YOUTUBE_VIDEO_REG = re.compile(r"(https?://)?(www\.)?youtube\.(com|ru)/watch\?v=([-\w]+)")

YANDEX_URL_REG = re.compile(r'https?://(?:www\.)?music\.yandex\.(ru|by|com|uz)/.+')
YANDEX_MUSIC_PLAYLIST_REG = re.compile(r"https?://music\.yandex\.(ru|by|com|uz)/users/([^/]+)/playlists/(\d+)")
YANDEX_MUSIC_ALBUM_REG = re.compile(r"https?://music\.yandex\.(ru|by|com|uz)/album/(\d+)(/track/(\d+))?") 
YANDEX_MUSIC_TRACK_REG = re.compile(r"https?://music\.yandex\.(ru|by|com|uz)/track/(\d+)")

def LoadEnv(envPath: str):
    config = dotenv_values(envPath)
    return config

def parse_yandex_music_url(url):
    if YANDEX_MUSIC_PLAYLIST_REG.match(url):
        match = YANDEX_MUSIC_PLAYLIST_REG.match(url)
        return {"type": "playlist", "user": match.group(2), "id": match.group(3)}
    elif (album_match := YANDEX_MUSIC_ALBUM_REG.match(url)):
        return {"type": "album", "id": album_match.group(2), "track_id": album_match.group(4)}
    elif (track_match := YANDEX_MUSIC_TRACK_REG.match(url)):
        return {"type": "track", "id": track_match.group(2)}
    else:
        return {"type": None}
    
def utc_time():
    return datetime.datetime.now(datetime.timezone.utc)

async def downloadFirstYM(queue: list):
    if queue and not queue[0].get('cached'):
        loop = asyncio.get_event_loop()
        info: dict = queue.pop(0)
        track: yandex_music.Track = info.get('track')
        path = f'temp/{track.id}.ogg'
        trackSource = await track.download_bytes_async()

        def trackCompression():
            audio_data = AudioSegment.from_file(BytesIO(trackSource), format='mp3')
            normalized_audio = audio_data.normalize()
            normalized_audio.export(path, format='ogg', bitrate='64k')

        await loop.run_in_executor(ThreadPoolExecutor(), trackCompression)

        info['cached'] = True
        queue.insert(0, info)

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
yandexClient = ClientAsync(LoadEnv('tokens.env').get('YANDEXMUSIC'))

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

    def __init__(self, inter: disnake.ApplicationCommandInteraction):
        self.inter = inter
        self.bot = inter.bot
        self.queue = []
        self.current = None
        self.event = asyncio.Event()
        self.now_playing = None
        self.timeout_task = None
        self.channel: disnake.VoiceChannel = None
        self.disconnect_timeout = 600
        self.repeat = False
        self.repeat_queue = False
        self.exiting = False
        self.fx = []
        self.no_message = False
        self.locked = False
        self.volume = 100
        self.pause = False

    async def player_timeout(self):
        await asyncio.sleep(self.disconnect_timeout)
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
            if self.now_playing:
                try:
                    await self.now_playing.delete()
                except:
                    pass
            embed = disnake.Embed(
                description=f"Очередь закончилась.",
                color=disnake.Colour.purple())
            await self.inter.channel.send(embed=embed)
            return
        if self.now_playing:
            try:
                await self.now_playing.delete()
            except:
                pass
        await downloadFirstYM(self.queue)
        await self.start_play()

    async def renew_url(self):
        if not self.repeat_queue:
            info = self.queue.pop(0)
        else:
            info = self.queue.pop(0)
            self.queue.append(info)
        self.bot.loop.create_task(downloadFirstYM(self.queue))
        self.current = info
        typee = info['type']
        if typee == 'youtube':
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
        elif typee == 'yandex':
            pass
        return info, typee

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
            info, typee = await self.renew_url()
        except Exception as e:
            traceback.print_exc()
            try:
                await self.inter.channel.send(embed=disnake.Embed(
                    description=f"**Произошла ошибка во время работы плеера:\n[{self.current['title']}]({self.current['webpage_url']})** ```css\n{e}\n```",
                    color=disnake.Colour.red()))
            except:
                pass
            self.locked = True
            await asyncio.sleep(6)
            self.locked = False
            await self.process_next()
            return
        if typee == 'youtube':
            url = ""
            for format in info['formats']:
                if format['ext'] == 'm4a':
                    url = format['url']
                    break
            if not url:
                url = info['formats'][0]['url']
        elif typee == 'yandex':
            url = info['url']
        ffmpg_opts = dict(FFMPEG_OPTIONS)

        self.fx = []

        if self.fx:
            ffmpg_opts['options'] += (f" -af \"" + ", ".join(self.fx) + "\"")

        try:
            if self.channel != self.inter.me.voice.channel:
                self.channel = self.inter.me.voice.channel
                await self.inter.guild.voice_client.move_to(self.channel)
        except AttributeError:
            return
        source = await YTDLSource.source(url, ffmpeg_opts=ffmpg_opts)
        source.volume = self.volume / 100

        self.inter.guild.voice_client.play(source, after=lambda e: self.ffmpeg_after(e))

        if self.now_playing:
            try:
                await self.now_playing.delete()
            except:
                pass
            
        if self.no_message:
            self.no_message = False
        else:
            try:
                if info.get('type', 'youtube') == 'yandex':
                    emoji = self.bot.get_emoji(1180872256465936394)
                else:
                    emoji = self.bot.get_emoji(1180872765264379975)
                embed = disnake.Embed(
                    title=f"Сейчас играет:",
                    description=f"[{emoji} **{info['title']}**]({info['webpage_url']})\n\n**Длинна:** `{datetime.timedelta(seconds=info['duration'])}`",
                    color=disnake.Color.purple(),
                )

                thumb = info.get('thumbnail')
                if self.repeat:
                    embed.description += '\n**Повтор:** 🔂 `Текущий трек`'
                elif self.repeat_queue:
                    embed.description += '\n**Повтор:** 🔁 `Вся очередь`'
                else:
                    embed.description += '\n**Повтор:** `Выключен`'
                if thumb:
                    embed.set_thumbnail(url=thumb)
                embed.set_footer(text=f'В очереди {len(self.queue)} треков...')
                components = [
                    disnake.ui.Button(style=disnake.ButtonStyle.gray, custom_id='music_repeat_button', emoji='🔁'),
                    disnake.ui.Button(style=disnake.ButtonStyle.gray, custom_id='music_back_button', emoji='⬅️'),
                    disnake.ui.Button(style=disnake.ButtonStyle.gray, custom_id='music_pause_button', emoji='⏯️'),
                    disnake.ui.Button(style=disnake.ButtonStyle.gray, custom_id='music_next_track_button', emoji='➡️'),
                    disnake.ui.Button(style=disnake.ButtonStyle.gray, custom_id='music_shuffle_button', emoji='🔀'),
                    disnake.ui.Button(style=disnake.ButtonStyle.gray, custom_id='music_list_button', emoji='📄', label='Очередь'),
                    disnake.ui.Button(style=disnake.ButtonStyle.gray, custom_id='music_close_button', emoji='❌', label='Выключить')
                ]
                self.now_playing = await self.inter.channel.send(embed=embed, components=components)

            except Exception:
                traceback.print_exc()

        await self.event.wait()

        source.cleanup()

        if self.repeat:
            self.queue.insert(0, self.current)

        self.current = None

        await self.process_next()


class music(commands.Cog):
    def __init__(self, bot):

        if not hasattr(bot, 'players'):
            bot.players = {}
        self.players = {}
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
        inter.player.repeat = False

        try:
            inter.player.timeout_task.cancel()
        except:
            pass

        del self.bot.players[inter.guild.id]

        if inter.me.voice:
            await inter.guild.voice_client.disconnect()
        elif inter.guild.voice_client:
            inter.guild.voice_client.cleanup()

    async def search_ym(self, item, inter: disnake.ApplicationCommandInteraction, bot: commands.Bot):
        result = parse_yandex_music_url(item)
        type = result.get('type')
        limited = False
        ym = bot.get_emoji(1180872256465936394)
        wait = bot.get_emoji(1177997423105282110)
        if not type:
            type = 'search'
            search = await yandexClient.search(item, type_='track')
            tracks_search = search.tracks 
            if tracks_search:
                tracks = list(tracks_search.results[0])
            else:
                tracks = []
        elif type == 'album':
            track_id = result.get('track_id')
            album_id = result.get('id')
            if track_id:
                type = 'track'
                tracks = await yandexClient.tracks(track_ids=track_id)
            else:
                album = await yandexClient.albums_with_tracks(album_id=album_id)
                tracks_lists = album.volumes
                tracks = []
                for listt in tracks_lists:
                    tracks.extend(listt)
        elif type == 'track':
            track_id = result.get('id')
            tracks = await yandexClient.tracks(track_ids=track_id)
        elif type == 'playlist':
            playlist_id = result.get('id')
            user = result.get('user')
            playlist = await yandexClient.users_playlists(kind=playlist_id, user_id=user)
            tracks_low = playlist.tracks
            tracks = []
            count = 0
            embed = disnake.Embed(description=f'{ym} Сканирую плейлист {wait}', color=disnake.Color.purple())
            embed.set_footer(text='За раз можно просканировать не более 500 треков')
            await inter.edit_original_message(embed=embed)
            if tracks_low:
                for track in tracks_low:
                    count += 1
                    track = await track.fetchTrackAsync()
                    tracks.append(track)
                    if count >= 50:
                        limited = True
                        break
                    if count % 10 == 0:
                        embed = disnake.Embed(description=f'{ym} Сканирую плейлист **({count}/{playlist.track_count})** {wait}', color=disnake.Color.purple())
                        embed.set_footer(text='За раз можно просканировать не более 500 треков')
                        await inter.edit_original_message(embed=embed)
        if type != 'playlist':
            total = len(tracks)
        else:
            total = count
        data = []
        if tracks:
            count = 0
            lim = ' '
            if limited:
                lim = ' (Достигнут лимит) ' 
            description = f'{ym} {f"Обнаружено **{total}{lim}**треков" if total != 1 else "Трек найден"}!'
            embed = disnake.Embed(description=description, color=disnake.Color.purple())
            await inter.edit_original_message(embed=embed)
            for track in tracks:
                count += 1
                path = f'temp/{track.id}.ogg'
                if os.path.exists(path):
                    cached = True
                else:
                    cached = False
                result = {
                        'track': track,
                        'type': 'yandex',
                        'cached': cached,
                        'url': f'http://localhost:9298/{path}',
                        'title': f'{", ".join(track.artists_name())} - {track.title}',
                        'uploader': None,
                        'duration': int(track.duration_ms / 1000),
                        'webpage_url': f'https://music.yandex.ru/track/{track.id}/',
                        'thumbnail': "https://" + track.cover_uri.replace("%%", "200x200")
                    }
                data.append(result)
                await downloadFirstYM(data)

        return data

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

        data = []

        for t in entries:

            if not (duration:=t.get('duration')):
                continue

            url = t.get('webpage_url') or t['url']

            if not URL_REG.match(url):
                url = f"https://www.youtube.com/watch?v={url}"

            data.append(
                {
                    'type': 'youtube',
                    'url': url,
                    'title': fix_characters(t['title']),
                    'uploader': t['uploader'],
                    'duration': duration,
                    'webpage_url': url
                }
            )
        return data



    @commands.slash_command(name=Localised('music', key='MUSIC_NAME'))
    async def music(self, inter: disnake.ApplicationCommandInteraction):
     pass
     

    @music.sub_command(name=Localised('play', key="PLAY_NAME"), description="Включить аудио с ютуба или яндекс музыки в текущем голосовом канале")
    async def p(
            self,
            inter: disnake.ApplicationCommandInteraction,
            query: str = commands.Param(name=Localised('query', key="QUERY_NAME"), description="Название или ссылка"),
            search: str = commands.Param(name=Localised('platform', key="PLATFORM_NAME"), description="Где искать?", choices=['Yandex Music', 'YouTube'])
    ):

        if not inter.author.voice:
            embedvc = disnake.Embed(
                colour=disnake.Color.red(), 
                description='⚠️ Чтобы воспроизвести музыку, сначала подключитесь к голосовому каналу.'
            )
            await inter.send(embed=embedvc, ephemeral=True)
            return

        if not inter.player:
            inter.player = self.get_player(inter)
        player = inter.player
        self.players[inter.guild.id] = player
        vc_channel = inter.author.voice.channel

        if not inter.guild.voice_client or not inter.guild.voice_client.is_connected():
            player.channel = vc_channel
            await vc_channel.connect(timeout=None, reconnect=False)
        if not inter.author.voice or inter.author.voice.channel != inter.me.voice.channel:
            embedvc = disnake.Embed(
                colour=disnake.Colour.red(),
                description = "⚠️ Вы должны находиться в одном голосовом канале с плеером, что бы включать треки"
            )
            await inter.send(embed=embedvc, ephemeral=True)
            return

        query = query.strip("<>")

        try:
            wait = self.bot.get_emoji(1177997423105282110)
            embed = disnake.Embed(description=f'Подготовка плеера {wait}', color=disnake.Color.purple())
            await inter.response.send_message(embed=embed)
            if search == 'YouTube':
                emoji = self.bot.get_emoji(1180872765264379975)
                embed = disnake.Embed(description=f'{emoji} Получении аудиодорожек с видео {wait}', color=disnake.Color.purple())
                await inter.edit_original_message(embed=embed)
                songs = await self.search_yt(query)
            elif search == 'Yandex Music':
                emoji = self.bot.get_emoji(1180872256465936394)
                songs = await self.search_ym(query, inter, self.bot)
        except Exception as e:
            traceback.print_exc()
            embedvc = disnake.Embed(
                colour=disnake.Color.red(),
                description=f'**⚠️ Что-то пошло не так при обработке вашего запроса:**\n```css\n{repr(e)}```'
            )
            await inter.edit_original_message(embed=embedvc)
            return

        if not songs:
            embedvc = disnake.Embed(
                colour=disnake.Color.red(),
                description=f'⚠️ Нет результатов для вашего запроса: {emoji} **{query}**'
            )
            await inter.edit_original_message(embed=embedvc)
            return


        if (size := len(songs)) > 1:
            txt = f" {emoji} **{size}** треков!"
        else:
            txt = f": **[{emoji} {songs[0]['title']}]({songs[0]['webpage_url']})**"

        for song in songs:
            song['requester'] = inter.author
            player.queue.append(song)

        embedvc = disnake.Embed(
            colour=disnake.Color.purple(), 
            description=f"✅ В очередь добавлено{txt}")
        
        if player.pause:
            inter.guild.voice_client.resume()
            player.pause = False

        await inter.edit_original_message(embed=embedvc)
        
        if not inter.guild.voice_client.is_playing() or inter.guild.voice_client.is_paused():
            await player.process_next()

    @music.sub_command(name=Localised('queue', key='QUEUE_NAME'), description="Список треков в очереди.")
    async def q(self, inter: disnake.ApplicationCommandInteraction):

        player = self.players.get(inter.guild.id)

        if not player:
            embedvc = disnake.Embed(
                colour=disnake.Colour.red(),
                description='⚠️ Плеер не активен в данный момент'
            )
            await inter.send(embed=embedvc, ephemeral=True)
            return

        if not player.queue:
            embedvc = disnake.Embed(
                colour=disnake.Colour.purple(),
                description='😟 Нет следующих треков'
            )
            await inter.send(embed=embedvc, ephemeral=True)
            return

        retval = ""

        def limit(text):
            if len(text) > 30:
                return text[:28] + "..."
            return text

        for n, i in enumerate(player.queue[:20]):
            if i['type'] == 'yandex':
                emoji = self.bot.get_emoji(1180872256465936394)
            elif i['type'] == 'youtube':
                emoji = self.bot.get_emoji(1180872765264379975)
            retval += f'**{n + 1} | {emoji} | `{datetime.timedelta(seconds=i["duration"])}` - ** [{limit(i["title"])}]({i["url"]}) | {i["requester"].mention}\n'

        if (qsize := len(player.queue)) > 20:
            retval += f"\nИ ещё **{qsize - 20}**..."

        embedvc = disnake.Embed(
            colour=disnake.Color.purple(),
            description=f"{retval}"
        )
        await inter.send(embed=embedvc)

    @music.sub_command(name=Localised('back', key='BACK_NAME'), description="Проиграть трек с начала")
    async def back(self, inter: disnake.ApplicationCommandInteraction):

        player = self.players.get(inter.guild.id)

        if not player:
            embedvc = disnake.Embed(
                colour=disnake.Colour.red(),
                description='⚠️ Плеер не активен в данный момент'
            )
            await inter.send(embed=embedvc, ephemeral=True)
            return
        
        if not inter.author.voice or inter.author.voice.channel != inter.me.voice.channel:
            embedvc = disnake.Embed(
                colour=disnake.Colour.red(),
                description = "⚠️ Вы должны находиться в одном голосовом канале с плеером, что бы проиграть трек сначала"
            )
            await inter.send(embed=embedvc, ephemeral=True)
            return
        
        player.queue.insert(0, player.current)
        inter.guild.voice_client.stop()

        embedvc = disnake.Embed(
                colour=disnake.Colour.purple(),
                description = f'✅ Трек запущен сначала'
            )
        
        await inter.send(embed=embedvc)

    @music.sub_command(name=Localised('pause', key='PAUSE_NAME'), description="Поставить плеер на паузу или снять паузу")
    async def pause(self, inter: disnake.ApplicationCommandInteraction):

        player = self.players.get(inter.guild.id)

        if not player:
            embedvc = disnake.Embed(
                colour=disnake.Colour.red(),
                description='⚠️ Плеер не активен в данный момент'
            )
            await inter.send(embed=embedvc, ephemeral=True)
            return
        
        if not inter.author.voice or inter.author.voice.channel != inter.me.voice.channel:
            embedvc = disnake.Embed(
                colour=disnake.Colour.red(),
                description = "⚠️ Вы должны находиться в одном голосовом канале с плеером, что бы ставить плеер на паузу"
            )
            await inter.send(embed=embedvc, ephemeral=True)
            return
        
        player.pause = not player.pause
        if player.pause:
            inter.guild.voice_client.pause()
        else:
            inter.guild.voice_client.resume()

        embedvc = disnake.Embed(
                colour=disnake.Colour.purple(),
                description = f'✅ {"Плеер поставлен на паузу" if player.pause else "Работа плеера возобновлена"}'
            )
        
        await inter.send(embed=embedvc)


    @music.sub_command(name=Localised('skip', key='SKIP_NAME'), description="Пропустить трек")
    async def skip(self, inter: disnake.ApplicationCommandInteraction):

        player = inter.player

        if not player:
            embedvc = disnake.Embed(
                colour=disnake.Colour.red(),
                description='⚠️ Плеер не активен в данный момент'
            )
            await inter.send(embed=embedvc, ephemeral=True)
            return
        
        if not inter.author.voice or inter.author.voice.channel != inter.me.voice.channel:
            embedvc = disnake.Embed(
                colour=disnake.Colour.red(),
                description = "⚠️ Вы должны находиться в одном голосовом канале с плеером, что бы пропускать треки"
            )
            await inter.send(embed=embedvc, ephemeral=True)
            return
        
        if not inter.guild.voice_client or not inter.guild.voice_client.is_playing():

            embedvc = disnake.Embed(
                colour=disnake.Colour.red(),
                description='⚠️ Плеер не активен в данный момент'
            )

            await inter.send(embed=embedvc, ephemeral=True)
            return

        player.repeat = False
        inter.guild.voice_client.stop()

        embedvc = disnake.Embed(description="**✅ Трек пропущен**", color=disnake.Colour.purple())

        await inter.send(embed=embedvc)

    @music.sub_command(name=Localised('shuffle', key="SHUFFLE_NAME"), description="Перемешать треки в очереди.")
    async def shuffle_(self, inter: disnake.ApplicationCommandInteraction):

        player = inter.player


        if not player:
            embedvc = disnake.Embed(
                colour=disnake.Colour.red(),
                description='⚠️ Плеер не активен в данный момент'
            )
            await inter.send(embed=embedvc, ephemeral=True)
            return
        
        if not inter.author.voice or inter.author.voice.channel != inter.me.voice.channel:
            embedvc = disnake.Embed(
                colour=disnake.Colour.red(),
                description = "⚠️ Вы должны находиться в одном голосовом канале с плеером, что бы перемешивать треки"
            )
            await inter.send(embed=embedvc, ephemeral=True)
            return
        
        if len(player.queue) < 3:
            embedvc = disnake.Embed(
                colour=disnake.Colour.red(),
                description = "⚠️ Необходимо хотя бы 3 трека в очереди, что бы её переемешать!"
            )
            await inter.send(embed=embedvc, ephemeral=True)
            return

        shuffle(player.queue)
        embedvc = disnake.Embed(
            description = f"✅ Очередь перемешана",
            colour = disnake.Colour.purple()
        )
        await inter.send(embed=embedvc)
        await downloadFirstYM(player.queue)

    @music.sub_command(name=Localised('repeat', key='REPEAT_NAME'), description="Переключение режима повтора трека")
    async def repeat(self, inter: disnake.ApplicationCommandInteraction,
                    mode: str = commands.Param(name=Localised('mode', key="MODE_NAME"), description="Режим повтора", choices=['Один трек', 'Очередь', 'Отключен']),
                    ):

        player = inter.player

        if not player:
            embedvc = disnake.Embed(
                colour=disnake.Colour.red(),
                description='⚠️ Плеер не активен в данный момент'
            )
            await inter.send(embed=embedvc, ephemeral=True)
            return
        
        if not inter.author.voice or inter.author.voice.channel != inter.me.voice.channel:
            embedvc = disnake.Embed(
                colour=disnake.Colour.red(),
                description = "⚠️ Вы должны находиться в одном голосовом канале с плеером, что бы переключать состояиние плеера"
            )
            await inter.send(embed=embedvc, ephemeral=True)
            return
        
        if mode == 'Один трек':
            player.repeat_queue = False
            player.repeat = True
            embedvc = disnake.Embed(
                colour = disnake.Color.purple(),
                description = f"🔂 **Теперь плеер будет повторять текущий трек.**"
            )
            now = player.current
            if now:
                if now in player.queue:
                    player.queue.remove(now)

        if mode == 'Очередь':
            player.repeat = False
            player.repeat_queue = True
            embedvc = disnake.Embed(
                colour = disnake.Color.purple(),
                description = f"🔁 **Теперь плеер будет повторять текущую очередь.**"
            )
            now = player.current
            if now:
                if now not in player.queue:
                    player.queue.append(now)
        else:
            player.repeat_queue = False
            player.repeat = False
            embedvc = disnake.Embed(
                colour = disnake.Color.purple(),
                description = f"☑️ **Повтор выключен**"
            )   
            now = player.current
            if now:
                if now in player.queue:
                    player.queue.remove(now)
        await inter.send(embed=embedvc)

    @music.sub_command(name=Localised('stop', key='STOP_NAME'), description="Отключить плеер от голосового канала.")
    async def stop(self, inter: disnake.ApplicationCommandInteraction):


        player = inter.player

        if not player:
            embedvc = disnake.Embed(
                colour=disnake.Colour.red(),
                description='⚠️ Плеер не активен в данный момент'
            )
            await inter.send(embed=embedvc, ephemeral=True)
            return

        if not inter.me.voice:
            embedvc = disnake.Embed(
                colour=disnake.Colour.red(),
                description = "⚠️ Плеер не активен в данный момент"
            )
            await inter.send(embed=embedvc, ephemeral=True)
            return

        if not inter.author.voice or inter.author.voice.channel != inter.me.voice.channel:
            embedvc = disnake.Embed(
                colour=disnake.Colour.red(),
                description = "⚠️ Вы должны находиться в одном голосовом канале с плеером, что бы отключить его"
            )
            await inter.send(embed=embedvc, ephemeral=True)
            return

        if any(m for m in inter.me.voice.channel.members if
               not m.bot and m.guild_permissions.manage_channels) and not inter.author.guild_permissions.manage_channels:
            embedvc.description = "⚠️ Вы не можете отключить плеер"
            await inter.send(embed=embedvc, ephemeral=True)
            return

        if player.now_playing:
            try:
                await player.now_playing.delete()
            except:
                pass

        await self.destroy_player(inter)
        embedvc = disnake.Embed(
            colour = disnake.Color.purple(),
            description = "✅ Плеер отключен"
        )
        await inter.send(embed=embedvc)

    @commands.Cog.listener("on_button_click")
    async def player_buttons(self, inter: disnake.MessageInteraction):
        if inter.component.custom_id not in ('music_repeat_button', 'music_next_track_button', 'music_shuffle_button', 'music_close_button', 'music_list_button', 'music_pause_button', 'music_back_button'):
            return
        player = self.players.get(inter.guild.id)
        inter.player = player
        if not player or not inter.me.voice:
            embedvc = disnake.Embed(
                colour=disnake.Colour.red(),
                description='⚠️ Плеер не активен в данный момент'
            )
            await inter.send(embed=embedvc, ephemeral=True)
            return
        
        if inter.component.custom_id == 'music_list_button':
            if not player.queue:
                embedvc = disnake.Embed(
                    colour=disnake.Colour.purple(),
                    description='😟 Нет следующих треков'
                )
                await inter.send(embed=embedvc, ephemeral=True)
                return

            retval = ""

            def limit(text):
                if len(text) > 30:
                    return text[:28] + "..."
                return text

            for n, i in enumerate(player.queue[:20]):
                if i['type'] == 'yandex':
                    emoji = self.bot.get_emoji(1180872256465936394)
                elif i['type'] == 'youtube':
                    emoji = self.bot.get_emoji(1180872765264379975)
                retval += f'**{n + 1} | {emoji} | `{datetime.timedelta(seconds=i["duration"])}` - ** [{limit(i["title"])}]({i["url"]}) | {i["requester"].mention}\n'

            if (qsize := len(player.queue)) > 20:
                retval += f"\nИ ещё **{qsize - 20}**..."

            embedvc = disnake.Embed(
                colour=disnake.Color.purple(),
                description=f"{retval}"
            )
            await inter.send(embed=embedvc, ephemeral=True)

        elif inter.component.custom_id == 'music_next_track_button':
            if not inter.author.voice or inter.author.voice.channel != inter.me.voice.channel:
                embedvc = disnake.Embed(
                    colour=disnake.Colour.red(),
                    description = "⚠️ Вы должны находиться в одном голосовом канале с плеером, что бы пропускать треки"
                )
                await inter.send(embed=embedvc, ephemeral=True)
                return
            
            if not inter.guild.voice_client or not inter.guild.voice_client.is_playing():

                embedvc = disnake.Embed(
                    colour=disnake.Colour.red(),
                    description='⚠️ Плеер не активен в данный момент'
                )

                await inter.send(embed=embedvc, ephemeral=True)
                return

            embedvc = disnake.Embed(description="**✅ Трек пропущен**", color=disnake.Colour.purple())

            player.repeat = False
            inter.guild.voice_client.stop()

            await inter.send(embed=embedvc, ephemeral=True)
            
        elif inter.component.custom_id == 'music_shuffle_button':
            if not inter.author.voice or inter.author.voice.channel != inter.me.voice.channel:
                embedvc = disnake.Embed(
                    colour=disnake.Colour.red(),
                    description = "⚠️ Вы должны находиться в одном голосовом канале с плеером, что бы перемешивать треки"
                )
                await inter.send(embed=embedvc, ephemeral=True)
                return
            
            if len(player.queue) < 3:
                embedvc = disnake.Embed(
                    colour=disnake.Colour.red(),
                    description = "⚠️ Необходимо хотя бы 3 трека в очереди, что бы её переемешать!"
                )
                await inter.send(embed=embedvc, ephemeral=True)
                return

            shuffle(player.queue)
            embedvc = disnake.Embed(
                description = f"✅ Очередь перемешана",
                colour = disnake.Colour.purple()
            )
            await inter.send(embed=embedvc, ephemeral=True)
            await downloadFirstYM(player.queue)

        elif inter.component.custom_id == 'music_repeat_button':
            if not inter.author.voice or inter.author.voice.channel != inter.me.voice.channel:
                embedvc = disnake.Embed(
                    colour=disnake.Colour.red(),
                    description = "⚠️ Вы должны находиться в одном голосовом канале с плеером, что бы переключать состояиние плеера"
                )
                await inter.send(embed=embedvc, ephemeral=True)
                return
            embedvc = disnake.Embed(colour=disnake.Color.purple())

            if not player.repeat and not player.repeat_queue:
                player.repeat_queue = True
                embedvc.description = f"**🔁 Теперь плеер будет повторять всю очередь.**"
                now = player.current
                if now:
                    if now not in player.queue:
                        player.queue.append(now)
            elif player.repeat_queue:
                player.repeat_queue = False
                player.repeat = True
                embedvc.description = f"**🔂 Теперь плеер будет повторять только текущий трек.**"
                now = player.current
                if now:
                    if now in player.queue:
                        player.queue.remove(now)
            else:
                player.repeat = False
                embedvc.description = f'**☑️ Повторение отключено. Плеер больше не будет повторять треки.**'
                now = player.current
                if now:
                    if now in player.queue:
                        player.queue.remove(now)

            await inter.send(embed=embedvc, ephemeral=True)

        elif inter.component.custom_id == 'music_close_button':
            if not inter.me.voice:
                embedvc = disnake.Embed(
                    colour=disnake.Colour.red(),
                    description = "⚠️ Плеер не активен в данный момент"
                )
                await inter.send(embed=embedvc, ephemeral=True)
                return

            if not inter.author.voice or inter.author.voice.channel != inter.me.voice.channel:
                embedvc = disnake.Embed(
                    colour=disnake.Colour.red(),
                    description = "⚠️ Вы должны находиться в одном голосовом канале с плеером, что бы отключить его"
                )
                await inter.send(embed=embedvc, ephemeral=True)
                return

            if any(m for m in inter.me.voice.channel.members if
                not m.bot and m.guild_permissions.manage_channels) and not inter.author.guild_permissions.manage_channels:
                embedvc.description = "⚠️ Вы не можете отключить плеер"
                await inter.send(embed=embedvc, ephemeral=True)
                return
            if player.now_playing:
                try:
                    await player.now_playing.delete()
                except:
                    pass
            await self.destroy_player(inter)
            embedvc = disnake.Embed(
                colour = disnake.Color.purple(),
                description = "✅ Плеер отключен"
            )
            await inter.send(embed=embedvc, ephemeral=True)

        elif inter.component.custom_id == 'music_back_button':
            if not inter.author.voice or inter.author.voice.channel != inter.me.voice.channel:
                embedvc = disnake.Embed(
                    colour=disnake.Colour.red(),
                    description = "⚠️ Вы должны находиться в одном голосовом канале с плеером, что бы ставить начать трек сначала"
                )
                await inter.send(embed=embedvc, ephemeral=True)
                return
            
            player.queue.insert(0, player.current)
            inter.guild.voice_client.stop()
            if len(player.queue) > 1 and player.queue[-1] == player.current:
                player.queue.pop(-1)

            embedvc = disnake.Embed(
                    colour=disnake.Colour.purple(),
                    description = f'✅ Трек запущен сначала'
                )
            
            await inter.send(embed=embedvc, ephemeral=True)
        
        elif inter.component.custom_id == 'music_pause_button':
            if not inter.author.voice or inter.author.voice.channel != inter.me.voice.channel:
                embedvc = disnake.Embed(
                    colour=disnake.Colour.red(),
                    description = "⚠️ Вы должны находиться в одном голосовом канале с плеером, что бы ставить плеер на паузу"
                )
                await inter.send(embed=embedvc, ephemeral=True)
                return
            
            player.pause = not player.pause
            if player.pause:
                inter.guild.voice_client.pause()
            else:
                inter.guild.voice_client.resume()

            embedvc = disnake.Embed(
                    colour=disnake.Colour.purple(),
                    description = f'✅ {"Плеер поставлен на паузу" if player.pause else "Работа плеера возобновлена"}'
                )
            
            await inter.send(embed=embedvc, ephemeral=True)
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

        embed = disnake.Embed(description="**Меня исключили из голосового канала! Плеер отключён 😟**", color=disnake.Color.red())

        if player.now_playing:
            try:
                await player.now_playing.delete()
            except:
                pass
        await player.inter.channel.send(embed=embed)
        await player.inter.guild.voice_client.stop()
        await self.destroy_player(player.inter)

    async def cog_before_slash_command_invoke(self, inter):

        inter.player = self.bot.players.get(inter.guild.id)


def setup(client):
    client.add_cog(music(client))