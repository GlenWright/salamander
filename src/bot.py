#   Copyright 2020 Michael Hall
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.


from __future__ import annotations

import asyncio
import io
import logging
import re
import signal
import sys
from logging.handlers import RotatingFileHandler
from types import TracebackType
from typing import Any, Callable, Final, List, Optional, Sequence, Type, TypeVar, Union
from uuid import uuid4

try:
    import uvloop
except ImportError:
    uvloop = None

try:
    import jishaku
except ImportError:
    jishaku = None

import apsw
import discord
from discord.ext import commands, menus
from lru import LRU

from .ipc_layer import ZMQHandler
from .modlog import ModlogHandler
from .utils import MainThreadSingletonMeta, format_list, only_once, pagify

log = logging.getLogger("salamander")

BASALISK_GAZE = "basalisk.gaze"
BASALISK_OFFER = "basalisk.offer"


__all__ = ["setup_logging", "Salamander", "SalamanderContext"]


@only_once
def setup_logging():
    log = logging.getLogger("salamander")
    handler = logging.StreamHandler(sys.stdout)
    rotating_file_handler = RotatingFileHandler(
        "salamander.log", maxBytes=10000000, backupCount=5
    )
    # Log appliance use in future with aiologger.
    formatter = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        style="%",
    )
    handler.setFormatter(formatter)
    rotating_file_handler.setFormatter(formatter)
    log.addHandler(handler)
    log.addHandler(rotating_file_handler)


class SalamanderException(Exception):
    """ Base Exception for custom Exceptions """

    custom_message: str
    reset_cooldown: bool


class IncompleteInputError(SalamanderException):
    """ To be used when a command did not recieve all the inputs """

    def __init__(self, *args, reset_cooldown: bool = False, custom_message: str = ""):
        super().__init__("Incomplete user input")
        self.reset_cooldown: bool = reset_cooldown
        self.custom_message: str = custom_message


class HierarchyException(SalamanderException):
    """ For cases where invalid targetting due to hierarchy occurs """

    def __init__(self, *args, custom_message: str = ""):
        super().__init__("Hierarchy memes")
        self.custom_message: str = custom_message
        self.reset_cooldown: bool = False


class UserFeedbackError(SalamanderException):
    """ Generic error which propogates a message to the user """

    def __init__(self, *args, custom_message: str):
        super().__init__(self, custom_message)
        self.custom_message = custom_message
        self.reset_cooldown: bool = False


_PT = TypeVar("_PT", bound=str)

UNTIMELY_RESPONSE_ERROR_STR = "I'm not waiting forever for a response (exiting)."

NON_TEXT_RESPONSE_ERROR_STR = (
    "There doesn't appear to be any text in your response, "
    "please try this command again."
)

INVALID_OPTION_ERROR_FMT = (
    "That wasn't a valid option, please try this command again."
    "\n(Valid options are: {})"
)


class PreFormattedListSource(menus.ListPageSource):
    def __init__(self, data):
        super().__init__(data, per_page=1)

    async def format_page(self, menu, page):
        return page


