from __future__ import annotations

import argparse
import json
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import server


ROOT = Path(__file__).resolve().parent
STATIC_ROOT = ROOT / "static"


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def read_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def copy_static_assets(out_dir: Path) -> None:
    if out_dir.exists():
        shutil.rmtree(out_dir)
    shutil.copytree(STATIC_ROOT, out_dir)
    (out_dir / ".nojekyll").write_text("", encoding="utf-8")


def copy_cached_data(cache_dir: Path | None, data_dir: Path) -> bool:
    if not cache_dir:
        return False
    cache_data_dir = cache_dir / "data"
    if not cache_data_dir.exists():
        return False
    if data_dir.exists():
        shutil.rmtree(data_dir)
    shutil.copytree(cache_data_dir, data_dir)
    return True


def save_data_cache(data_dir: Path, cache_dir: Path | None) -> None:
    if not cache_dir or not data_dir.exists():
        return
    cache_data_dir = cache_dir / "data"
    if cache_data_dir.exists():
        shutil.rmtree(cache_data_dir)
    shutil.copytree(data_dir, cache_data_dir)


def count_json_files(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for entry in path.rglob("*.json") if entry.is_file())


def add_profile_row(profiles: dict[str, dict[str, Any]], row: dict[str, Any], row_type: str, split: str) -> None:
    name = str(row.get("name", "")).strip()
    if not name:
        return
    key = server.slugify(name)
    profile = profiles.setdefault(
        key,
        {
            "name": name,
            "history_start_year": server.HISTORY_START_YEAR,
            "identity_keys": sorted(server.name_identity_keys(name)),
            "available_splits": [],
            "splits": {},
        },
    )
    if split not in profile["available_splits"]:
        profile["available_splits"].append(split)
    split_payload = profile["splits"].setdefault(split, {"skater_seasons": [], "goalie_seasons": []})
    target = "goalie_seasons" if row_type == "goalie" else "skater_seasons"
    split_payload[target].append(row)


def write_profile_files(players_dir: Path, profiles: dict[str, dict[str, Any]]) -> int:
    count = 0
    for slug, profile in sorted(profiles.items()):
        for split in profile["available_splits"]:
            split_payload = profile["splits"].get(split, {})
            skater_rows = split_payload.get("skater_seasons", [])
            goalie_rows = split_payload.get("goalie_seasons", [])
            payload = {
                "name": profile["name"],
                "season_type": split,
                "history_start_year": profile["history_start_year"],
                "identity_keys": profile["identity_keys"],
                "available_splits": profile["available_splits"],
                "skater_seasons": skater_rows,
                "goalie_seasons": goalie_rows,
                "skater_career": server.career_totals(skater_rows, mode="skater"),
                "goalie_career": server.career_totals(goalie_rows, mode="goalie"),
            }
            write_json(players_dir / split / f"{slug}.json", payload)
            count += 1
    return count


def recompute_profile_payload(payload: dict[str, Any], split: str) -> dict[str, Any]:
    skater_rows = payload.get("skater_seasons", []) if isinstance(payload.get("skater_seasons"), list) else []
    goalie_rows = payload.get("goalie_seasons", []) if isinstance(payload.get("goalie_seasons"), list) else []
    available_splits = payload.get("available_splits") if isinstance(payload.get("available_splits"), list) else [split]
    if split not in available_splits:
        available_splits.append(split)
    payload["season_type"] = split
    payload["available_splits"] = available_splits
    payload["skater_seasons"] = skater_rows
    payload["goalie_seasons"] = goalie_rows
    payload["skater_career"] = server.career_totals(skater_rows, mode="skater")
    payload["goalie_career"] = server.career_totals(goalie_rows, mode="goalie")
    return payload


