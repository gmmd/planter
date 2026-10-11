"""Publish one command menu across Telegram's language and chat scopes."""

from __future__ import annotations

import logging
from typing import Iterable

from aiogram import Bot
from aiogram.enums import ChatType
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllChatAdministrators,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
    BotCommandScopeChatMember,
    BotCommandScopeDefault,
    BotCommandScopeUnion,
    MenuButtonCommands,
    Message,
)


logger = logging.getLogger(__name__)
BOT_COMMANDS = (
    BotCommand(command="start", description="Запустить бота"),
    BotCommand(command="help", description="Справка по командам"),
    BotCommand(command="take_photo", description="Сделать фотографию"),
    BotCommand(command="water_lemon", description="Полить лимон и снять видео"),
    BotCommand(command="water_pepper", description="Полить перец и снять видео"),
    BotCommand(command="get_schedule", description="Получить новый план от AI"),
    BotCommand(command="read_schedule", description="Посмотреть сохранённый план"),
)


async def _sync_commands(
    bot: Bot, scopes: Iterable[BotCommandScopeUnion], languages: tuple[str, ...]
) -> bool:
    successful = True
    for scope in scopes:
        for language in languages:
            try:
                accepted = await bot.set_my_commands(
                    list(BOT_COMMANDS), scope=scope, language_code=language
                )
                if not accepted:
                    raise RuntimeError("Telegram did not accept the command menu")
            except Exception:
                successful = False
                logger.exception(
                    "Could not synchronize Telegram command menu: scope=%s language=%s",
                    scope.model_dump(exclude_none=True), language or "default",
                )
            else:
                logger.info(
                    "Telegram command menu synchronized: scope=%s language=%s",
                    scope.model_dump(exclude_none=True), language or "default",
                )
    return successful


async def _set_menu_button(bot: Bot, chat_id: int | None = None) -> bool:
    try:
        accepted = await bot.set_chat_menu_button(
            chat_id=chat_id, menu_button=MenuButtonCommands()
        )
        if not accepted:
            raise RuntimeError("Telegram did not accept the menu button")
    except Exception:
        logger.exception("Could not set Telegram command menu button: chat_id=%s", chat_id)
        return False
    return True


async def register_commands(bot: Bot) -> bool:
    # A language-specific or narrower scope overrides the default command list.
    # All users see the same menu; authorization remains in the handlers.
    successful = await _sync_commands(
        bot,
        (
            BotCommandScopeDefault(),
            BotCommandScopeAllPrivateChats(),
            BotCommandScopeAllGroupChats(),
            BotCommandScopeAllChatAdministrators(),
        ),
        ("", "ru", "en"),
    )
    button_set = await _set_menu_button(bot)
    return successful and button_set


async def refresh_chat_commands(message: Message) -> bool:
    """Refresh the caller's highest-priority scope, also after startup failures."""
    private = message.chat.type == ChatType.PRIVATE
    scope: BotCommandScopeUnion = BotCommandScopeChat(chat_id=message.chat.id)
    if message.chat.type in {ChatType.GROUP, ChatType.SUPERGROUP} and message.from_user is not None:
        scope = BotCommandScopeChatMember(
            chat_id=message.chat.id, user_id=message.from_user.id
        )
    raw_language = message.from_user.language_code if message.from_user else None
    language = (raw_language or "").lower().replace("_", "-").split("-", 1)[0]
    languages = ("",)
    if len(language) == 2 and language.isascii() and language.isalpha():
        languages += (language,)
    successful = await _sync_commands(message.bot, (scope,), languages)
    if private:
        button_set = await _set_menu_button(message.bot, chat_id=message.chat.id)
        return successful and button_set
    return successful