class SalamanderContext(commands.Context):

    bot: Salamander

    @property
    def clean_prefix(self) -> str:
        repl = f"@{self.me.display_name}".replace("\\", r"\\")
        pattern = re.compile(rf"<@!?{self.me.id}>")
        return pattern.sub(repl, self.prefix)

    async def send_help(self, command=None):
        """
        An opinionated choice that ctx.send_help()
        should default to help based on the current command
        """
        command = command or self.command
        await super().send_help(command)

    async def list_menu(
        self,
        pages: List[Union[discord.Embed, str]],
        *,
        timeout: float = 180,
        alt_destination: Optional[discord.abc.Messageable] = None,
    ) -> menus.Menu:
        """
        Returns the started menu,
        a List source is made for you from strings/embeds
        provided assuming them as being already prepared.
        """
        menu = menus.MenuPages(
            source=PreFormattedListSource(pages),
            check_embeds=True,
            clear_reactions_after=True,
            timeout=timeout,
        )
        await menu.start(self, channel=alt_destination or self.channel)
        return menu

    async def prompt(
        self,
        prompt: str,
        *,
        options: Sequence[_PT],
        timeout: float,
        case_sensitive: bool = False,
        reset_cooldown_on_failure: bool = False,
        delete_on_return: bool = False,
    ) -> _PT:
        """
        Prompt for a choice, raising an error if not matching
        """

        def check(m: discord.Message) -> bool:
            return m.author.id == self.author.id and m.channel.id == self.channel.id

        #  bot.wait_for adds to internal listeners, returning an awaitable
        #  This ordering is desirable as we can ensure we are listening
        #  before asking, but have the timeout apply after we've sent
        #  (avoiding a (user perspective) inconsistent timeout)
        fut = self.bot.wait_for("message", check=check, timeout=timeout)
        sent = await self.send(prompt)
        try:
            response = await fut
        except asyncio.TimeoutError:
            raise IncompleteInputError(
                custom_message=UNTIMELY_RESPONSE_ERROR_STR,
                reset_cooldown=reset_cooldown_on_failure,
            )

        if response.content is None:
            # did they upload an image as a response?
            raise IncompleteInputError(
                custom_message=NON_TEXT_RESPONSE_ERROR_STR,
                reset_cooldown=reset_cooldown_on_failure,
            )

        content = response.content if case_sensitive else response.content.casefold()

        if content not in options:
            raise IncompleteInputError(
                custom_message=INVALID_OPTION_ERROR_FMT.format(format_list(options)),
                reset_cooldown=reset_cooldown_on_failure,
            )

        try:
            return content
        finally:
            if delete_on_return:
                try:
                    await sent.delete()
                    await response.delete()
                except Exception as exc:
                    if __debug__:
                        log.exception(
                            "Could not delete messages as intended in prompt",
                            exc_info=exc,
                        )

    async def send_paged(
        self,
        content: str,
        *,
        box: bool = False,
        prepend: Optional[str] = None,
        page_size: int = 1800,
        allowed_mentions: Optional[discord.AllowedMentions] = None,
    ):
        """ Send something paged out """

        for i, page in enumerate(pagify(content, page_size=page_size)):
            if box:
                page = f"```\n{page}\n```"
            if i == 0 and prepend:
                page = f"{prepend}\n{page}"
            await self.send(page, allowed_mentions=allowed_mentions)

    async def safe_send(self, content: str, **kwargs):
        if kwargs.pop("file", None):
            raise TypeError("Safe send is incompatible with sending a file.")
        if len(content) > 2000:
            fp = io.BytesIO(content.encode())
            return await self.send(
                file=discord.File(fp, filename="message.txt"), **kwargs
            )
        else:
            return await self.send(content, **kwargs)


_CT = TypeVar("_CT", bound=SalamanderContext)


def _prefix(
    bot: "Salamander", msg: discord.Message
) -> Callable[["Salamander", discord.Message], List[str]]:
    guild = msg.guild
    base = bot.prefix_manager.get_guild_prefixes(guild.id) if guild else ()
    return commands.when_mentioned_or(*base)(bot, msg)


class BehaviorFlags:
    """
    Contains info about which external services are expected
    and which extensions are to be loaded initially.

    (To be used by a configuration parser of some sort in the future: TODO)
    """

    def __init__(
        self,
        *,
        no_basalisk: bool = False,
        no_serpent: bool = False,
        initial_exts: Sequence[str] = (),
        about_text: Optional[str] = None,
    ):
        self.no_basalisk: bool = no_basalisk
        self.no_serpent: bool = no_serpent
        self.initial_exts: Sequence[str] = initial_exts
        self.about_text: Final[Optional[str]] = about_text

    @classmethod
    def defaults(cls):
        """
        Factory method for the defaults.

        The defaults are not guaranteed to be unchanging.

        The current defaults depend partially on if running python with -O or not.
        """

        exts = (
            "src.contrib_extensions.activitymetadata",
            "src.contrib_extensions.dice",
            "src.contrib_extensions.qotw",
            "src.extensions.mod",
            "src.extensions.meta",
            "src.extensions.cleanup",
            "src.extensions.rolemanagement",
        )

        if __debug__:
            if jishaku is not None:
                exts = exts + ("jishaku",)

        return cls(no_serpent=True, initial_exts=exts)