def strip_profile_seasons(players_dir: Path, season_ids: set[str], splits: set[str]) -> int:
    stripped = 0
    if not season_ids:
        return stripped
    for split in splits:
        split_dir = players_dir / split
        if not split_dir.exists():
            continue
        for path in split_dir.glob("*.json"):
            payload = read_json(path)
            if not isinstance(payload, dict):
                continue
            changed = False
            for key in ("skater_seasons", "goalie_seasons"):
                rows = payload.get(key, [])
                if not isinstance(rows, list):
                    rows = []
                filtered = [row for row in rows if str(row.get("season_id", "")) not in season_ids]
                if len(filtered) != len(rows):
                    changed = True
                payload[key] = filtered
            if not changed:
                continue
            stripped += 1
            if not payload["skater_seasons"] and not payload["goalie_seasons"]:
                path.unlink(missing_ok=True)
            else:
                write_json(path, recompute_profile_payload(payload, split))
    return stripped


def merge_profile_files(players_dir: Path, profiles: dict[str, dict[str, Any]]) -> int:
    for slug, profile in sorted(profiles.items()):
        for split in profile["available_splits"]:
            split_payload = profile["splits"].get(split, {})
            path = players_dir / split / f"{slug}.json"
            existing = read_json(path)
            if not isinstance(existing, dict):
                existing = {
                    "name": profile["name"],
                    "history_start_year": profile["history_start_year"],
                    "identity_keys": profile["identity_keys"],
                    "available_splits": [],
                    "skater_seasons": [],
                    "goalie_seasons": [],
                }
            available_splits = existing.get("available_splits") if isinstance(existing.get("available_splits"), list) else []
            for available_split in profile["available_splits"]:
                if available_split not in available_splits:
                    available_splits.append(available_split)
            identity_keys = sorted(set(existing.get("identity_keys", [])) | set(profile.get("identity_keys", [])))
            payload = {
                **existing,
                "name": existing.get("name") or profile["name"],
                "history_start_year": existing.get("history_start_year") or profile["history_start_year"],
                "identity_keys": identity_keys,
                "available_splits": available_splits,
                "skater_seasons": [
                    *(existing.get("skater_seasons", []) if isinstance(existing.get("skater_seasons"), list) else []),
                    *split_payload.get("skater_seasons", []),
                ],
                "goalie_seasons": [
                    *(existing.get("goalie_seasons", []) if isinstance(existing.get("goalie_seasons"), list) else []),
                    *split_payload.get("goalie_seasons", []),
                ],
            }
            write_json(path, recompute_profile_payload(payload, split))
    return count_json_files(players_dir)


def team_matches_game(team: dict[str, Any], game: dict[str, Any]) -> bool:
    team_id = str(team.get("id", ""))
    team_name = str(team.get("name", ""))
    return (
        (team_id and team_id in {str(game.get("away_team_id", "")), str(game.get("home_team_id", ""))})
        or (team_name and team_name in {str(game.get("away_team", "")), str(game.get("home_team", ""))})
    )


def new_team_payload(season_id: str, team: dict[str, Any]) -> dict[str, Any]:
    return {
        "season": season_id,
        "team_id": str(team.get("id", "")),
        "team_name": team.get("name", ""),
        "games": [],
        "players": [],
        "goalies": [],
        "team_stats": [],
        "special_teams": [],
    }


def int_sort_value(value: Any) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 0


def person_team_key(team: Any, name: Any) -> tuple[str, str]:
    return (server.normalize_name(str(team or "")), server.normalize_name(str(name or "")))


def box_score_event_sort_key(event: dict[str, Any]) -> tuple[int, int]:
    return (
        server.period_sort_value(event.get("period")),
        -server.seconds_remaining(event.get("time")),
    )


