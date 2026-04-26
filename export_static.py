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


def copy_static_assets(out_dir: Path) -> None:
    if out_dir.exists():
        shutil.rmtree(out_dir)
    shutil.copytree(STATIC_ROOT, out_dir)
    (out_dir / ".nojekyll").write_text("", encoding="utf-8")


def export_site(out_dir: Path, current_only: bool = False, include_playoffs: bool = True, include_profiles: bool = True) -> dict[str, Any]:
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
    exported_team_keys: set[tuple[str, str]] = set()
    exported_division_keys: set[tuple[str, str, str, str]] = set()
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
            division_key = (division_season, str(division.get("level", "0")), str(division.get("conf", "0")), "1")
            exported_division_keys.add(division_key)
            for team in division.get("teams", []):
                if team.get("id"):
                    exported_team_keys.add((division_season, str(team["id"])))

    for season_id in exported_schedule_ids:
        write_json(schedule_dir / f"{season_id}.json", client.schedule(season_id))

    for season_id, level, conf, stat_class in sorted(exported_division_keys):
        stats = client.division_stats(season_id, level, conf, stat_class=stat_class)
        write_json(division_stats_dir / f"{season_id}-{level}-{conf}-{stat_class}.json", stats)
        all_names.update(str(row.get("name", "")) for row in stats.get("players", []) if row.get("name"))
        all_names.update(str(row.get("name", "")) for row in stats.get("goalies", []) if row.get("name"))

    for season_id, team_id in sorted(exported_team_keys):
        write_json(teams_dir / season_id / f"{team_id}.json", client.team(season_id, team_id))

    # Player profiles are the slowest live lookup. Export them once so public
    # visitors get a static JSON file instead of triggering history scans.
    if include_profiles:
        for season_type, stat_class in [("regular", "1"), ("playoffs", "2")]:
            if season_type == "playoffs" and not include_playoffs:
                continue
            for season in client.player_history_seasons():
                try:
                    index = client.season_player_index(season, stat_class=stat_class)
                except RuntimeError:
                    continue
                all_names.update(str(row.get("name", "")) for row in index.get("players", []) if row.get("name"))
                all_names.update(str(row.get("name", "")) for row in index.get("goalies", []) if row.get("name"))

            for name in sorted(all_names, key=str.casefold):
                payload = client.player_profile(name, season_type=season_type)
                write_json(players_dir / season_type / f"{server.slugify(name)}.json", payload)

    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "history_start_year": server.HISTORY_START_YEAR,
        "league_id": server.LEAGUE_ID,
        "requested_seasons": requested_season_ids,
        "standings_files": len(standings_by_request),
        "division_stat_files": len(exported_division_keys),
        "schedule_files": len(exported_schedule_ids),
        "team_files": len(exported_team_keys),
        "player_profile_names": len(all_names),
        "include_playoffs": include_playoffs,
        "include_profiles": include_profiles,
    }
    write_json(data_dir / "manifest.json", manifest)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="Export the hockey stats app as a static GitHub Pages site.")
    parser.add_argument("--out", default="dist", type=Path, help="Output directory for the static site.")
    parser.add_argument("--current-only", action="store_true", help="Export only the current season shell data.")
    parser.add_argument("--skip-playoffs", action="store_true", help="Skip playoff player profile JSON.")
    parser.add_argument("--skip-profiles", action="store_true", help="Skip player profile JSON. Useful for quick build smoke tests.")
    args = parser.parse_args()

    manifest = export_site(
        args.out,
        current_only=args.current_only,
        include_playoffs=not args.skip_playoffs,
        include_profiles=not args.skip_profiles,
    )
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