class PrefixManager(metaclass=MainThreadSingletonMeta):
    def __init__(self, bot: Salamander):
        self._bot: Salamander = bot
        self._cache = LRU(128)

    def get_guild_prefixes(self, guild_id: int) -> Sequence[str]:
        base = self._cache.get(guild_id, ())
        if base:
            return base

        cursor = self._bot._conn.cursor()
        res = tuple(
            pfx
            for (pfx,) in cursor.execute(
                """
                SELECT prefix FROM guild_prefixes
                WHERE guild_id=?
                ORDER BY prefix DESC
                """,
                (guild_id,),
            )
        )
        self._cache[guild_id] = res
        return res

    def add_guild_prefixes(self, guild_id: int, *prefixes: str):
        cursor = self._bot._conn.cursor()
        with self._bot._conn:

            cursor.execute(
                """
                INSERT INTO guild_settings (guild_id) VALUES (?)
                ON CONFLICT (guild_id) DO NOTHING
                """,
                (guild_id,),
            )

            cursor.executemany(
                """
                INSERT INTO guild_prefixes (guild_id, prefix)
                VALUES (?, ?)
                ON CONFLICT (guild_id, prefix) DO NOTHING
                """,
                tuple((guild_id, pfx) for pfx in prefixes),
            )

        # Extremely likely to be cached already
        try:
            del self._cache[guild_id]
        except KeyError:
            pass

    def remove_guild_prefixes(self, guild_id: int, *prefixes: str):
        cursor = self._bot._conn.cursor()
        with self._bot._conn:

            cursor.executemany(
                """
                DELETE FROM guild_prefixes WHERE guild_id=? AND prefix=?
                """,
                tuple((guild_id, pfx) for pfx in prefixes),
            )

        # Extremely likely to be cached already
        try:
            del self._cache[guild_id]
        except KeyError:
            pass


class BlockManager(metaclass=MainThreadSingletonMeta):
    def __init__(self, bot: Salamander):
        self._bot: Salamander = bot

    def guild_is_blocked(self, guild_id: int) -> bool:
        cursor = self._bot._conn.cursor()
        r = cursor.execute(
            """
            SELECT is_blocked from guild_settings
            WHERE guild_id =?
            """,
            (guild_id,),
        ).fetchone()
        if r:
            return r[0]
        return False

    def _modify_guild_block(self, val: bool, guild_id: int):
        cursor = self._bot._conn.cursor()
        cursor.execute(
            """
            INSERT INTO guild_settings (guild_id, is_blocked)
            VALUES (?, ?)
            ON CONFLICT (guild_id)
            DO UPDATE SET is_blocked=excluded.is_blocked
            """,
            (guild_id, val),
        )

    def block_guild(self, guild_id: int):
        self._modify_guild_block(True, guild_id)

    def unblock_guild(self, guild_id: int):
        self._modify_guild_block(False, guild_id)

    def user_is_blocked(self, user_id: int) -> bool:
        cursor = self._bot._conn.cursor()
        r = cursor.execute(
            """
            SELECT is_blocked from user_settings
            WHERE user_id = ?
            """,
            (user_id,),
        ).fetchone()
        if r:
            return r[0]
        return False

    def member_is_blocked(self, guild_id: int, user_id: int) -> bool:
        cursor = self._bot._conn.cursor()
        r = cursor.execute(
            """
            SELECT is_blocked from member_settings
            WHERE guild_id = ? AND user_id = ?
            """,
            (guild_id, user_id),
        ).fetchone()
        if r:
            return r[0]
        return False

    def _modify_user_block(self, val: bool, user_ids: Sequence[int]):
        cursor = self._bot._conn.cursor()
        with self._bot._conn:
            cursor.executemany(
                """
                INSERT INTO user_settings (user_id, is_blocked)
                VALUES (?, ?)
                ON CONFLICT (user_id) DO UPDATE SET
                    is_blocked=excluded.is_blocked
                """,
                tuple((uid, val) for uid in user_ids),
            )

    def _modify_member_block(self, val: bool, guild_id: int, user_ids: Sequence[int]):
        cursor = self._bot._conn.cursor()
        with self._bot._conn:
            cursor.executemany(
                """
                INSERT INTO user_settings (user_id)
                VALUES (?)
                ON CONFLICT (user_id) DO NOTHING
                """,
                tuple((uid,) for uid in user_ids),
            )
            cursor.execute(
                """
                INSERT INTO guild_settings (guild_id)
                VALUES (?)
                ON CONFLICT (guild_id) DO NOTHING
                """,
                (guild_id,),
            )
            cursor.executemany(
                """
                INSERT INTO member_settings (guild_id, user_id, is_blocked)
                VALUES (?, ?, ?)
                ON CONFLICT (guild_id, user_id) DO UPDATE SET
                    is_blocked=excluded.is_blocked
                """,
                tuple((guild_id, uid, val) for uid in user_ids),
            )

    def block_users(self, *user_ids: int):
        self._modify_user_block(True, user_ids)

    def unblock_users(self, *user_ids: int):
        self._modify_user_block(False, user_ids)

    def block_members(self, guild_id: int, *user_ids: int):
        self._modify_member_block(True, guild_id, user_ids)

    def unblock_members(self, guild_id: int, *user_ids: int):
        self._modify_member_block(False, guild_id, user_ids)