def apply_team_scoped_box_score_totals(records: list[dict[str, Any]]) -> None:
    """Replace source-wide event totals with season running totals for that team."""
    records_by_season: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        payload = record.get("payload")
        if isinstance(payload, dict):
            records_by_season.setdefault(str(payload.get("season", "")), []).append(record)

    for season_records in records_by_season.values():
        goal_totals: dict[tuple[str, str], int] = {}
        assist_totals: dict[tuple[str, str], int] = {}
        season_records.sort(key=lambda record: int_sort_value(record.get("game_id")))

        for record in season_records:
            payload = record.get("payload", {})
            scoring = payload.get("scoring", []) if isinstance(payload, dict) else []
            if not isinstance(scoring, list):
                continue

            events: list[dict[str, Any]] = []
            for period in scoring:
                if isinstance(period, dict) and isinstance(period.get("events"), list):
                    events.extend(event for event in period["events"] if isinstance(event, dict))

            for event in sorted(events, key=box_score_event_sort_key):
                team = event.get("team")
                scorer = event.get("scorer")
                if team and scorer:
                    key = person_team_key(team, scorer)
                    goal_totals[key] = goal_totals.get(key, 0) + 1
                    event["scorer_total"] = goal_totals[key]
                    event["scorer_total_scope"] = "team"

                assists = event.get("assists", [])
                if not isinstance(assists, list):
                    continue
                for assist in assists:
                    if not isinstance(assist, dict):
                        continue
                    name = assist.get("name")
                    if team and name:
                        key = person_team_key(team, name)
                        assist_totals[key] = assist_totals.get(key, 0) + 1
                        assist["total"] = assist_totals[key]
                        assist["total_scope"] = "team"


def score_value(payload: dict[str, Any], side: str) -> int | None:
    value = payload.get(f"{side}_score")
    if isinstance(value, int):
        return value
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def game_winning_goal(payload: dict[str, Any]) -> dict[str, str] | None:
    """Return the scorer/team for the standard loser-final-score-plus-one GWG."""
    away_score = score_value(payload, "away")
    home_score = score_value(payload, "home")
    if away_score is None or home_score is None or away_score == home_score:
        return None

    winner_side = "away" if away_score > home_score else "home"
    winner_team = str(payload.get(f"{winner_side}_team", "")).strip()
    winning_goal_number = min(away_score, home_score) + 1
    if not winner_team:
        return None

    scoring = payload.get("scoring", [])
    if not isinstance(scoring, list):
        return None

    events: list[dict[str, Any]] = []
    for period in scoring:
        if isinstance(period, dict) and isinstance(period.get("events"), list):
            events.extend(event for event in period["events"] if isinstance(event, dict))

    running_winner_goals = 0
    for event in sorted(events, key=box_score_event_sort_key):
        if server.normalize_name(str(event.get("team", ""))) != server.normalize_name(winner_team):
            continue
        running_winner_goals += 1
        score = event.get("score")
        winner_score = score.get(winner_side) if isinstance(score, dict) else None
        if not isinstance(winner_score, int):
            winner_score = running_winner_goals
        if winner_score == winning_goal_number and event.get("scorer"):
            return {
                "season": str(payload.get("season", "")),
                "team": winner_team,
                "scorer": str(event["scorer"]),
            }
    return None


def derived_gwg_totals(records: list[dict[str, Any]]) -> dict[tuple[str, str, str], int]:
    totals: dict[tuple[str, str, str], int] = {}
    for record in records:
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        winner = game_winning_goal(payload)
        if not winner:
            continue
        season = winner["season"]
        team = server.normalize_name(winner["team"])
        for identity_key in server.name_identity_keys(winner["scorer"]):
            key = (season, team, identity_key)
            totals[key] = totals.get(key, 0) + 1
    return totals


def gwg_total_for_row(totals: dict[tuple[str, str, str], int], row: dict[str, Any]) -> int | None:
    season = str(row.get("season_id", ""))
    team = server.normalize_name(str(row.get("team", "")))
    if not season or not team:
        return None
    matches = [
        totals[(season, team, identity_key)]
        for identity_key in server.name_identity_keys(str(row.get("name", "")))
        if (season, team, identity_key) in totals
    ]
    return max(matches) if matches else None


