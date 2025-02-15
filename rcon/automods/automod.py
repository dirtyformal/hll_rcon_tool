import logging
import time
from threading import Timer
from typing import List

from pydantic import HttpUrl
from redis.client import Redis

import rcon.game_logs
from rcon.automods.level_thresholds import LevelThresholdsAutomod
from rcon.automods.models import ActionMethod, PunishPlayer, PunitionsToApply
from rcon.automods.no_leader import NoLeaderAutomod
from rcon.automods.no_solotank import NoSoloTankAutomod
from rcon.automods.seeding_rules import SeedingRulesAutomod
from rcon.cache_utils import get_redis_client
from rcon.commands import CommandFailedError, HLLServerError
from rcon.discord import send_to_discord_audit
from rcon.hooks import inject_player_ids
from rcon.logs.loop import on_kill, on_connected
from rcon.rcon import Rcon, get_rcon
from rcon.types import GetDetailedPlayer, StructuredLogLineType
from rcon.user_config.auto_mod_level import AutoModLevelUserConfig
from rcon.user_config.auto_mod_no_leader import AutoModNoLeaderUserConfig
from rcon.user_config.auto_mod_seeding import AutoModSeedingUserConfig
from rcon.user_config.auto_mod_solo_tank import AutoModNoSoloTankUserConfig

logger = logging.getLogger(__name__)
first_run_done_key = "first_run_done"


def get_punitions_to_apply(rcon, moderators) -> PunitionsToApply:
    logger.debug("Getting team info")
    team_view = rcon.get_team_view()
    gamestate = rcon.get_gamestate()
    punitions_to_apply = PunitionsToApply()

    for team in ["allies", "axis"]:
        if not team_view.get(team):
            continue

        if team_view[team]["commander"] is not None:
            for mod in moderators:
                punitions_to_apply.merge(
                    mod.punitions_to_apply(
                        team_view,
                        "Commander",
                        team,
                        {
                            "players": [team_view[team]["commander"]],
                        },
                        gamestate,
                    )
                )

        for squad_name, squad in team_view[team]["squads"].items():
            for mod in moderators:
                punitions_to_apply.merge(
                    mod.punitions_to_apply(
                        team_view, squad_name, team, squad, gamestate
                    )
                )

    return punitions_to_apply


def _do_punitions(
    rcon: Rcon,
    method: ActionMethod,
    players: List[PunishPlayer],
    mods,
):
    for aplayer in players:
        try:
            if method == ActionMethod.MESSAGE:
                if not aplayer.details.dry_run:
                    rcon.message_player(
                        aplayer.name,
                        aplayer.player_id,
                        aplayer.details.message,
                        by=aplayer.details.author,
                    )
                audit(
                    discord_webhook_url=aplayer.details.discord_audit_url,
                    command_name="message_player",
                    msg=f"-> WARNING: {aplayer}",
                    author=aplayer.details.author,
                )

            if method == ActionMethod.PUNISH:
                if not aplayer.details.dry_run:
                    rcon.punish(
                        aplayer.name, aplayer.details.message, by=aplayer.details.author
                    )
                audit(
                    discord_webhook_url=aplayer.details.discord_audit_url,
                    command_name="punish",
                    msg=f"--> PUNISHING: {aplayer}",
                    author=aplayer.details.author,
                )

            if method == ActionMethod.KICK:
                if not aplayer.details.dry_run:
                    rcon.kick(
                        player_name=aplayer.name,
                        reason=aplayer.details.message,
                        by=aplayer.details.author,
                        player_id=aplayer.player_id,
                    )
                audit(
                    discord_webhook_url=aplayer.details.discord_audit_url,
                    command_name="kick",
                    msg=f"---> KICKING <---: {aplayer}",
                    author=aplayer.details.author,
                )
        except (CommandFailedError, HLLServerError):
            logger.warning(
                "Couldn't `%s` player `%s`. Will retry.", repr(method), repr(aplayer)
            )
            if method == ActionMethod.PUNISH:
                for m in mods:
                    m.player_punish_failed(aplayer)
            elif method == ActionMethod.KICK:
                audit(
                    discord_webhook_url=aplayer.details.discord_audit_url,
                    command_name="kick",
                    msg=f"---> KICK FAILED, will retry <---: {aplayer}",
                    author=aplayer.details.author,
                )