class PrivHandler(metaclass=MainThreadSingletonMeta):
    def __init__(self, bot: Salamander):
        self._bot: Salamander = bot

    def member_is_mod(self, guild_id: int, user_id: int) -> bool:
        cursor = self._bot._conn.cursor()
        r = cursor.execute(
            """
            SELECT is_mod OR is_admin
            FROM member_settings
            WHERE guild_id = ? and user_id = ?
            """,
            (guild_id, user_id),
        ).fetchone()

        return r[0] if r else False

    def member_is_admin(self, guild_id: int, user_id: int) -> bool:
        cursor = self._bot._conn.cursor()
        r = cursor.execute(
            """
            SELECT is_admin
            FROM member_settings
            WHERE guild_id = ? and user_id = ?
            """,
            (guild_id, user_id),
        ).fetchone()

        return r[0] if r else False

    def _modify_mod_status(self, val: bool, guild_id: int, user_ids: Sequence[int]):
        cursor = self._bot._conn.cursor()
        with self._bot._conn:

            cursor.executemany(
                """
                INSERT INTO user_settings (user_id) VALUES (?)
                ON CONFLICT (user_id) DO NOTHING
                """,
                tuple((uid,) for uid in user_ids),
            )
            cursor.execute(
                """
                INSERT INTO guild_settings (guild_id)
                VALUES (?)
                ON CONFLICT (guild_id) DO NOTHING
                """,
                (guild_id,),
            )
            cursor.executemany(
                """
                INSERT INTO member_settings (guild_id, user_id, is_mod)
                VALUES (?,?,?)
                ON CONFLICT (guild_id, user_id)
                DO UPDATE SET is_mod=excluded.is_mod
                """,
                tuple((guild_id, uid, val) for uid in user_ids),
            )

    def _modify_admin_status(self, val: bool, guild_id: int, user_ids: Sequence[int]):
        cursor = self._bot._conn.cursor()
        with self._bot._conn:
            cursor.executemany(
                """
                INSERT INTO user_settings (user_id)
                VALUES (?)
                ON CONFLICT (user_id) DO NOTHING
                """,
                tuple((uid,) for uid in user_ids),
            )
            cursor.execute(
                """
                INSERT INTO guild_settings (guild_id)
                VALUES (?)
                ON CONFLICT (guild_id) DO NOTHING
                """,
                (guild_id,),
            )
            cursor.executemany(
                """
                INSERT INTO member_settings (guild_id, user_id, is_admin)
                VALUES (?,?,?)
                ON CONFLICT (guild_id, user_id)
                DO UPDATE SET is_admin=excluded.is_mod
                """,
                tuple((guild_id, uid, val) for uid in user_ids),
            )

    def give_mod(self, guild_id: int, *user_ids: int):
        self._modify_mod_status(True, guild_id, user_ids)

    def remove_mod(self, guild_id: int, *user_ids: int):
        self._modify_mod_status(False, guild_id, user_ids)

    def give_admin(self, guild_id: int, *user_ids: int):
        self._modify_admin_status(True, guild_id, user_ids)

    def remove_admin(self, guild_id: int, *user_ids: int):
        self._modify_admin_status(False, guild_id, user_ids)


