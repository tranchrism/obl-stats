from __future__ import annotations

import argparse
import json
import shutil
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


def export_game_centers(
    client: server.TimetoscoreClient,
    schedules: dict[str, dict[str, Any]],
    out_dir: Path,
    cache_dir: Path | None,
    limit: int = 0,
) -> dict[str, int]:
    exported = 0
    fetched = 0
    reused = 0
    failed = 0
    seen: dict[str, dict[str, Any] | None] = {}
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
            cache_path = cache_game_center_dir / f"{game_id}.json" if cache_game_center_dir else None
            if cache_path:
                payload = read_json(cache_path)
            if payload is not None:
                reused += 1
            elif not limit or fetched < limit:
                try:
                    payload = client.game_center(game_id, season_id, game)
                    fetched += 1
                    if cache_path:
                        write_json(cache_path, payload)
                except Exception as exc:
                    failed += 1
                    payload = {"game_id": game_id, "season": season_id, "error": str(exc), "has_events": False}
            seen[game_id] = payload

        if not payload or payload.get("error"):
            continue
        write_json(game_center_dir / f"{game_id}.json", payload)
        game["boxscore_available"] = True
        game["boxscore_path"] = f"data/game-centers/{game_id}.json"
        exported += 1

    return {
        "game_center_files": exported,
        "game_centers_fetched": fetched,
        "game_centers_reused": reused,
        "game_centers_failed": failed,
        "game_centers_missing": max(len({game.get("game_id") for _, game in final_games if game.get("game_id")}) - len([game_id for game_id, payload in seen.items() if payload and not payload.get("error")]), 0),
    }


def export_site(
    out_dir: Path,
    current_only: bool = False,
    include_playoffs: bool = True,
    include_profiles: bool = True,
    include_game_centers: bool = False,
    cache_dir: Path | None = None,
    game_center_limit: int = 0,
) -> dict[str, Any]:
    copy_static_assets(out_dir)

    client = server.TimetoscoreClient()
    data_dir = out_dir / "data"
    standings_dir = data_dir / "standings"
    division_stats_dir = data_dir / "division-stats"
    schedule_dir = data_dir / "schedule"
    teams_dir = data_dir / "teams"
    players_dir = data_dir / "players"

    standings_by_request: dict[str, dict[str, Any]] = {}
    requested_season_ids = ["0"]
    if not current_only:
        requested_season_ids.extend(season["id"] for season in client.seasons() if season["id"] != "0")

    all_names: set[str] = set()
    team_payloads: dict[tuple[str, str], dict[str, Any]] = {}
    team_context: dict[tuple[str, str], dict[str, Any]] = {}
    exported_division_keys: set[tuple[str, str, str, str]] = set()
    division_context: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    exported_schedule_ids: set[str] = set()

    for requested_season in requested_season_ids:
        standings = client.standings(requested_season)
        if current_only:
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
        exported_schedule_ids.add(str(standings.get("season", requested_season)))

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

    schedules: dict[str, dict[str, Any]] = {}
    for season_id in exported_schedule_ids:
        schedule = client.schedule(season_id)
        schedules[season_id] = schedule
    game_center_manifest = (
        export_game_centers(client, schedules, data_dir, cache_dir, limit=game_center_limit)
        if include_game_centers
        else {"game_center_files": 0, "game_centers_fetched": 0, "game_centers_reused": 0, "game_centers_failed": 0, "game_centers_missing": 0}
    )
    for season_id, schedule in schedules.items():
        write_json(schedule_dir / f"{season_id}.json", schedule)
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

    profile_files = write_profile_files(players_dir, profiles) if include_profiles else 0

    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "history_start_year": server.HISTORY_START_YEAR,
        "league_id": server.LEAGUE_ID,
        "requested_seasons": requested_season_ids,
        "standings_files": len(standings_by_request),
        "division_stat_files": len(exported_division_keys),
        "schedule_files": len(exported_schedule_ids),
        "team_files": len(team_payloads),
        "player_profile_names": len(all_names),
        "player_profile_files": profile_files,
        "include_playoffs": include_playoffs,
        "include_profiles": include_profiles,
        "include_game_centers": include_game_centers,
        **game_center_manifest,
    }
    write_json(data_dir / "manifest.json", manifest)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="Export the hockey stats app as a static GitHub Pages site.")
    parser.add_argument("--out", default="dist", type=Path, help="Output directory for the static site.")
    parser.add_argument("--current-only", action="store_true", help="Export only the current season shell data.")
    parser.add_argument("--skip-playoffs", action="store_true", help="Skip playoff player profile JSON.")
    parser.add_argument("--skip-profiles", action="store_true", help="Skip player profile JSON. Useful for quick build smoke tests.")
    parser.add_argument("--include-game-centers", action="store_true", help="Export cached game-center box scores for final games.")
    parser.add_argument("--cache-dir", default=".export-cache", type=Path, help="Persistent cache directory reused by scheduled exports.")
    parser.add_argument("--game-center-limit", default=0, type=int, help="Maximum missing game centers to fetch this run. 0 means no limit.")
    args = parser.parse_args()

    manifest = export_site(
        args.out,
        current_only=args.current_only,
        include_playoffs=not args.skip_playoffs,
        include_profiles=not args.skip_profiles,
        include_game_centers=args.include_game_centers,
        cache_dir=args.cache_dir,
        game_center_limit=args.game_center_limit,
    )
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