def export_game_centers(
    client: server.TimetoscoreClient,
    schedules: dict[str, dict[str, Any]],
    out_dir: Path,
    cache_dir: Path | None,
    limit: int = 0,
) -> tuple[dict[str, int], dict[tuple[str, str, str], int]]:
    exported = 0
    attempted = 0
    fetched = 0
    reused = 0
    failed = 0
    seen: dict[str, dict[str, Any] | None] = {}
    records: list[dict[str, Any]] = []
    game_center_dir = out_dir / "game-centers"
    cache_game_center_dir = cache_dir / "game-centers" if cache_dir else None

    final_games: list[tuple[str, dict[str, Any]]] = []
    for season_id, schedule in schedules.items():
        for game in schedule.get("games", []):
            if game.get("final") and game.get("game_id"):
                final_games.append((season_id, game))
    final_games.sort(
        key=lambda item: (
            int(item[0]) if str(item[0]).isdigit() else 0,
            int(item[1].get("game_id", 0)) if str(item[1].get("game_id", "")).isdigit() else 0,
        ),
        reverse=True,
    )

    for season_id, game in final_games:
        game_id = str(game.get("game_id", ""))
        if not game_id:
            continue
        cached_payload = seen.get(game_id)
        if game_id in seen:
            payload = cached_payload
        else:
            payload = None
            stale_payload = None
            cache_path = cache_game_center_dir / f"{game_id}.json" if cache_game_center_dir else None
            if cache_path:
                payload = read_json(cache_path)
                if payload and payload.get("schema_version") != server.GAME_CENTER_SCHEMA_VERSION:
                    stale_payload = payload
                    payload = None
            if payload is not None:
                reused += 1
            elif not limit or attempted < limit:
                attempted += 1
                try:
                    payload = client.game_center(game_id, season_id, game)
                    fetched += 1
                    if cache_path:
                        write_json(cache_path, payload)
                except Exception as exc:
                    failed += 1
                    payload = stale_payload or {"game_id": game_id, "season": season_id, "error": str(exc), "has_events": False}
            else:
                payload = stale_payload
            seen[game_id] = payload

        if not payload or payload.get("error"):
            continue
        records.append({"game_id": game_id, "payload": payload})
        game["boxscore_available"] = True
        game["boxscore_path"] = f"data/game-centers/{game_id}.json"
        exported += 1

    apply_team_scoped_box_score_totals(records)
    for record in records:
        write_json(game_center_dir / f"{record['game_id']}.json", record["payload"])

    manifest = {
        "game_center_files": exported,
        "game_centers_attempted": attempted,
        "game_centers_fetched": fetched,
        "game_centers_reused": reused,
        "game_centers_failed": failed,
        "game_centers_missing": max(len({game.get("game_id") for _, game in final_games if game.get("game_id")}) - len([game_id for game_id, payload in seen.items() if payload and not payload.get("error")]), 0),
    }
    return manifest, derived_gwg_totals(records)


