from datetime import datetime, timedelta
import os
import logging

from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt

from rcon.config import get_config
from rcon.utils import MapsHistory
from rcon.recorded_commands import RecordedRcon
from rcon.commands import CommandFailedError
from rcon.steam_utils import get_steam_profile
from rcon.settings import SERVER_INFO
from rcon import game_logs
from rcon.models import (
    LogLine,
    PlayerSteamID,
    PlayerName,
    enter_session,
    Maps,
    PlayerStats,
)
from rcon.discord import send_to_discord_audit
from rcon.scoreboard import LiveStats, TimeWindowStats, get_cached_live_game_stats, current_game_stats

from .views import ctl, _get_data
from .auth import api_response, login_required
from rcon.utils import map_name, LONG_HUMAN_MAP_NAMES

logger = logging.getLogger("rconweb")


def make_table(scoreboard):
    return "\n".join(
        ["Rank  Name                  Ratio Kills Death"]
        + [
            f"{('#'+ str(idx+1)).ljust(6)}{obj['player'].ljust(27)}{obj['ratio'].ljust(6)}{str(obj['(real) kills']).ljust(6)}{str(obj['(real) death']).ljust(5)}"
            for idx, obj in enumerate(scoreboard)
        ]
    )


def make_tk_table(scoreboard):
    justification = [6, 27, 10, 10, 14, 14]
    headers = ["Rank", "Name", "Time(min)", "Teamkills", "Death-by-TK", "TK/Minutes"]
    keys = [
        "idx",
        "player",
        "Estimated play time (minutes)",
        "Teamkills",
        "Death by TK",
        "TK Minutes",
    ]

    return "\n".join(
        ["".join(h.ljust(justification[idx]) for idx, h in enumerate(headers))]
        + [
            "".join(
                [
                    str({"idx": f"#{idx}", **obj}[key]).ljust(justification[i])
                    for i, key in enumerate(keys)
                ]
            )
            for idx, obj in enumerate(scoreboard)
        ]
    )


@csrf_exempt
def text_scoreboard(request):
    try:
        minutes = abs(int(request.GET.get("minutes")))
    except (ValueError, KeyError, TypeError):
        minutes = 180

    name = ctl.get_name()
    try:
        scoreboard = ctl.get_scoreboard(minutes, "ratio")
        text = make_table(scoreboard)
        scoreboard = ctl.get_scoreboard(minutes, "(real) kills")
        text2 = make_table(scoreboard)
    except CommandFailedError:
        text, text2 = "No logs"

    return HttpResponse(
        f"""<div>
        <h1>{name}</h1>
        <h1>Scoreboard (last {minutes} min. 2min delay)</h1>
        <h6>Real death only (redeploy / suicides not included). Kills counted only if player is not revived</h6>
        <p>
        See for last:
        <a href="/api/scoreboard?minutes=120">120 min</a>
        <a href="/api/scoreboard?minutes=90">90 min</a>
        <a href="/api/scoreboard?minutes=60">60 min</a>
        <a href="/api/scoreboard?minutes=30">30 min</a>
        </p>
        <div style="float:left; margin-right:20px"><h3>By Ratio</h3><pre>{text}</pre></div>
        <div style="float:left; margin-left:20px"><h3>By Kills</h3><pre>{text2}</pre></div>
        </div>
        """
    )


@csrf_exempt
def text_tk_scoreboard(request):
    name = ctl.get_name()
    try:
        scoreboard = ctl.get_teamkills_boards()
        text = make_tk_table(scoreboard)
        scoreboard = ctl.get_teamkills_boards("Teamkills")
        text2 = make_tk_table(scoreboard)
    except CommandFailedError:
        text, text2 = "No logs"

    return HttpResponse(
        f"""<div>
        <h1>{name}</h1>
        <div style="float:left; margin-right:20px"><h3>By TK / Minute</h3><pre>{text}</pre></div>
        <div style="float:left; margin-left:20px"><h3>By Total TK</h3><pre>{text2}</pre></div>
        </div>
        """
    )


@csrf_exempt
def live_scoreboard(request):
    stats = LiveStats()
    config = get_config()

    try:
        result = stats.get_cached_stats()
        result = {
            "snapshot_timestamp": result["snapshot_timestamp"],
            "refresh_interval_sec": config.get('LIVE_STATS', {}).get('refresh_stats_seconds', 30),
            "stats": list(result["stats"].values()),
        }
        error = (None,)
        failed = False
    except Exception as e:
        logger.exception("Unable to produce live stats")
        result = {}
        error = ""
        failed = True

    return api_response(
        result=result, error=error, failed=failed, command="live_scoreboard"
    )


@csrf_exempt
def get_scoreboard_maps(request):
    data = _get_data(request)

    page_size = min(int(data.get("limit", 100)), 1000)
    page = max(1, int(data.get("page", 1)))
    server_number = data.get("server_number", os.getenv("SERVER_NUMBER"))

    with enter_session() as sess:
        query = (
            sess.query(Maps)
            .filter(Maps.server_number == server_number)
            .order_by(Maps.start.desc())
        )
        total = query.count()
        res = query.limit(page_size).offset((page - 1) * page_size).all()

        return api_response(
            result={
                "page": page,
                "page_size": page_size,
                "total": total,
                "maps": [
                    dict(
                        just_name=map_name(r.map_name),
                        long_name=LONG_HUMAN_MAP_NAMES.get(r.map_name, r.map_name),
                        **r.to_dict(),
                    )
                    for r in res
                ],
            },
            failed=False,
            command="get_scoreboard_maps",
        )

@csrf_exempt
def get_map_scoreboard(request):
    data = _get_data(request)
    error = None
    failed = False
    game = None

    try:
        map_id = int(data.get("map_id", None))
        with enter_session() as sess:
            game = sess.query(Maps).filter(Maps.id == map_id).one_or_none()
            #import ipdb; ipdb.set_trace()
            if not game:
                error = "No map for this ID"
                failed = True
            else:
                game = game.to_dict(with_stats=True)
    except Exception as e:
        game = None
        error = repr(e)
        failed = True
    
    return api_response(
        result=game, error=error, failed=failed, command="get_map_scoreboard"
    )

@csrf_exempt
def get_live_game_stats(request):
    stats = None 
    error_ = None
    failed = True

    try:
        stats = get_cached_live_game_stats()
        failed = False
    except Exception as e:
        logger.exception("Failed to get live game stats")
        error_ = repr(e)

    return api_response(
        result=stats, error=error_, failed=failed, command="get_live_game_stats"
    )
    
@csrf_exempt
@login_required
def date_scoreboard(request):
    try:
        start = datetime.fromtimestamp(request.GET.get("start"))
    except (ValueError, KeyError, TypeError) as e:
        start = datetime.now() - timedelta(minutes=60)
    try:
        end = datetime.fromtimestamp(request.GET.get("end"))
    except (ValueError, KeyError, TypeError) as e:
        end = datetime.now()

    stats = TimeWindowStats()

    try:
        result = stats.get_players_stats_at_time(start, end)
        error_ = (None,)
        failed = False

    except Exception as e:
        logger.exception("Unable to produce date stats")
        result = {}
        error_ = ""
        failed = True

    return api_response(
        result=result, error=error_, failed=failed, command="date_scoreboard"
    )
