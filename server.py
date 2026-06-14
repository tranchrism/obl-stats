from __future__ import annotations

import json
import hashlib
import hmac
import http.cookiejar
import re
import ssl
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import RLock, Thread
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urljoin, urlparse
from urllib.request import HTTPCookieProcessor, HTTPSHandler, Request, build_opener, urlopen


BASE_URL = "https://stats.sharksice.timetoscore.com/"
API_PROXY_PATH = "test/api-proxy.php"
API_BOOTSTRAP_PATH = "test/site.php"
LEAGUE_ID = "27"
CACHE_TTL_SECONDS = 24 * 60 * 60
HISTORY_START_YEAR = 2015
API_MIN_INTERVAL_SECONDS = 0.25
API_RATE_LIMIT_RETRY_SECONDS = 12
GAME_CENTER_PATH = "/get_game_center"
GAME_CENTER_BOOTSTRAP_URL = urljoin(BASE_URL, "oss-scoresheet")
GAME_CENTER_SCHEMA_VERSION = 3
GAME_CENTER_CONFIG_ERROR_TTL_SECONDS = 5 * 60
ROOT = Path(__file__).resolve().parent
STATIC_ROOT = ROOT / "static"


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("\xa0", " ")).strip()


def int_or_none(value: str) -> int | None:
    value = clean_text(value).replace("*", "")
    if value == "":
        return None
    try:
        return int(value)
    except ValueError:
        return None


def float_or_none(value: str) -> float | None:
    value = clean_text(value).replace("%", "")
    if value == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def slugify(value: str) -> str:
    value = clean_text(value).lower()
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    return value or "unknown"


def parse_query(href: str) -> dict[str, str]:
    parsed = urlparse(urljoin(BASE_URL, href))
    return {key: values[-1] for key, values in parse_qs(parsed.query).items()}


def absolutize(href: str | None) -> str | None:
    if not href:
        return None
    return urljoin(BASE_URL, href)


def season_year(value: str) -> int | None:
    years = [int(year) for year in re.findall(r"(?:19|20)\d{2}", value)]
    if years:
        return max(years)
    short_range = re.search(r"\b((?:19|20)\d{2})\s*[-/]\s*(\d{2})\b", value)
    if not short_range:
        return None
    start = int(short_range.group(1))
    end = (start // 100 * 100) + int(short_range.group(2))
    if end < start:
        end += 100
    return end


def is_supported_season(season: dict[str, Any]) -> bool:
    if season.get("id") == "0" or season.get("current") or season.get("name") == "Current":
        return True
    year = season_year(str(season.get("name", "")))
    return year is None or year >= HISTORY_START_YEAR


def api_payload_error(endpoint: str, payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return f"TimeToScore API {endpoint} returned {type(payload).__name__}, expected object"
    status = clean_text(str(payload.get("status", ""))).casefold()
    if payload.get("success") is False or status in {"error", "failed"} or payload.get("error"):
        detail = payload.get("error") or payload.get("message") or payload.get("errors") or payload
        return f"TimeToScore API {endpoint} returned an error: {detail}"
    return None


@dataclass
class Cell:
    text: str = ""
    header: bool = False
    links: list[dict[str, str]] = field(default_factory=list)


class TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[Cell]]] = []
        self._in_table = 0
        self._current_table: list[list[Cell]] | None = None
        self._current_row: list[Cell] | None = None
        self._current_cell: Cell | None = None
        self._active_link: str | None = None
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_map = {key.lower(): value for key, value in attrs if value is not None}
        tag = tag.lower()
        if tag in {"script", "style"}:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag == "table":
            self._in_table += 1
            if self._in_table == 1:
                self._current_table = []
            return
        if self._in_table == 0:
            return
        if tag == "tr":
            self._current_row = []
            return
        if tag in {"td", "th"}:
            self._current_cell = Cell(header=(tag == "th"))
            return
        if tag == "a" and self._current_cell is not None:
            self._active_link = attrs_map.get("href")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style"} and self._skip_depth:
            self._skip_depth -= 1
            return
        if self._skip_depth:
            return
        if tag == "a":
            self._active_link = None
            return
        if tag in {"td", "th"} and self._current_cell is not None:
            self._current_cell.text = clean_text(self._current_cell.text)
            if self._current_row is not None:
                self._current_row.append(self._current_cell)
            self._current_cell = None
            return
        if tag == "tr" and self._current_row is not None:
            if self._current_table is not None and self._current_row:
                self._current_table.append(self._current_row)
            self._current_row = None
            return
        if tag == "table" and self._in_table:
            self._in_table -= 1
            if self._in_table == 0 and self._current_table is not None:
                self.tables.append(self._current_table)
                self._current_table = None

    def handle_data(self, data: str) -> None:
        if self._skip_depth or self._current_cell is None:
            return
        self._current_cell.text += data
        if self._active_link:
            if self._current_cell.links and self._current_cell.links[-1]["href"] == self._active_link:
                self._current_cell.links[-1]["text"] += data
            else:
                self._current_cell.links.append({"href": self._active_link, "text": data})


class OptionParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.options: list[dict[str, str]] = []
        self._current: dict[str, str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "option":
            return
        attrs_map = {key.lower(): value or "" for key, value in attrs}
        self._current = {
            "id": attrs_map.get("value", ""),
            "name": "",
            "current": "selected" in attrs_map,
        }

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "option" and self._current is not None:
            self._current["name"] = clean_text(self._current["name"])
            self.options.append(self._current)
            self._current = None

    def handle_data(self, data: str) -> None:
        if self._current is not None:
            self._current["name"] += data


class TimetoscoreClient:
    def __init__(self) -> None:
        self.cache: dict[str, tuple[float, Any]] = {}
        self._cache_lock = RLock()
        self._warm_lock = RLock()
        self._warming: set[str] = set()
        self._game_center_config: dict[str, str] | None = None
        self._game_center_config_error: str | None = None
        self._game_center_config_error_at = 0.0
        self._api_lock = RLock()
        self._api_request_lock = RLock()
        self._last_api_request_at = 0.0
        self._api_cookiejar = http.cookiejar.CookieJar()
        self._api_opener = build_opener(HTTPCookieProcessor(self._api_cookiejar))
        self._api_proxy_session: str | None = None

    def _cache_get(self, key: str) -> Any | None:
        with self._cache_lock:
            hit = self.cache.get(key)
            if not hit:
                return None
            created_at, value = hit
            if time.time() - created_at > CACHE_TTL_SECONDS:
                self.cache.pop(key, None)
                return None
            return value

    def _cache_set(self, key: str, value: Any) -> Any:
        with self._cache_lock:
            self.cache[key] = (time.time(), value)
        return value

    def fetch(self, path: str, params: dict[str, str | int | None]) -> str:
        clean_params = {key: str(value) for key, value in params.items() if value not in (None, "")}
        url = urljoin(BASE_URL, path)
        if clean_params:
            url = f"{url}?{urlencode(clean_params)}"
        cached = self._cache_get(url)
        if cached is not None:
            return cached
        req = Request(url, headers={"User-Agent": "OaklandHockeyStats/0.1"})
        try:
            with urlopen(req, timeout=12) as response:
                body = response.read().decode("utf-8", errors="replace")
        except URLError as exc:
            reason = getattr(exc, "reason", None)
            if isinstance(reason, ssl.SSLCertVerificationError):
                with urlopen(req, timeout=12, context=ssl._create_unverified_context()) as response:
                    body = response.read().decode("utf-8", errors="replace")
            else:
                raise RuntimeError(f"Could not fetch TimeToScore data from {url}: {exc}") from exc
        except (HTTPError, URLError, TimeoutError) as exc:
            raise RuntimeError(f"Could not fetch TimeToScore data from {url}: {exc}") from exc
        return self._cache_set(url, body)

    def fetch_url(self, url: str) -> str:
        cached = self._cache_get(url)
        if cached is not None:
            return cached
        req = Request(url, headers={"User-Agent": "OaklandHockeyStats/0.1"})
        try:
            with urlopen(req, timeout=12) as response:
                body = response.read().decode("utf-8", errors="replace")
        except URLError as exc:
            reason = getattr(exc, "reason", None)
            if isinstance(reason, ssl.SSLCertVerificationError):
                with urlopen(req, timeout=12, context=ssl._create_unverified_context()) as response:
                    body = response.read().decode("utf-8", errors="replace")
            else:
                raise RuntimeError(f"Could not fetch TimeToScore data from {url}: {exc}") from exc
        except (HTTPError, URLError, TimeoutError) as exc:
            raise RuntimeError(f"Could not fetch TimeToScore data from {url}: {exc}") from exc
        return self._cache_set(url, body)

    def api_bootstrap(self) -> str:
        with self._api_lock:
            if self._api_proxy_session:
                return self._api_proxy_session
            url = urljoin(BASE_URL, f"{API_BOOTSTRAP_PATH}?{urlencode({'league': LEAGUE_ID})}")
            req = Request(url, headers={"User-Agent": "OaklandHockeyStats/0.1"})
            try:
                with self._api_opener.open(req, timeout=12) as response:
                    html = response.read().decode("utf-8", errors="replace")
            except URLError as exc:
                reason = getattr(exc, "reason", None)
                if isinstance(reason, ssl.SSLCertVerificationError):
                    self._api_cookiejar = http.cookiejar.CookieJar()
                    self._api_opener = build_opener(
                        HTTPCookieProcessor(self._api_cookiejar),
                        HTTPSHandler(context=ssl._create_unverified_context()),
                    )
                    with self._api_opener.open(req, timeout=12) as response:
                        html = response.read().decode("utf-8", errors="replace")
                else:
                    raise RuntimeError(f"Could not bootstrap TimeToScore API proxy from {url}: {exc}") from exc
            match = re.search(r'data-proxy-session="([^"]+)"', html)
            if not match:
                raise RuntimeError("Could not read TimeToScore API proxy session")
            self._api_proxy_session = match.group(1)
            return self._api_proxy_session

    def api_throttle(self) -> None:
        with self._api_request_lock:
            elapsed = time.time() - self._last_api_request_at
            if elapsed < API_MIN_INTERVAL_SECONDS:
                time.sleep(API_MIN_INTERVAL_SECONDS - elapsed)
            self._last_api_request_at = time.time()

    def api(self, endpoint: str, params: dict[str, str | int | None], retry: bool = True) -> dict[str, Any]:
        clean_params = {
            key: str(value)
            for key, value in params.items()
            if value not in (None, "", -1)
        }
        query = urlencode({"endpoint": endpoint, **clean_params})
        cache_key = f"api:{endpoint}:{query}"
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached

        session = self.api_bootstrap()
        url = urljoin(BASE_URL, f"{API_PROXY_PATH}?{query}")
        req = Request(
            url,
            headers={
                "User-Agent": "OaklandHockeyStats/0.1",
                "X-Proxy-Session": session,
                "Referer": urljoin(BASE_URL, f"{API_BOOTSTRAP_PATH}?{urlencode({'league': LEAGUE_ID})}"),
            },
        )
        try:
            self.api_throttle()
            with self._api_opener.open(req, timeout=20) as response:
                payload = json.loads(response.read().decode("utf-8", errors="replace"))
        except HTTPError as exc:
            if retry and exc.code == HTTPStatus.UNAUTHORIZED:
                self._api_proxy_session = None
                return self.api(endpoint, params, retry=False)
            if retry and exc.code == HTTPStatus.TOO_MANY_REQUESTS:
                retry_after = int_or_none(exc.headers.get("Retry-After", "")) or API_RATE_LIMIT_RETRY_SECONDS
                time.sleep(max(retry_after, API_RATE_LIMIT_RETRY_SECONDS))
                return self.api(endpoint, params, retry=False)
            raise RuntimeError(f"TimeToScore API {endpoint} returned HTTP {exc.code}") from exc
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Could not fetch TimeToScore API {endpoint}: {exc}") from exc
        error = api_payload_error(endpoint, payload)
        if error:
            raise RuntimeError(error)
        return self._cache_set(cache_key, payload)

    def league_metadata(self) -> dict[str, Any]:
        payload = self.api("get_leagues", {"league_id": LEAGUE_ID})
        leagues = payload.get("leagues") if isinstance(payload, dict) else None
        if not isinstance(leagues, list) or not leagues:
            raise RuntimeError("TimeToScore API get_leagues returned no leagues")
        return leagues[0]

    def current_season_id(self) -> str:
        return str(self.league_metadata().get("current_season") or "0")

    def game_center_config(self, game_id: str) -> dict[str, str]:
        if self._game_center_config:
            return self._game_center_config
        if self._game_center_config_error and time.time() - self._game_center_config_error_at < GAME_CENTER_CONFIG_ERROR_TTL_SECONDS:
            raise RuntimeError(self._game_center_config_error)
        self._game_center_config_error = None
        self._game_center_config_error_at = 0.0
        html = self.fetch_url(f"{GAME_CENTER_BOOTSTRAP_URL}?{urlencode({'game_id': game_id, 'mode': 'display'})}")
        config: dict[str, str] = {}
        for key in ("username", "secret", "api_url", "league_id"):
            match = re.search(rf'"{key}"\s*:\s*"([^"]+)"', html)
            if match:
                config[key] = match.group(1)
        missing = {"username", "secret", "api_url", "league_id"} - set(config)
        if missing:
            self._game_center_config_error = f"Could not read TimeToScore game-center config: missing {', '.join(sorted(missing))}"
            self._game_center_config_error_at = time.time()
            raise RuntimeError(self._game_center_config_error)
        self._game_center_config = config
        return config

    def game_center(self, game_id: str, season: str = "0", game: dict[str, Any] | None = None) -> dict[str, Any]:
        game_id = clean_text(str(game_id))
        if not game_id:
            raise RuntimeError("Missing game_id for game center")
        try:
            payload = self.api("get_game_center", {"game_id": game_id, "stat_class": "1"})
            if isinstance(payload, dict) and isinstance(payload.get("game_center"), dict):
                payload = payload["game_center"]
            return normalize_game_center(payload, game_id, season, game or {})
        except RuntimeError:
            pass

        config = self.game_center_config(game_id)
        timestamp = str(int(time.time()))
        params = {
            "auth_key": config["username"],
            "auth_timestamp": timestamp,
            "body_md5": hashlib.md5(b"").hexdigest(),
            "game_id": game_id,
            "league_id": config["league_id"],
            "season_id": season,
            "widget": "gamecenter",
        }
        query_without_signature = urlencode(params)
        canonical = f"GET\n{GAME_CENTER_PATH}\n{query_without_signature}"
        signature = hmac.new(config["secret"].encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()
        url = f"https://{config['api_url']}{GAME_CENTER_PATH}?{query_without_signature}&auth_signature={signature}"
        payload = json.loads(self.fetch_url(url))
        if isinstance(payload, dict) and isinstance(payload.get("game_center"), dict):
            payload = payload["game_center"]
        return normalize_game_center(payload, game_id, season, game or {})

    def tables(self, path: str, params: dict[str, str | int | None]) -> list[list[list[Cell]]]:
        html = self.fetch(path, params)
        parser = TableParser()
        parser.feed(html)
        return parser.tables

    def seasons(self) -> list[dict[str, Any]]:
        try:
            league = self.league_metadata()
            current_season = str(league.get("current_season") or "")
            seasons: list[dict[str, Any]] = [{"id": "0", "name": "Current", "current": True}]
            for row in league.get("seasons", []):
                if not isinstance(row, dict):
                    continue
                season_id = str(row.get("season_id", ""))
                if not season_id or season_id == current_season:
                    continue
                seasons.append(
                    {
                        "id": season_id,
                        "name": clean_text(str(row.get("season_name", ""))) or season_id,
                        "current": False,
                    }
                )
            return [season for season in seasons if is_supported_season(season)]
        except RuntimeError:
            pass

        html = self.fetch("display-stats.php", {"league": LEAGUE_ID})
        parser = OptionParser()
        parser.feed(html)
        seasons: list[dict[str, Any]] = []
        for option in parser.options:
            season_id = option["id"]
            if season_id == "0":
                seasons.append({"id": "0", "name": "Current", "current": bool(option["current"])})
            else:
                seasons.append({"id": season_id, "name": option["name"], "current": bool(option["current"])})
        return [season for season in seasons if is_supported_season(season)]

    def standings(self, season: str = "0") -> dict[str, Any]:
        cache_key = f"standings:{season}"
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached

        api_error: RuntimeError | None = None
        try:
            payload = self.api(
                "get_standings",
                {"league_id": LEAGUE_ID, "season_id": season or "0", "stat_class": "1"},
            )
            normalized = normalize_api_standings(payload, season, self.seasons())
            return self._cache_set(cache_key, normalized)
        except RuntimeError as exc:
            api_error = exc

        tables = self.tables("display-stats.php", {"league": LEAGUE_ID, "season": season})
        divisions: list[dict[str, Any]] = []
        current: dict[str, Any] | None = None
        resolved_season = season

        for row in tables[0] if tables else []:
            texts = [cell.text for cell in row]
            first = texts[0] if texts else ""
            link = row[0].links[0]["href"] if row and row[0].links else ""

            if link and "display-schedule" in link and first.endswith("Schedule"):
                query = parse_query(link)
                if "level" not in query:
                    resolved_season = query.get("season", resolved_season)
                    continue
                current = {
                    "id": f"{query.get('level', '0')}:{query.get('conf', '0')}",
                    "name": first.removesuffix(" Schedule"),
                    "level": query.get("level", "0"),
                    "conf": query.get("conf", "0"),
                    "season": query.get("season", resolved_season),
                    "schedule_url": absolutize(link),
                    "stats_url": None,
                    "teams": [],
                }
                divisions.append(current)
                continue

            if link and "display-league-stats" in link and current is not None:
                current["stats_url"] = absolutize(link)
                query = parse_query(link)
                current["level"] = query.get("level", current["level"])
                current["conf"] = query.get("conf", current["conf"])
                current["season"] = query.get("season", current["season"])
                continue

            if len(row) >= 7 and row[0].links and "display-schedule" in row[0].links[0]["href"] and current:
                query = parse_query(row[0].links[0]["href"])
                current["teams"].append(
                    {
                        "id": query.get("team", ""),
                        "name": row[0].text,
                        "slug": slugify(row[0].text),
                        "schedule_url": absolutize(row[0].links[0]["href"]),
                        "gp": int_or_none(row[1].text),
                        "wins": int_or_none(row[2].text),
                        "losses": int_or_none(row[3].text),
                        "ties": int_or_none(row[4].text),
                        "points": int_or_none(row[5].text),
                        "games_remaining": int_or_none(row[6].text),
                        "division": current["name"],
                        "level": current["level"],
                        "conf": current["conf"],
                    }
                )

        self.add_standings_goal_totals(divisions, resolved_season or season)

        if api_error and not divisions:
            raise RuntimeError(f"Could not fetch standings for season {season}: {api_error}") from api_error

        result = {
            "league_id": LEAGUE_ID,
            "requested_season": season,
            "season": resolved_season,
            "history_start_year": HISTORY_START_YEAR,
            "seasons": self.seasons(),
            "divisions": divisions,
        }
        return self._cache_set(cache_key, result)

    def add_standings_goal_totals(self, divisions: list[dict[str, Any]], season: str) -> None:
        teams_by_id: dict[str, dict[str, Any]] = {}
        teams_by_name: dict[str, dict[str, Any]] = {}
        for division in divisions:
            for team in division.get("teams", []):
                team["goals_for"] = 0
                team["goals_against"] = 0
                team["goal_diff"] = 0
                if team.get("id"):
                    teams_by_id[str(team["id"])] = team
                teams_by_name[str(team.get("name", ""))] = team

        try:
            games = self.schedule(season).get("games", [])
        except RuntimeError:
            return

        def apply_result(team_id: str, team_name: str, goals_for: int, goals_against: int) -> None:
            team = teams_by_id.get(team_id) or teams_by_name.get(team_name)
            if not team:
                return
            team["goals_for"] += goals_for
            team["goals_against"] += goals_against
            team["goal_diff"] = team["goals_for"] - team["goals_against"]

        for game in games:
            away_goals = game.get("away_goals")
            home_goals = game.get("home_goals")
            if not game.get("final") or not isinstance(away_goals, int) or not isinstance(home_goals, int):
                continue
            apply_result(str(game.get("away_team_id", "")), str(game.get("away_team", "")), away_goals, home_goals)
            apply_result(str(game.get("home_team_id", "")), str(game.get("home_team", "")), home_goals, away_goals)

    def api_season_stats(self, season: str, stat_class: str = "1") -> dict[str, list[dict[str, Any]]]:
        resolved_season = self.current_season_id() if str(season) == "0" else str(season)
        cache_key = f"api-season-stats:{resolved_season}:{stat_class}"
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached

        skater_payload = self.api(
            "get_skaters",
            {"league_id": LEAGUE_ID, "season_id": resolved_season, "stat_class": stat_class},
        )
        goalie_payload = self.api(
            "get_goalies",
            {"league_id": LEAGUE_ID, "season_id": resolved_season, "stat_class": stat_class},
        )
        return self._cache_set(
            cache_key,
            {
                "players": normalize_api_skaters(skater_payload),
                "goalies": normalize_api_goalies(goalie_payload),
            },
        )

    def division_stats(self, season: str, level: str, conf: str = "0", stat_class: str = "1") -> dict[str, Any]:
        api_error: RuntimeError | None = None
        try:
            resolved_season = self.current_season_id() if str(season) == "0" else str(season)
            standings = self.standings(resolved_season)
            division = next(
                (
                    division
                    for division in standings.get("divisions", [])
                    if str(division.get("level")) == str(level) and str(division.get("conf", "0")) == str(conf)
                ),
                None,
            )
            if not division:
                raise RuntimeError(f"Division {level}:{conf} not found for season {resolved_season}")
            team_ids = {str(team.get("id", "")) for team in division.get("teams", []) if team.get("id")}
            team_names = {normalize_name(str(team.get("name", ""))) for team in division.get("teams", []) if team.get("name")}
            season_stats = self.api_season_stats(resolved_season, stat_class)

            def in_division(row: dict[str, Any]) -> bool:
                return str(row.get("team_id", "")) in team_ids or normalize_name(str(row.get("team", ""))) in team_names

            context = {"level": level, "conf": conf}
            result = {
                "players": [{**row, **context} for row in season_stats["players"] if in_division(row)],
                "goalies": [{**row, **context} for row in season_stats["goalies"] if in_division(row)],
            }
            result["players"] = remove_goalie_overlap_from_skaters(result["players"], result["goalies"])
            result["players"].sort(key=lambda row: (row.get("points") or 0, row.get("goals") or 0), reverse=True)
            result["goalies"].sort(key=lambda row: (row.get("save_pct") or 0, row.get("gp") or 0), reverse=True)
            return result
        except RuntimeError as exc:
            api_error = exc

        tables = self.tables(
            "display-league-stats",
            {"stat_class": stat_class, "league": LEAGUE_ID, "season": season, "level": level, "conf": conf},
        )
        result = {"players": [], "goalies": []}
        for table in tables:
            title = table[0][0].text if table and table[0] else ""
            if title == "Player Stats":
                result["players"] = parse_records(table, context={"level": level, "conf": conf})
            elif title == "Goalie Stats":
                result["goalies"] = parse_records(table, context={"level": level, "conf": conf})
        result["players"] = remove_goalie_overlap_from_skaters(result["players"], result["goalies"])
        if api_error and not result["players"] and not result["goalies"]:
            raise RuntimeError(f"Could not fetch division stats for season {season}, level {level}, conf {conf}: {api_error}") from api_error
        return result

    def season_player_index(self, season: dict[str, str], stat_class: str = "1") -> dict[str, Any]:
        cache_key = f"season-player-index:{season['id']}:{stat_class}"
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached

        standings = self.standings("0") if season["id"] == "0" else self.standings(season["id"])
        indexed_players: list[dict[str, Any]] = []
        indexed_goalies: list[dict[str, Any]] = []
        divisions = standings["divisions"]

        def load_division(division: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
            stats = self.division_stats(division["season"], division["level"], division["conf"], stat_class=stat_class)
            return division, stats

        loaded_divisions: list[tuple[int, dict[str, Any], dict[str, Any]]] = []
        max_workers = min(8, max(1, len(divisions)))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(load_division, division): index for index, division in enumerate(divisions)}
            for future in as_completed(futures):
                try:
                    division, stats = future.result()
                except RuntimeError:
                    continue
                loaded_divisions.append((futures[future], division, stats))

        for _, division, stats in sorted(loaded_divisions, key=lambda item: item[0]):
            team_by_name = {team["name"]: team for team in division["teams"]}
            context = {
                "season": season["name"],
                "season_id": division["season"],
                "division": division["name"],
                "division_id": division["id"],
            }
            for player in stats["players"]:
                team = team_by_name.get(str(player.get("team", "")))
                indexed_players.append({**player, **context, "team_id": team.get("id", "") if team else ""})
            for goalie in stats["goalies"]:
                team = team_by_name.get(str(goalie.get("team", "")))
                indexed_goalies.append({**goalie, **context, "team_id": team.get("id", "") if team else ""})

        return self._cache_set(cache_key, {"players": indexed_players, "goalies": indexed_goalies})

    def player_history_seasons(self) -> list[dict[str, str]]:
        current = self.standings("0")
        seen_season_ids: set[str] = {current["season"]}
        season_rows = [{"id": current["season"], "name": "Current"}]
        for season in self.seasons():
            season_id = season["id"]
            if season_id == "0" or season_id in seen_season_ids:
                continue
            seen_season_ids.add(season_id)
            season_rows.append({"id": season_id, "name": season["name"]})
        return [season for season in season_rows if is_supported_season(season)]

    def prewarm_player_history(self, season_type: str = "regular") -> dict[str, Any]:
        stat_class = "2" if season_type == "playoffs" else "1"
        cache_key = f"player-history-warm:{stat_class}:{HISTORY_START_YEAR}"
        if self._cache_get(cache_key):
            return {"status": "warm", "season_type": season_type}

        with self._warm_lock:
            if cache_key in self._warming:
                return {"status": "warming", "season_type": season_type}
            self._warming.add(cache_key)

        thread = Thread(target=self._prewarm_player_history_worker, args=(cache_key, stat_class, season_type), daemon=True)
        thread.start()
        return {"status": "warming", "season_type": season_type}

    def _prewarm_player_history_worker(self, cache_key: str, stat_class: str, season_type: str) -> None:
        try:
            warmed = 0
            for season in self.player_history_seasons():
                try:
                    self.season_player_index(season, stat_class=stat_class)
                    warmed += 1
                except RuntimeError:
                    continue
            self._cache_set(cache_key, {"season_type": season_type, "warmed_seasons": warmed})
        finally:
            with self._warm_lock:
                self._warming.discard(cache_key)

    def player_profile(self, name: str, season_type: str = "regular", team_id: str = "") -> dict[str, Any]:
        target_name = normalize_name(name)
        if not target_name:
            raise ValueError("Player name is required")

        stat_class = "2" if season_type == "playoffs" else "1"
        target_keys = name_identity_keys(name)
        cache_key = f"player:{sorted(target_keys)}:{stat_class}:{HISTORY_START_YEAR}"
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached

        skater_rows: list[dict[str, Any]] = []
        goalie_rows: list[dict[str, Any]] = []
        searchable_seasons = self.player_history_seasons()
        found_player = False
        consecutive_misses = 0
        for season in searchable_seasons:
            before_count = len(skater_rows) + len(goalie_rows)
            try:
                index = self.season_player_index(season, stat_class=stat_class)
            except RuntimeError:
                continue

            for player in index["players"]:
                if target_keys & name_identity_keys(str(player.get("name", ""))):
                    skater_rows.append(player)
            for goalie in index["goalies"]:
                if target_keys & name_identity_keys(str(goalie.get("name", ""))):
                    goalie_rows.append(goalie)

            after_count = len(skater_rows) + len(goalie_rows)
            if after_count > before_count:
                found_player = True
                consecutive_misses = 0
            elif found_player:
                consecutive_misses += 1
                if consecutive_misses >= 8:
                    break

        payload = {
            "name": name,
            "season_type": "playoffs" if stat_class == "2" else "regular",
            "history_start_year": HISTORY_START_YEAR,
            "identity_keys": sorted(target_keys),
            "available_splits": ["regular", "playoffs"],
            "skater_seasons": skater_rows,
            "goalie_seasons": goalie_rows,
            "skater_career": career_totals(skater_rows, mode="skater"),
            "goalie_career": career_totals(goalie_rows, mode="goalie"),
        }
        return self._cache_set(cache_key, payload)

    def schedule(self, season: str = "0", team: str | None = None, level: str | None = None, conf: str | None = None) -> dict[str, Any]:
        api_error: RuntimeError | None = None
        try:
            resolved_season = self.current_season_id() if str(season) == "0" else str(season)
            payload = self.api(
                "get_schedule_lite",
                {
                    "league_id": LEAGUE_ID,
                    "season_id": resolved_season,
                    "level_id": level,
                    "conf_id": conf,
                    "team_id": team,
                },
            )
            divisions = self.standings(season).get("divisions", [])
            return {
                "season": resolved_season,
                "team": team,
                "level": level,
                "games": normalize_api_schedule(payload, resolved_season, divisions),
            }
        except RuntimeError as exc:
            api_error = exc

        params: dict[str, str | int | None] = {"stat_class": 1, "league": LEAGUE_ID, "season": season}
        if team:
            params["team"] = team
        if level:
            params["level"] = level
            params["conf"] = conf or "0"
        tables = self.tables("display-schedule", params)
        games: list[dict[str, Any]] = []
        for table in tables:
            title = table[0][0].text if table and table[0] else ""
            if title == "Game Results":
                games.extend(parse_games_table(table))
        if api_error and not games and not team and not level:
            raise RuntimeError(f"Could not fetch schedule for season {season}: {api_error}") from api_error
        return {"season": season, "team": team, "level": level, "games": games}

    def team(self, season: str, team_id: str, stat_class: str = "1") -> dict[str, Any]:
        tables = self.tables("display-schedule", {"team": team_id, "season": season, "league": LEAGUE_ID, "stat_class": stat_class})
        payload: dict[str, Any] = {
            "season": season,
            "team_id": team_id,
            "games": [],
            "players": [],
            "goalies": [],
            "team_stats": [],
            "special_teams": [],
        }
        for table in tables:
            title = table[0][0].text if table and table[0] else ""
            if title == "Game Results":
                payload["games"] = parse_games_table(table)
            elif title == "Player Stats":
                payload["players"] = parse_records(table, context={"team_id": team_id})
            elif title == "Goalie Stats":
                payload["goalies"] = parse_records(table, context={"team_id": team_id})
            elif title == "Team Stats":
                payload["team_stats"] = parse_records(table)
            elif title == "Special Teams":
                payload["special_teams"] = parse_records(table)
        payload["players"] = remove_goalie_overlap_from_skaters(payload["players"], payload["goalies"])
        linked_names = []
        for game in payload["games"]:
            if game["away_team_id"] == team_id:
                linked_names.append(game["away_team"])
            if game["home_team_id"] == team_id:
                linked_names.append(game["home_team"])
        names = linked_names
        if not names:
            team_counts = Counter(
                clean_text(team_name)
                for game in payload["games"]
                for team_name in (game.get("away_team"), game.get("home_team"))
                if team_name
            )
            names = [team_counts.most_common(1)[0][0]] if team_counts else []
        if not names and payload["players"]:
            names.append(payload["players"][0].get("team", ""))
        payload["team_name"] = clean_text(names[0]) if names else ""
        return payload


def api_value(source: dict[str, Any], keys: list[str], default: Any = "") -> Any:
    for key in keys:
        value = source.get(key)
        if value not in (None, ""):
            return value
    return default


def api_int(source: dict[str, Any], keys: list[str]) -> int | None:
    return int_or_none(str(api_value(source, keys)))


def api_float(source: dict[str, Any], keys: list[str]) -> float | None:
    return float_or_none(str(api_value(source, keys)))


def normalize_api_standings(payload: dict[str, Any], requested_season: str, seasons: list[dict[str, Any]]) -> dict[str, Any]:
    standings = payload.get("standings") if isinstance(payload.get("standings"), dict) else {}
    leagues = standings.get("leagues") if isinstance(standings.get("leagues"), list) else []
    if not leagues:
        raise RuntimeError("TimeToScore API get_standings returned no standings")
    league = leagues[0]
    resolved_season = str(league.get("season") or requested_season)
    divisions: list[dict[str, Any]] = []

    for level in league.get("levels", []):
        if not isinstance(level, dict):
            continue
        level_id = str(level.get("id") or level.get("level_id") or "")
        level_name = clean_text(str(level.get("name") or level.get("level_name") or "Division"))
        conferences = level.get("conferences") if isinstance(level.get("conferences"), list) else []
        if not conferences:
            conferences = [{"id": "0", "teams": []}]
        for conference in conferences:
            if not isinstance(conference, dict):
                continue
            conf_id = str(conference.get("id") if conference.get("id") not in (None, "") else "0")
            conf_name = clean_text(str(conference.get("conf_name") or conference.get("name") or ""))
            division_name = f"{level_name} {conf_name}".strip()
            teams = []
            for team in conference.get("teams", []):
                if not isinstance(team, dict):
                    continue
                name = clean_text(str(team.get("team_name") or team.get("name") or ""))
                goals_for = api_int(team, ["goals_for"])
                goals_against = api_int(team, ["goals_against"])
                goal_diff = api_int(team, ["plusminus"])
                if goal_diff is None and goals_for is not None and goals_against is not None:
                    goal_diff = goals_for - goals_against
                teams.append(
                    {
                        "id": str(team.get("id") or team.get("team_id") or ""),
                        "name": name,
                        "slug": slugify(name),
                        "schedule_url": absolutize(f"/test/schedule.php?league={LEAGUE_ID}&team={team.get('id') or team.get('team_id') or ''}"),
                        "gp": api_int(team, ["games_played"]),
                        "wins": api_int(team, ["wins", "total_wins"]),
                        "losses": api_int(team, ["losses"]),
                        "ties": api_int(team, ["ties"]),
                        "points": api_int(team, ["pts", "points"]),
                        "games_remaining": api_int(team, ["games_remaining"]),
                        "goals_for": goals_for or 0,
                        "goals_against": goals_against or 0,
                        "goal_diff": goal_diff or 0,
                        "division": division_name,
                        "level": level_id,
                        "conf": conf_id,
                    }
                )
            divisions.append(
                {
                    "id": f"{level_id}:{conf_id}",
                    "name": division_name,
                    "level": level_id,
                    "conf": conf_id,
                    "season": resolved_season,
                    "schedule_url": absolutize(f"/test/schedule.php?league={LEAGUE_ID}&season={resolved_season}&level={level_id}&conf={conf_id}"),
                    "stats_url": absolutize(f"/test/skater-leaders.php?league={LEAGUE_ID}&season={resolved_season}&level={level_id}"),
                    "teams": teams,
                }
            )

    return {
        "league_id": LEAGUE_ID,
        "requested_season": requested_season,
        "season": resolved_season,
        "history_start_year": HISTORY_START_YEAR,
        "seasons": seasons,
        "divisions": divisions,
    }


def normalize_api_player_team_rows(player: dict[str, Any]) -> list[dict[str, Any]]:
    teams = player.get("teams")
    if isinstance(teams, list) and teams:
        return [team for team in teams if isinstance(team, dict)]
    return [player]


def normalize_api_skaters(payload: dict[str, Any], context: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    context = context or {}
    if "skaters" not in payload:
        raise RuntimeError("TimeToScore API get_skaters returned no skaters key")
    skaters = payload.get("skaters") if isinstance(payload.get("skaters"), list) else []
    rows: list[dict[str, Any]] = []
    for player in skaters:
        if not isinstance(player, dict):
            continue
        name = display_person_name(str(player.get("player_name") or ""))
        if not name:
            continue
        for team_stats in normalize_api_player_team_rows(player):
            team_name = clean_text(str(api_value(team_stats, ["team_name", "team_ab"], api_value(player, ["team_name", "team_ab"]))))
            row = {
                **context,
                "name": name,
                "team": team_name,
                "team_id": str(api_value(team_stats, ["team_id"], api_value(player, ["team_id"], ""))),
                "number": clean_text(str(api_value(team_stats, ["jersey"], api_value(player, ["jersey"], "")))),
                "position": clean_text(str(api_value(team_stats, ["position"], api_value(player, ["position"], "")))),
                "gp": api_int(team_stats, ["games_played"]) or 0,
                "goals": api_int(team_stats, ["goals"]) or 0,
                "assists": api_int(team_stats, ["assists"]) or 0,
                "points": api_int(team_stats, ["points"]) or 0,
                "plus_minus": api_int(team_stats, ["plusminus"]) or 0,
                "pims": api_int(team_stats, ["pims"]) or 0,
                "hat": api_int(team_stats, ["hat", "hat_tricks"]) or 0,
                "shots": api_int(team_stats, ["sog", "shots"]) if api_int(team_stats, ["sog", "shots"]) is not None else "-",
                "points_per_game": api_float(team_stats, ["point_per_game", "points_per_game"]) or 0,
                "ppg": api_int(team_stats, ["ppg"]) or 0,
                "ppa": api_int(team_stats, ["ppa"]) or 0,
                "shg": api_int(team_stats, ["shg"]) or 0,
                "sha": api_int(team_stats, ["sha"]) or 0,
                "gwg": api_int(team_stats, ["gwg"]) or 0,
                "otg": api_int(team_stats, ["otg"]) or 0,
                "player_id": str(api_value(player, ["player_id"], slugify(f"{name} {team_name}"))),
            }
            rows.append(row)
    return rows


def normalize_api_goalies(payload: dict[str, Any], context: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    context = context or {}
    if "goalies" not in payload:
        raise RuntimeError("TimeToScore API get_goalies returned no goalies key")
    goalies = payload.get("goalies") if isinstance(payload.get("goalies"), list) else []
    rows: list[dict[str, Any]] = []
    for player in goalies:
        if not isinstance(player, dict):
            continue
        name = display_person_name(str(player.get("player_name") or ""))
        if not name:
            continue
        for team_stats in normalize_api_player_team_rows(player):
            team_name = clean_text(str(api_value(team_stats, ["team_name", "team_ab"], api_value(player, ["team_name", "team_ab"]))))
            row = {
                **context,
                "name": name,
                "team": team_name,
                "team_id": str(api_value(team_stats, ["team_id"], api_value(player, ["team_id"], ""))),
                "number": clean_text(str(api_value(team_stats, ["jersey"], api_value(player, ["jersey"], "")))),
                "position": "G",
                "gp": api_int(team_stats, ["goalie_games_played", "goalie_games", "games_played"]) or 0,
                "wins": api_int(team_stats, ["wins"]) or 0,
                "losses": api_int(team_stats, ["losses"]) or 0,
                "ties": api_int(team_stats, ["so_ties", "ties"]) or 0,
                "overtime_losses": api_int(team_stats, ["ot_losses", "otlosses"]) or 0,
                "shots": api_int(team_stats, ["shots_against", "shots"]) or 0,
                "saves": api_int(team_stats, ["saves"]) or 0,
                "goals_against": api_int(team_stats, ["goals_against"]) or 0,
                "goals_against_average": api_float(team_stats, ["goals_against_ave", "goals_against_average"]) or 0,
                "save_pct": api_float(team_stats, ["save_pct"]) or 0,
                "shutouts": api_int(team_stats, ["shutouts"]) or 0,
                "pims": api_int(team_stats, ["pims"]) or 0,
                "time_on_ice": clean_text(str(api_value(team_stats, ["toi"], ""))),
                "player_id": str(api_value(player, ["player_id"], slugify(f"{name} {team_name} goalie"))),
            }
            rows.append(row)
    return rows


def format_api_schedule_date(value: Any) -> str:
    text = clean_text(str(value or ""))
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d")
        return f"{parsed.strftime('%a %b')} {parsed.day}"
    except ValueError:
        return text


def normalize_api_schedule(payload: dict[str, Any], season: str, divisions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if "games" not in payload:
        raise RuntimeError("TimeToScore API get_schedule_lite returned no games key")
    games = payload.get("games") if isinstance(payload.get("games"), list) else []
    team_context_by_id: dict[str, dict[str, Any]] = {}
    team_context_by_name: dict[str, dict[str, Any]] = {}
    for division in divisions:
        for team in division.get("teams", []):
            context = {"division": division.get("name", ""), "level": division.get("level", ""), "conf": division.get("conf", "")}
            team_id = str(team.get("id", ""))
            team_name = clean_text(str(team.get("name", "")))
            if team_id:
                team_context_by_id[team_id] = context
            if team_name:
                team_context_by_name[normalize_name(team_name)] = context

    normalized: list[dict[str, Any]] = []
    for game in games:
        if not isinstance(game, dict):
            continue
        away_team = clean_text(str(game.get("away_team") or ""))
        home_team = clean_text(str(game.get("home_team") or ""))
        away_id = str(game.get("away_id") or "")
        home_id = str(game.get("home_id") or "")
        away_context = team_context_by_id.get(away_id) or team_context_by_name.get(normalize_name(away_team), {})
        home_context = team_context_by_id.get(home_id) or team_context_by_name.get(normalize_name(home_team), {})
        level_name = away_context.get("division") or home_context.get("division") or ""
        away_goals = int_or_none(str(game.get("away_goals", "")))
        home_goals = int_or_none(str(game.get("home_goals", "")))
        final = clean_text(str(game.get("result_string") or game.get("game_status") or "")).casefold() in {"final", "closed"} or (
            away_goals is not None and home_goals is not None
        )
        game_id = clean_text(str(game.get("game_id") or ""))
        normalized.append(
            {
                "game_id": game_id,
                "final": final,
                "date": format_api_schedule_date(game.get("date")),
                "time": clean_text(str(game.get("formatted_time") or game.get("time") or "")),
                "rink": clean_text(str(game.get("location") or "")),
                "league": LEAGUE_ID,
                "level": level_name,
                "away_team": away_team,
                "away_team_id": away_id,
                "away_goals": away_goals,
                "home_team": home_team,
                "home_team_id": home_id,
                "home_goals": home_goals,
                "type": clean_text(str(game.get("gtype_name") or "")),
                "game_center_url": absolutize(f"/oss-scoresheet?game_id={game_id}&mode=display") if game_id else None,
                "scorecard_url": absolutize(f"/generate-scorecard.php?game_id={game_id}") if game_id else None,
                "season": season,
            }
        )
    return normalized


def normalize_header(value: str) -> str:
    mapping = {
        "#": "number",
        "ass.": "assists",
        "min": "pims",
        "pims": "pims",
        "+/-": "plus_minus",
        "save %": "save_pct",
        "pts/game": "points_per_game",
        "pts": "points",
        "goals": "goals",
        "gp": "gp",
        "ga": "goals_against",
        "gaa": "goals_against_average",
        "so": "shutouts",
        "toi": "time_on_ice",
        "w": "wins",
        "l": "losses",
        "otl": "overtime_losses",
        "sol": "shootout_losses",
        "rw": "regulation_wins",
        "otw": "overtime_wins",
        "sow": "shootout_wins",
        "tie": "ties",
    }
    key = clean_text(value).lower()
    if key in mapping:
        return mapping[key]
    return re.sub(r"[^a-z0-9]+", "_", key).strip("_")


def find_team(divisions: list[dict[str, Any]], team_id: str) -> dict[str, Any] | None:
    for division in divisions:
        for team in division.get("teams", []):
            if team.get("id") == team_id:
                return team
    return None


def first_game_level(games: list[dict[str, Any]]) -> str:
    for game in games:
        if game.get("level"):
            return game["level"]
    return ""


def normalize_name(value: str) -> str:
    return re.sub(r"\s+", " ", clean_text(value)).casefold()


def display_person_name(value: str) -> str:
    value = clean_text(value)
    letters = re.findall(r"[A-Za-z]", value)
    if not value or not letters:
        return value
    if not (all(letter.isupper() for letter in letters) or all(letter.islower() for letter in letters)):
        return value

    def format_word(match: re.Match[str]) -> str:
        word = match.group(0)
        upper = word.upper()
        if upper in {"II", "III", "IV", "V"}:
            return upper
        if upper in {"JR", "SR"}:
            return f"{upper[0]}{upper[1:].lower()}"
        if len(upper) <= 2:
            return upper
        if upper.startswith("MC") and len(upper) > 3:
            return f"Mc{upper[2]}{upper[3:].lower()}"
        if upper.startswith("MAC") and len(upper) > 5:
            return f"Mac{upper[3]}{upper[4:].lower()}"
        return f"{upper[0]}{upper[1:].lower()}"

    return re.sub(r"[A-Za-z]+", format_word, value)


def name_identity_keys(value: str) -> set[str]:
    normalized = normalize_name(value)
    tokens = re.findall(r"[a-z0-9]+", normalized)
    if not tokens:
        return set()
    nickname_map = {"chris": "christopher"}
    tokens = [nickname_map.get(token, token) for token in tokens]
    keys = {" ".join(tokens)}
    if len(tokens) >= 3:
        without_middle_initials = [tokens[0], *[token for token in tokens[1:-1] if len(token) > 1], tokens[-1]]
        keys.add(" ".join(without_middle_initials))
    if len(tokens) >= 2:
        keys.add(f"{tokens[0]} {tokens[-1]}")
    return keys


def role_overlap_key(row: dict[str, Any]) -> tuple[str, str, str, str]:
    division_key = row.get("division_id") or f"{row.get('level', '')}:{row.get('conf', '')}"
    return (
        normalize_name(str(row.get("name", ""))),
        str(row.get("team_id") or row.get("team") or ""),
        str(row.get("season_id") or row.get("season") or ""),
        str(division_key),
    )


def remove_goalie_overlap_from_skaters(players: list[dict[str, Any]], goalies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    goalie_keys: set[tuple[str, str, str, str]] = set()
    for goalie in goalies:
        gp = goalie.get("gp")
        if isinstance(gp, (int, float)) and gp > 0:
            goalie_keys.add(role_overlap_key(goalie))
    return [player for player in players if role_overlap_key(player) not in goalie_keys]


def sum_numeric(rows: list[dict[str, Any]], key: str) -> int | float:
    values = [row.get(key) for row in rows if isinstance(row.get(key), (int, float))]
    if any(isinstance(value, float) for value in values):
        return round(sum(float(value) for value in values), 3)
    return sum(int(value) for value in values)


def career_totals(rows: list[dict[str, Any]], mode: str) -> dict[str, Any]:
    if not rows:
        return {}
    if mode == "goalie":
        shots = sum_numeric(rows, "shots")
        goals_against = sum_numeric(rows, "goals_against")
        save_pct = round((shots - goals_against) / shots, 3) if shots else None
        return {
            "season": "Career",
            "team": "Totals",
            "gp": sum_numeric(rows, "gp"),
            "shots": shots,
            "goals_against": goals_against,
            "goals_against_average": None,
            "save_pct": save_pct,
            "shutouts": sum_numeric(rows, "shutouts"),
        }

    gp = sum_numeric(rows, "gp")
    points = sum_numeric(rows, "points")
    return {
        "season": "Career",
        "team": "Totals",
        "gp": gp,
        "goals": sum_numeric(rows, "goals"),
        "assists": sum_numeric(rows, "assists"),
        "points": points,
        "plus_minus": sum_numeric(rows, "plus_minus"),
        "pims": sum_numeric(rows, "pims"),
        "hat": sum_numeric(rows, "hat"),
        "points_per_game": round(points / gp, 2) if gp else None,
    }


def typed_value(key: str, value: str) -> str | int | float | None:
    value = clean_text(value)
    if value == "":
        return None
    if key == "name":
        return display_person_name(value)
    if key in {"name", "team", "situation", "time", "time_on_ice", "number"}:
        return value
    if key in {"save_pct", "points_per_game", "goals_against_average", "pp", "pk"} or key.endswith("_pct"):
        parsed = float_or_none(value)
        return parsed if parsed is not None else value
    parsed_int = int_or_none(value)
    if parsed_int is not None:
        return parsed_int
    parsed_float = float_or_none(value)
    return parsed_float if parsed_float is not None else value


def parse_records(table: list[list[Cell]], context: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    context = context or {}
    header_index = -1
    headers: list[str] = []
    for index, row in enumerate(table):
        raw = [cell.text for cell in row]
        normalized = [normalize_header(cell.text) for cell in row]
        if "name" in normalized or "team" in normalized or "gp" in normalized or "situation" in normalized:
            header_index = index
            headers = normalized
            break
        if raw and raw[0] == "GP":
            header_index = index
            headers = normalized
            break
    if header_index == -1:
        return []
    if table and table[0] and table[0][0].text == "Team Stats" and headers == ["situation", "time", "goals_for"]:
        headers.append("goals_against")

    records: list[dict[str, Any]] = []
    for row in table[header_index + 1 :]:
        if len(row) < len(headers):
            continue
        if all(not cell.text for cell in row):
            continue
        record: dict[str, Any] = dict(context)
        for key, cell in zip(headers, row):
            if not key:
                continue
            record[key] = typed_value(key, cell.text)
        if "name" in record and record["name"]:
            record["player_id"] = slugify(f"{record.get('name')} {record.get('team', '')} {record.get('number', '')}")
        records.append(record)
    return records


def first_value(source: dict[str, Any], keys: list[str], default: Any = "") -> Any:
    for key in keys:
        value = source.get(key)
        if value not in (None, ""):
            return value
    return default


def seconds_remaining(value: Any) -> int:
    parts = str(value or "").split(":")
    if len(parts) != 2:
        return -1
    try:
        return int(parts[0]) * 60 + int(parts[1])
    except ValueError:
        return -1


def period_sort_value(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        match = re.search(r"\d+", str(value or ""))
        return int(match.group(0)) if match else 0


def period_label(value: Any) -> str:
    period = period_sort_value(value)
    if period == 1:
        return "1st Period"
    if period == 2:
        return "2nd Period"
    if period == 3:
        return "3rd Period"
    if period > 3:
        return f"OT{period - 3}" if period > 4 else "Overtime"
    return clean_text(str(value or "Period"))


def flatten_game_center_events(events: Any) -> list[dict[str, Any]]:
    if isinstance(events, list):
        return [event for event in events if isinstance(event, dict)]
    if not isinstance(events, dict):
        return []
    flattened: list[dict[str, Any]] = []
    for period, period_events in events.items():
        if not isinstance(period_events, list):
            continue
        for event in period_events:
            if isinstance(event, dict):
                flattened.append({"period": event.get("period", period), **event})
    return flattened


def normalize_assist(event: dict[str, Any], name_key: str, total_key: str) -> dict[str, Any] | None:
    name = display_person_name(str(event.get(name_key, "")))
    if not name:
        return None
    return {
        "name": name,
        "total": int_or_none(str(event.get(total_key, ""))),
    }


def normalize_goalie_event(event: dict[str, Any]) -> dict[str, str] | None:
    name = display_person_name(str(first_value(event, ["goalie_player_name", "goalie_name"])))
    if not name:
        return None
    return {
        "name": name,
        "id": clean_text(str(first_value(event, ["goalie_player_id", "goalie_id", "goalie"]))),
    }


def normalize_game_goalies(events: list[dict[str, Any]], game: dict[str, Any]) -> dict[str, list[dict[str, str]]]:
    goalies: dict[str, list[dict[str, str]]] = {"away": [], "home": []}
    seen: dict[str, set[str]] = {"away": set(), "home": set()}
    goalie_changes: list[dict[str, str]] = []
    away_id = str(game.get("away_team_id", ""))
    home_id = str(game.get("home_team_id", ""))

    def add(side: str, goalie: dict[str, str] | None) -> None:
        if not goalie:
            return
        key = goalie.get("id") or goalie.get("name", "").casefold()
        if not key or key in seen[side]:
            return
        seen[side].add(key)
        goalies[side].append(goalie)

    for event in events:
        event_type = clean_text(str(first_value(event, ["type", "event_type", "event_type_name"]))).lower().replace(" ", "_")
        goalie = normalize_goalie_event(event)
        if event_type == "goalie_change" and goalie:
            goalie_changes.append(goalie)

        # For save events, TimeToScore's team id is the shooting team, not the
        # goalie's team. Assign the goalie to the opposite side of that shot.
        team_id = str(first_value(event, ["team", "team_id"]))
        if team_id and team_id == away_id:
            add("home", goalie)
        elif team_id and team_id == home_id:
            add("away", goalie)

    unique_changes: list[dict[str, str]] = []
    unique_keys: set[str] = set()
    for goalie in goalie_changes:
        key = goalie.get("id") or goalie.get("name", "").casefold()
        if not key or key in unique_keys:
            continue
        unique_keys.add(key)
        unique_changes.append(goalie)

    # Older/incomplete games may only include opening goalie-change events, which
    # TimeToScore emits home then away.
    if not goalies["away"] and unique_changes:
        add("away", unique_changes[1] if len(unique_changes) > 1 else unique_changes[0])
    if not goalies["home"] and len(unique_changes) > 1:
        add("home", unique_changes[0])

    return goalies


def normalize_game_center(payload: dict[str, Any], game_id: str, season: str, game: dict[str, Any]) -> dict[str, Any]:
    live = payload.get("live") if isinstance(payload.get("live"), dict) else {}
    events = flatten_game_center_events(live.get("events"))
    shot_summary = live.get("shot_summary") if isinstance(live.get("shot_summary"), dict) else {}
    away_shots = shot_summary.get("away_shots") if isinstance(shot_summary.get("away_shots"), dict) else {}
    home_shots = shot_summary.get("home_shots") if isinstance(shot_summary.get("home_shots"), dict) else {}
    away_team = clean_text(str(game.get("away_team") or first_value(payload.get("game_info", {}) if isinstance(payload.get("game_info"), dict) else {}, ["away_team_name", "visitor_team_name"])))
    home_team = clean_text(str(game.get("home_team") or first_value(payload.get("game_info", {}) if isinstance(payload.get("game_info"), dict) else {}, ["home_team_name"])))
    away_score = 0
    home_score = 0
    goals: list[dict[str, Any]] = []
    penalties: list[dict[str, Any]] = []

    sorted_events = sorted(events, key=lambda event: (period_sort_value(event.get("period")), -seconds_remaining(event.get("time"))))
    for event in sorted_events:
        event_type = clean_text(str(first_value(event, ["type", "event_type", "event_type_name"]))).lower().replace(" ", "_")
        if event_type == "goal":
            team_name = clean_text(str(first_value(event, ["team_name", "team"])))
            if team_name and away_team and team_name == away_team:
                away_score += 1
            elif team_name and home_team and team_name == home_team:
                home_score += 1
            else:
                # Fall back to the public schedule score orientation when names are absent.
                team_id = str(first_value(event, ["team_id"]))
                if team_id and team_id == str(game.get("away_team_id", "")):
                    away_score += 1
                elif team_id and team_id == str(game.get("home_team_id", "")):
                    home_score += 1
            assists = [
                assist
                for assist in (
                    normalize_assist(event, "ass1_player_name", "ass1_prior"),
                    normalize_assist(event, "ass2_player_name", "ass2_prior"),
                )
                if assist
            ]
            goals.append(
                {
                    "period": period_sort_value(event.get("period")),
                    "period_label": period_label(event.get("period")),
                    "time": clean_text(str(event.get("time", ""))),
                    "team": team_name,
                    "scorer": display_person_name(str(first_value(event, ["goal_player_name", "player_name"]))),
                    "scorer_number": clean_text(str(first_value(event, ["goal_player_jersey", "jersey_number"]))),
                    "scorer_total": int_or_none(str(first_value(event, ["goal_prior", "player_prior"]))),
                    "goal_type": clean_text(str(first_value(event, ["goal_type_name"]))),
                    "assists": assists,
                    "score": {"away": away_score, "home": home_score},
                }
            )
        elif "penalty" in event_type:
            penalties.append(
                {
                    "period": period_sort_value(event.get("period")),
                    "period_label": period_label(event.get("period")),
                    "time": clean_text(str(event.get("time", ""))),
                    "team": clean_text(str(first_value(event, ["team_name", "team"]))),
                    "player": display_person_name(str(first_value(event, ["penalty_player_name", "player_name", "player"]))),
                    "infraction": clean_text(str(first_value(event, ["penalty_type_name", "penalty_name", "infraction", "penalty"]))),
                    "minutes": clean_text(str(first_value(event, ["penalty_minutes", "minutes", "min"]))),
                }
            )

    return {
        "schema_version": GAME_CENTER_SCHEMA_VERSION,
        "game_id": game_id,
        "season": season,
        "away_team": away_team,
        "home_team": home_team,
        "away_goals": game.get("away_goals"),
        "home_goals": game.get("home_goals"),
        "shots": {
            "away": normalize_shot_summary(away_shots),
            "home": normalize_shot_summary(home_shots),
        },
        "goalies": normalize_game_goalies(events, game),
        "scoring": group_events_by_period(goals),
        "penalties": group_events_by_period(penalties),
        "has_events": bool(goals or penalties),
    }


def normalize_shot_summary(summary: dict[str, Any]) -> dict[str, Any]:
    periods = {
        str(key): int_or_none(str(value))
        for key, value in summary.items()
        if str(key).isdigit()
    }
    return {
        "total": int_or_none(str(summary.get("total", ""))),
        "periods": periods,
    }


def group_events_by_period(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[int, dict[str, Any]] = {}
    for event in events:
        period = period_sort_value(event.get("period"))
        grouped.setdefault(period, {"period": period, "label": period_label(period), "events": []})["events"].append(event)
    return [
        {**period, "events": sorted(period["events"], key=lambda event: -seconds_remaining(event.get("time")))}
        for _, period in sorted(grouped.items())
    ]


def parse_games_table(table: list[list[Cell]]) -> list[dict[str, Any]]:
    if len(table) < 3:
        return []
    headers = [normalize_header(cell.text) for cell in table[1]]
    games: list[dict[str, Any]] = []
    for row in table[2:]:
        if len(row) < len(headers):
            continue

        def cell_for(name: str, occurrence: int = 0) -> Cell:
            matches = [cell for key, cell in zip(headers, row) if key == name]
            return matches[occurrence] if len(matches) > occurrence else Cell()

        game_text = clean_text(cell_for("game").text)
        game_id = game_text.replace("*", "")
        if not game_id:
            continue
        game_cell = cell_for("game")
        game_link = game_cell.links[0]["href"] if game_cell.links else None
        scorecard_link = None
        if cell_for("scoresheet").links:
            scorecard_link = cell_for("scoresheet").links[0]["href"]

        away_cell = cell_for("away")
        home_cell = cell_for("home")
        away_goals = int_or_none(cell_for("goals", 0).text)
        home_goals = int_or_none(cell_for("goals", 1).text)

        games.append(
            {
                "game_id": game_id,
                "final": "*" in game_text or away_goals is not None or home_goals is not None,
                "date": clean_text(cell_for("date").text),
                "time": clean_text(cell_for("time").text),
                "rink": clean_text(cell_for("rink").text),
                "league": clean_text(cell_for("league").text),
                "level": clean_text(cell_for("level").text),
                "away_team": clean_text(away_cell.text),
                "away_team_id": parse_query(away_cell.links[0]["href"]).get("team", "") if away_cell.links else "",
                "away_goals": away_goals,
                "home_team": clean_text(home_cell.text),
                "home_team_id": parse_query(home_cell.links[0]["href"]).get("team", "") if home_cell.links else "",
                "home_goals": home_goals,
                "type": clean_text(cell_for("type").text),
                "game_center_url": absolutize(game_link),
                "scorecard_url": absolutize(scorecard_link),
            }
        )
    return games


client = TimetoscoreClient()


class AppHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(STATIC_ROOT), **kwargs)

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[server] {self.address_string()} - {format % args}")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            self.handle_api(parsed.path, parse_qs(parsed.query))
            return
        if parsed.path in {"/", ""}:
            self.path = "/index.html"
        super().do_GET()

    def handle_api(self, path: str, query: dict[str, list[str]]) -> None:
        def param(name: str, default: str = "") -> str:
            return query.get(name, [default])[-1]

        try:
            if path == "/api/seasons":
                payload = {"seasons": client.seasons()}
            elif path == "/api/standings":
                payload = client.standings(param("season", "0"))
            elif path == "/api/division-stats":
                payload = client.division_stats(param("season", "0"), param("level"), param("conf", "0"), param("stat_class", "1"))
            elif path == "/api/schedule":
                payload = client.schedule(param("season", "0"), team=param("team") or None, level=param("level") or None, conf=param("conf") or None)
            elif path == "/api/game-center":
                schedule = client.schedule(param("season", "0"))
                game_id = param("game_id")
                game = next((entry for entry in schedule.get("games", []) if str(entry.get("game_id", "")) == game_id), {})
                payload = client.game_center(game_id, str(schedule.get("season", param("season", "0"))), game)
            elif path == "/api/team":
                payload = client.team(param("season", "0"), param("team"))
            elif path == "/api/player":
                payload = client.player_profile(param("name"), param("season_type", "regular"), param("team_id"))
            elif path == "/api/prewarm-player-history":
                payload = client.prewarm_player_history(param("season_type", "regular"))
            elif path == "/api/health":
                payload = {"ok": True, "source": BASE_URL, "league_id": LEAGUE_ID, "history_start_year": HISTORY_START_YEAR}
            else:
                self.write_json({"error": "Unknown API route"}, HTTPStatus.NOT_FOUND)
                return
            self.write_json(payload)
        except Exception as exc:
            self.write_json({"error": str(exc)}, HTTPStatus.BAD_GATEWAY)

    def write_json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Oakland hockey stats tracker")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8765, type=int)
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), AppHandler)
    print(f"Oakland hockey stats running at http://{args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