def do_punitions(rcon: Rcon, punitions_to_apply: PunitionsToApply):
    if punitions_to_apply:
        logger.debug(
            "Automod will apply the following punitions %s",
            repr(punitions_to_apply),
        )
    else:
        logger.debug("Automod did not suggest any punitions")

    _do_punitions(
        rcon, ActionMethod.MESSAGE, punitions_to_apply.warning, enabled_moderators()
    )

    _do_punitions(
        rcon, ActionMethod.PUNISH, punitions_to_apply.punish, enabled_moderators()
    )

    _do_punitions(
        rcon, ActionMethod.KICK, punitions_to_apply.kick, enabled_moderators()
    )


def enabled_moderators():
    red = get_redis_client()

    level_thresholds_config = AutoModLevelUserConfig.load_from_db()
    no_leader_config = AutoModNoLeaderUserConfig.load_from_db()
    seeding_config = AutoModSeedingUserConfig.load_from_db()
    solo_tank_config = AutoModNoSoloTankUserConfig.load_from_db()

    return list(
        filter(
            lambda m: m.enabled(),
            [
                NoLeaderAutomod(no_leader_config, red),
                SeedingRulesAutomod(seeding_config, red),
                LevelThresholdsAutomod(level_thresholds_config, red),
                NoSoloTankAutomod(solo_tank_config, red),
            ],
        )
    )


def is_first_run_done(r: Redis) -> bool:
    return r.exists(first_run_done_key) == 1


def set_first_run_done(r: Redis):
    r.setex(first_run_done_key, 4 * 60, "1")


def punish_squads(rcon: Rcon, r: Redis):
    mods = enabled_moderators()
    if len(mods) == 0:
        logger.debug("No automod is enabled")
        return

    punitions_to_apply = get_punitions_to_apply(rcon, mods)

    do_punitions(rcon, punitions_to_apply)
    set_first_run_done(r)


def audit(
    discord_webhook_url: HttpUrl | None, command_name: str, msg: str, author: str
):
    if discord_webhook_url is not None:
        send_to_discord_audit(
            message=msg,
            command_name=command_name,
            by=author,
            webhookurls=[discord_webhook_url],
            silent=False,
        )


@on_kill
def on_kill(rcon: Rcon, log: StructuredLogLineType):
    red = get_redis_client()
    if not is_first_run_done(red):
        logger.debug(
            "Kill event received, but not automod run done yet, "
            "giving mods time to warmup"
        )
        return
    mods = enabled_moderators()
    if len(mods) == 0:
        logger.debug("No automod is enabled")
        return

    punitions_to_apply = PunitionsToApply()
    for mod in mods:
        on_kill_hook = getattr(mod, "on_kill", None)
        if callable(on_kill_hook):
            punitions_to_apply.merge(mod.on_kill(log))

    do_punitions(rcon, punitions_to_apply)


pendingTimers = {}


@on_connected()
@inject_player_ids
def on_connected(rcon: Rcon, _, name: str, player_id: str):
    red = get_redis_client()
    if not is_first_run_done(red):
        logger.debug(
            "Kill event received, but not automod run done yet, "
            "giving mods time to warmup"
        )
        return
    mods = enabled_moderators()
    if len(mods) == 0:
        logger.debug("No automod is enabled")
        return

    # get detailed player info for use by on_connected_hook
    detailed_player_info: GetDetailedPlayer | None = None
    try:
        detailed_player_info = rcon.get_detailed_player_info(name)
    except Exception as e:
        logger.error(f"get_detailed_player_info threw an exception for {name}: {e}")

    punitions_to_apply: PunitionsToApply = PunitionsToApply()
    for mod in mods:
        on_connected_hook = getattr(mod, "on_connected", None)
        if callable(on_connected_hook):
            punitions_to_apply.merge(
                mod.on_connected(name, player_id, detailed_player_info)
            )

    def notify_player():
        try:
            for p in punitions_to_apply.warning:
                rcon.message_player(
                    player_id=p.player_id,
                    message=p.details.message,
                    by=p.details.author,
                    save_message=False,
                )
        except Exception as e:
            logger.error("Could not message player '%s' (%s) : %s", name, player_id, e)

    if len(punitions_to_apply.warning) == 0:
        return

    # The player might not yet have finished connecting in order to send messages.
    t = Timer(20, notify_player)
    pendingTimers[player_id] = t
    t.start()


def run():
    rcon = get_rcon()
    red = get_redis_client()

    while True:
        try:
            punish_squads(rcon, red)
            time.sleep(5)
        except Exception:
            logger.exception("Squad automod: Something unexpected happened")
            raise