class EmbedHelp(commands.HelpCommand):
    def get_ending_note(self):
        return f"Use {self.clean_prefix}{self.invoked_with} [command] for help with a specific command"

    def get_command_signature(self, command):
        return f"{command.qualified_name} {command.signature}"

    async def send_bot_help(self, mapping):
        embed = discord.Embed(title="Bot Commands", color=self.context.me.color)

        embeds = []

        def add_field(embed: discord.Embed, name: str, value: str) -> discord.Embed:
            if embed.fields and len(embed.fields) > 24:
                embeds.append(embed)
                r = discord.Embed(color=self.context.me.color)
                r.add_field(name=name, value=value)
                return r
            else:
                embed.add_field(name=name, value=value)
                return embed

        async def predicate(cmd):
            try:
                return await cmd.can_run(self.context)
            except commands.CommandError:
                return False

        for cog, cog_commands in sorted(
            mapping.items(),
            key=lambda kv: kv[0].qualified_name if kv[0] else "\U0010FFFF",
        ):
            name = "No Category" if cog is None else cog.qualified_name
            filtered = await self.filter_commands(cog_commands, sort=True)
            if filtered:
                value = "\N{EN SPACE}".join(
                    [c.name for c in cog_commands if await predicate(c)]
                )
                embed = add_field(embed, name, value)

        if embed.fields:  # needed in case the very last add field rolled it over
            embeds.append(embed)

        emb_l = len(embeds)
        end_note = self.get_ending_note()
        for index, embed in enumerate(embeds, 1):
            embed.set_footer(text=f"Page {index} of {emb_l} | {end_note}")

        if embeds:

            menu = menus.MenuPages(
                source=PreFormattedListSource(embeds),
                check_embeds=True,
                clear_reactions_after=True,
            )
            await menu.start(self.context, channel=self.get_destination())

    async def send_cog_help(self, cog):
        embed = discord.Embed(
            title=f"{cog.qualified_name} Commands", colour=self.context.me.color,
        )
        if cog.description:
            embed.description = cog.description

        filtered = await self.filter_commands(cog.get_commands(), sort=True)

        embeds = []

        def add_field(embed: discord.Embed, name: str, value: str) -> discord.Embed:
            if embed.fields and len(embed.fields) > 24:
                embeds.append(embed)
                r = discord.Embed(color=self.context.me.color)
                r.add_field(name=name, value=value, inline=False)
                return r
            else:
                embed.add_field(name=name, value=value, inline=False)
                return embed

        for command in filtered:
            embed = add_field(
                embed, self.get_command_signature(command), command.short_doc or "...",
            )

        if embed.fields:  # needed in case the very last add field rolled it over
            embeds.append(embed)

        emb_l = len(embeds)
        end_note = self.get_ending_note()
        for index, embed in enumerate(embeds, 1):
            embed.set_footer(text=f"Page {index} of {emb_l} | {end_note}")

        if embeds:

            menu = menus.MenuPages(
                source=PreFormattedListSource(embeds),
                check_embeds=True,
                clear_reactions_after=True,
            )
            await menu.start(self.context, channel=self.get_destination())

    async def send_group_help(self, group):

        try:
            if not await group.can_run(self.context):
                return
        except commands.CommandError:
            return

        embed = discord.Embed(title=group.qualified_name, colour=self.context.me.color)
        if group.help:
            embed.description = group.help

        embeds = []

        def add_field(embed: discord.Embed, name: str, value: str) -> discord.Embed:
            if embed.fields and len(embed.fields) > 24:
                embeds.append(embed)
                r = discord.Embed(color=self.context.me.color)
                r.add_field(name=name, value=value, inline=False)
                return r
            else:
                embed.add_field(name=name, value=value, inline=False)
                return embed

        if isinstance(group, commands.Group):
            filtered = await self.filter_commands(group.commands, sort=True)
            for command in filtered:
                embed = add_field(
                    embed,
                    self.get_command_signature(command),
                    command.short_doc or "...",
                )

        if embed.fields:  # needed in case the very last add field rolled it over
            embeds.append(embed)

        emb_l = len(embeds)
        end_note = self.get_ending_note()
        for index, embed in enumerate(embeds, 1):
            embed.set_footer(text=f"Page {index} of {emb_l} | {end_note}")

        if embeds:

            menu = menus.MenuPages(
                source=PreFormattedListSource(embeds),
                check_embeds=True,
                clear_reactions_after=True,
            )
            await menu.start(self.context, channel=self.get_destination())

    async def send_command_help(self, command):
        try:
            if await command.can_run(self.context):
                embed = discord.Embed(
                    title=self.get_command_signature(command),
                    colour=self.context.me.color,
                )
                if command.help:
                    embed.description = command.help

                menu = menus.MenuPages(
                    source=PreFormattedListSource([embed]),
                    check_embeds=True,
                    clear_reactions_after=True,
                )
                await menu.start(self.context, channel=self.get_destination())
        except commands.CommandError:
            pass