def export_site(
    out_dir: Path,
    current_only: bool = False,
    incremental_current: bool = False,
    include_playoffs: bool = True,
    include_profiles: bool = True,
    include_game_centers: bool = False,
    cache_dir: Path | None = None,
    game_center_limit: int = 0,
) -> dict[str, Any]:
    started_at = time.perf_counter()
    copy_static_assets(out_dir)

    client = server.TimetoscoreClient()
    data_dir = out_dir / "data"
    cached_data_restored = copy_cached_data(cache_dir, data_dir) if incremental_current else False
    if incremental_current and not cached_data_restored:
        incremental_current = False

    standings_dir = data_dir / "standings"
    division_stats_dir = data_dir / "division-stats"
    schedule_dir = data_dir / "schedule"
    teams_dir = data_dir / "teams"
    players_dir = data_dir / "players"

    standings_by_request: dict[str, dict[str, Any]] = {}
    requested_season_ids = ["0"]
    if not current_only and not incremental_current:
        requested_season_ids.extend(season["id"] for season in client.seasons() if season["id"] != "0")

    all_names: set[str] = set()
    team_payloads: dict[tuple[str, str], dict[str, Any]] = {}
    team_context: dict[tuple[str, str], dict[str, Any]] = {}
    exported_division_keys: set[tuple[str, str, str, str]] = set()
    division_context: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    exported_schedule_ids: set[str] = set()
    schedule_aliases: dict[str, str] = {}

    for requested_season in requested_season_ids:
        standings = client.standings(requested_season)
        if current_only and not incremental_current:
            standings = {
                **standings,
                "seasons": [
                    season
                    for season in standings.get("seasons", [])
                    if season.get("id") == requested_season or (requested_season == "0" and season.get("current"))
                ],
            }
        standings_by_request[requested_season] = standings
        write_json(standings_dir / f"{requested_season}.json", standings)
        resolved_schedule_id = str(standings.get("season", requested_season))
        exported_schedule_ids.add(resolved_schedule_id)
        if requested_season != resolved_schedule_id:
            schedule_aliases[requested_season] = resolved_schedule_id

        for division in standings.get("divisions", []):
            division_season = str(division.get("season", standings.get("season", requested_season)))
            exported_schedule_ids.add(division_season)
            stat_classes = ["1", "2"] if include_playoffs else ["1"]
            for stat_class in stat_classes:
                division_key = (division_season, str(division.get("level", "0")), str(division.get("conf", "0")), stat_class)
                exported_division_keys.add(division_key)
                division_context[division_key] = {
                    "season": standings.get("season", requested_season),
                    "season_name": next(
                        (entry.get("name") for entry in standings.get("seasons", []) if str(entry.get("id")) == requested_season),
                        "Current" if requested_season == "0" else requested_season,
                    ),
                    "division": division.get("name", ""),
                    "division_id": division.get("id", ""),
                    "teams": division.get("teams", []),
                }
            for team in division.get("teams", []):
                if team.get("id"):
                    team_key = (division_season, str(team["id"]))
                    team_context[team_key] = team
                    team_payloads.setdefault(team_key, new_team_payload(division_season, team))

    if incremental_current:
        for season_id in exported_schedule_ids:
            shutil.rmtree(teams_dir / season_id, ignore_errors=True)
            if division_stats_dir.exists():
                for path in division_stats_dir.glob(f"{season_id}-*.json"):
                    path.unlink(missing_ok=True)

    schedules: dict[str, dict[str, Any]] = {}
    for season_id in exported_schedule_ids:
        schedule = client.schedule(season_id)
        schedules[season_id] = schedule
    if include_game_centers:
        game_center_manifest, gwg_totals = export_game_centers(client, schedules, data_dir, cache_dir, limit=game_center_limit)
    else:
        game_center_manifest = {"game_center_files": 0, "game_centers_fetched": 0, "game_centers_reused": 0, "game_centers_failed": 0, "game_centers_missing": 0}
        gwg_totals = {}
    for season_id, schedule in schedules.items():
        write_json(schedule_dir / f"{season_id}.json", schedule)
    for alias_id, season_id in schedule_aliases.items():
        if season_id in schedules:
            write_json(schedule_dir / f"{alias_id}.json", schedules[season_id])
    for (season_id, team_id), payload in team_payloads.items():
        team = team_context.get((season_id, team_id), {})
        payload["games"] = [
            game
            for game in schedules.get(season_id, {}).get("games", [])
            if team_matches_game(team, game)
        ]

    profiles: dict[str, dict[str, Any]] = {}
    for season_id, level, conf, stat_class in sorted(exported_division_keys):
        stats = client.division_stats(season_id, level, conf, stat_class=stat_class)
        write_json(division_stats_dir / f"{season_id}-{level}-{conf}-{stat_class}.json", stats)
        all_names.update(str(row.get("name", "")) for row in stats.get("players", []) if row.get("name"))
        all_names.update(str(row.get("name", "")) for row in stats.get("goalies", []) if row.get("name"))
        context = division_context.get((season_id, level, conf, stat_class), {})
        team_by_name = {str(team.get("name", "")): team for team in context.get("teams", [])}
        split = "playoffs" if stat_class == "2" else "regular"
        for player in stats.get("players", []):
            team = team_by_name.get(str(player.get("team", "")), {})
            team_id = str(team.get("id", ""))
            player_row = {
                **player,
                "season": context.get("season_name", season_id),
                "season_id": context.get("season", season_id),
                "division": context.get("division", ""),
                "division_id": context.get("division_id", ""),
                "team_id": team_id,
            }
            derived_gwg = gwg_total_for_row(gwg_totals, player_row)
            if derived_gwg is not None:
                player_row["gwg"] = derived_gwg
            if include_profiles:
                add_profile_row(
                    profiles,
                    player_row,
                    "skater",
                    split,
                )
            if stat_class == "1" and team_id:
                team_payloads.setdefault((season_id, team_id), new_team_payload(season_id, team)).setdefault("players", []).append(player_row)
        for goalie in stats.get("goalies", []):
            team = team_by_name.get(str(goalie.get("team", "")), {})
            team_id = str(team.get("id", ""))
            goalie_row = {
                **goalie,
                "season": context.get("season_name", season_id),
                "season_id": context.get("season", season_id),
                "division": context.get("division", ""),
                "division_id": context.get("division_id", ""),
                "team_id": team_id,
            }
            if include_profiles:
                add_profile_row(
                    profiles,
                    goalie_row,
                    "goalie",
                    split,
                )
            if stat_class == "1" and team_id:
                team_payloads.setdefault((season_id, team_id), new_team_payload(season_id, team)).setdefault("goalies", []).append(goalie_row)

    for season_id, team_id in sorted(team_payloads):
        payload = team_payloads[(season_id, team_id)]
        payload["players"] = server.remove_goalie_overlap_from_skaters(payload.get("players", []), payload.get("goalies", []))
        write_json(teams_dir / season_id / f"{team_id}.json", payload)

    profile_seasons_stripped = 0
    if include_profiles and incremental_current:
        profile_seasons_stripped = strip_profile_seasons(
            players_dir,
            {str(season_id) for season_id in exported_schedule_ids},
            {"regular", "playoffs"} if include_playoffs else {"regular"},
        )
        profile_files = merge_profile_files(players_dir, profiles)
    else:
        profile_files = write_profile_files(players_dir, profiles) if include_profiles else 0

    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "export_mode": "incremental-current" if incremental_current else "current-only" if current_only else "full",
        "cached_data_restored": cached_data_restored,
        "history_start_year": server.HISTORY_START_YEAR,
        "league_id": server.LEAGUE_ID,
        "requested_seasons": requested_season_ids,
        "standings_files": count_json_files(standings_dir),
        "division_stat_files": count_json_files(division_stats_dir),
        "schedule_files": count_json_files(schedule_dir),
        "schedule_aliases": schedule_aliases,
        "team_files": count_json_files(teams_dir),
        "player_profile_names": len(all_names),
        "player_profile_files": profile_files,
        "profile_seasons_stripped": profile_seasons_stripped,
        "include_playoffs": include_playoffs,
        "include_profiles": include_profiles,
        "include_game_centers": include_game_centers,
        "duration_seconds": round(time.perf_counter() - started_at, 2),
        "total_game_center_files": count_json_files(data_dir / "game-centers"),
        **game_center_manifest,
    }
    write_json(data_dir / "manifest.json", manifest)
    if cache_dir and (incremental_current or not current_only):
        save_data_cache(data_dir, cache_dir)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="Export the hockey stats app as a static GitHub Pages site.")
    parser.add_argument("--out", default="dist", type=Path, help="Output directory for the static site.")
    parser.add_argument("--current-only", action="store_true", help="Export only the current season shell data.")
    parser.add_argument("--incremental-current", action="store_true", help="Restore cached static data and refresh only the current season.")
    parser.add_argument("--skip-playoffs", action="store_true", help="Skip playoff player profile JSON.")
    parser.add_argument("--skip-profiles", action="store_true", help="Skip player profile JSON. Useful for quick build smoke tests.")
    parser.add_argument("--include-game-centers", action="store_true", help="Export cached game-center box scores for final games.")
    parser.add_argument("--cache-dir", default=".export-cache", type=Path, help="Persistent cache directory reused by scheduled exports.")
    parser.add_argument("--game-center-limit", default=0, type=int, help="Maximum missing game centers to fetch this run. 0 means no limit.")
    args = parser.parse_args()

    manifest = export_site(
        args.out,
        current_only=args.current_only,
        incremental_current=args.incremental_current,
        include_playoffs=not args.skip_playoffs,
        include_profiles=not args.skip_profiles,
        include_game_centers=args.include_game_centers,
        cache_dir=args.cache_dir,
        game_center_limit=args.game_center_limit,
    )
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