class Salamander(commands.AutoShardedBot):
    def __init__(self, *args, **kwargs):
        self._behavior_flags: BehaviorFlags = kwargs.pop(
            "behavior", None
        ) or BehaviorFlags.defaults()
        super().__init__(
            *args,
            command_prefix=_prefix,
            description="Project Salamander",
            help_command=EmbedHelp(),
            **kwargs,
        )

        self._zmq = ZMQHandler()
        self._zmq_task: Optional[asyncio.Task] = None

        self._conn = apsw.Connection("salamander.db")

        self.modlog: ModlogHandler = ModlogHandler(self._conn)
        self.prefix_manager: PrefixManager = PrefixManager(self)
        self.block_manager: BlockManager = BlockManager(self)
        self.privlevel_manager: PrivHandler = PrivHandler(self)

        for ext in self._behavior_flags.initial_exts:
            self.load_extension(ext)

        if not self._behavior_flags.no_basalisk:
            self.load_extension("src.extensions.filter")

    async def __aenter__(self) -> "Salamander":
        if self._zmq_task is None:

            async def zmq_injest_task():
                await self.wait_until_ready()
                async with self._zmq as zmq_handler:
                    while True:
                        topic, payload = await zmq_handler.get()
                        self.dispatch("ipc_recv", topic, payload)

            self._zmq_task = asyncio.create_task(zmq_injest_task())

        return self

    async def __aexit__(
        self,
        exc_type: Optional[Type[BaseException]] = None,
        exc_value: Optional[BaseException] = None,
        traceback: Optional[TracebackType] = None,
    ):
        if self._zmq_task is not None:
            self._zmq_task.cancel()
            self._zmq_task = None

    async def on_command_error(self, ctx: SalamanderContext, exc: Exception):

        if isinstance(exc, commands.CommandNotFound):
            return
        elif isinstance(exc, commands.MissingRequiredArgument):
            await ctx.send_help()
        elif isinstance(exc, commands.NoPrivateMessage):
            await ctx.author.send("This command cannot be used in private messages.")
        elif isinstance(exc, commands.CommandInvokeError):
            original = exc.original
            if isinstance(original, SalamanderException):
                if original.reset_cooldown:
                    ctx.command.reset_cooldown(ctx)
                if original.custom_message:
                    await ctx.send_paged(original.custom_message)
            elif not isinstance(
                original, (discord.HTTPException, commands.TooManyArguments)
            ):
                # too many arguments should be handled on an individual basis
                # it requires enabling ignore_extra=False (default is True)
                # and the user facing message should be tailored to the situation.
                # HTTP exceptions should never hit this logger from a command (faulty command)
                log.exception(f"In {ctx.command.qualified_name}:", exc_info=original)
        elif isinstance(exc, commands.ArgumentParsingError):
            if exc.args and (msg := exc.args[0]):
                await ctx.send(msg)

    async def check_basalisk(self, string: str) -> bool:
        """
        Check whether or not something should be filtered

        This offloads work to the shared filtering process.

        The default is no response, it's assumed to be fine.

        This prevents other features specific to this
        component from failing over if basalisk is not in use or in a failed state.
        Status checks of other components will be handled later on.

        If anything blocks the loop for longer than the timeout,
        it's possible that false negatives can occur,
        but if anything blocks the loop for that long, there are larger issues.
        """

        if self._behavior_flags.no_basalisk:
            return False

        this_uuid = uuid4().bytes

        def matches(*args) -> bool:
            topic, (recv_uuid, *_data) = args
            return topic == BASALISK_GAZE and recv_uuid == this_uuid

        # This is an intentionally genererous timeout, won't be an issue.
        fut = self.wait_for("ipc_recv", check=matches, timeout=5)
        self.ipc_put(BASALISK_OFFER, ((this_uuid, None), string))
        try:
            await fut
        except asyncio.TimeoutError:
            return False
        else:
            return True

    def ipc_put(self, topic: str, payload: Any) -> None:
        """
        Put something in a queue to be sent via IPC
        """
        self._zmq.put(topic, payload)

    def member_is_considered_muted(self, member: discord.Member) -> bool:
        """
        Checks if a user has a mute entry in the database
        *or* has the muted role for the guild

        You should not modify the roles of members outside
        of muting/unmuting for whom this returns true
        """

        cursor = self._conn.cursor()
        guild = member.guild
        (has_mute_entry,) = cursor.execute(
            """
            SELECT EXISTS(
                SELECT 1 FROM guild_mutes
                WHERE guild_id = ? AND user_id = ?
            )
            """,
            (guild.id, member.id),
        ).fetchone()

        if has_mute_entry:
            return True

        row = cursor.execute(
            """
            SELECT mute_role FROM guild_settings WHERE guild_id = ?
            """,
            (guild.id,),
        ).fetchone()

        if row and member._roles.has(row[0]):
            return True

        return False

    async def get_context(
        self, message: discord.Message, *, cls: Type[_CT] = SalamanderContext
    ) -> _CT:
        return await super().get_context(message, cls=cls)

    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return

        await self.process_commands(message)

    async def process_commands(self, message: discord.Message):
        try:
            await asyncio.wait_for(self.wait_until_ready(), timeout=5)
        except asyncio.TimeoutError:
            return
        ctx = await self.get_context(message, cls=SalamanderContext)

        if ctx.command is None:
            return

        if self.block_manager.user_is_blocked(message.author.id):
            return

        if ctx.guild:

            if self.block_manager.member_is_blocked(ctx.guild.id, message.author.id):
                return

            if not ctx.channel.permissions_for(ctx.me).send_messages:
                if await self.is_owner(ctx.author):
                    await ctx.author.send(
                        "Hey, I don't even have send perms in that channel."
                    )
                return

        await self.invoke(ctx)

    async def close(self):
        await super().close()
        # do not remove, allows graceful disconnects.
        await asyncio.sleep(1)

    @classmethod
    def run_with_wrapping(cls, token: str, config=None):
        """
        This wraps all asyncio behavior

        Don't use this with manual control of the loop as a requirement.
        """

        setup_logging()

        if uvloop is not None:
            uvloop.install()

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        loop.add_signal_handler(signal.SIGINT, lambda: loop.stop())
        loop.add_signal_handler(signal.SIGTERM, lambda: loop.stop())

        async def runner():

            intents = discord.Intents(
                guilds=True,
                members=True,
                voice_states=True,
                guild_messages=True,
                guild_reactions=True,
                dm_messages=True,
                dm_reactions=True,
            )

            instance = cls(
                intents=intents,
                behavior=config,
                allowed_mentions=discord.AllowedMentions(
                    everyone=False, roles=False, users=False
                ),
            )

            try:
                async with instance:
                    await instance.start(token, reconnect=True)
            finally:
                if not instance.is_closed():
                    await instance.close()

        def stop_when_done(fut):
            loop.stop()

        try:
            fut = asyncio.ensure_future(runner(), loop=loop)
            fut.add_done_callback(stop_when_done)
            loop.run_forever()
        except KeyboardInterrupt:
            log.warning("Please don't shut the bot down by keyboard interrupt")
        finally:
            fut.remove_done_callback(stop_when_done)
            # allow outstanding non-discord tasks a brief moment to clean themselves up
            loop.run_until_complete(asyncio.sleep(2))

            tasks = {t for t in asyncio.all_tasks(loop) if not t.done()}
            for t in tasks:
                t.cancel()

            loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
            # impl detail of cancelation...
            loop.run_until_complete(asyncio.sleep(0.01))
            loop.run_until_complete(loop.shutdown_asyncgens())

            for task in tasks:
                try:
                    if task.exception() is not None:
                        loop.call_exception_handler(
                            {
                                "message": "Unhandled exception in task during shutdown.",
                                "exception": task.exception(),
                                "task": task,
                            }
                        )
                except (asyncio.InvalidStateError, asyncio.CancelledError):
                    pass

            loop.close()
            asyncio.set_event_loop(None)

        if not fut.cancelled():
            try:
                return fut.result()
            except KeyboardInterrupt:
                return None
