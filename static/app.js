const DEFAULT_HISTORY_START_YEAR = 2017;
const PLAYER_LANDING_LIMIT = 10;
const LOCAL_API_HOSTS = new Set(["", "127.0.0.1", "localhost"]);
const USE_STATIC_DATA = new URLSearchParams(window.location.search).get("data") === "static" || !LOCAL_API_HOSTS.has(window.location.hostname);
const ROUTE_VIEWS = new Set(["standings", "leaders", "schedule", "teams", "players"]);

const state = {
  view: "standings",
  season: "0",
  requestedSeason: "0",
  routeParams: {},
  historyStartYear: DEFAULT_HISTORY_START_YEAR,
  standings: null,
  divisionStats: new Map(),
  schedule: [],
  teams: [],
  leaderMode: "players",
  leaderSortDirection: "desc",
  scheduleMode: "all",
  scheduleDivisionFilter: "all",
  scheduleTeamFilter: "all",
  selectedTeam: null,
  teamDivisionFilter: null,
  teamPickerTeam: "",
  playerDivisionFilter: "all",
  playerTeamFilter: "all",
  playerNameFilter: "all",
  selectedPlayer: null,
  playerSeasonType: "regular",
  playerProfileDivisionFilter: "all",
  playerProfileTeamFilter: "all",
};

let activePlayerRequest = 0;
let isApplyingRoute = false;
const playerProfileCache = new Map();
const playerProfilePrefetches = new Set();

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));

async function api(path) {
  if (USE_STATIC_DATA && path.startsWith("/api/prewarm-player-history")) {
    return { status: "static" };
  }
  const requestPath = USE_STATIC_DATA ? staticDataPath(path) : path;
  const response = await fetch(requestPath);
  const contentType = response.headers.get("content-type") || "";
  if (!contentType.includes("application/json")) {
    throw new Error(USE_STATIC_DATA ? `Static data is missing for ${requestPath}. Rebuild the static export.` : "Request did not return JSON.");
  }
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload.error || "Request failed");
  }
  return payload;
}

function staticDataPath(path) {
  const url = new URL(path, window.location.origin);
  const params = url.searchParams;
  const season = params.get("season") || "0";
  if (url.pathname === "/api/standings") {
    return relativeDataPath(`standings/${season}.json`);
  }
  if (url.pathname === "/api/division-stats") {
    const level = params.get("level") || "0";
    const conf = params.get("conf") || "0";
    const statClass = params.get("stat_class") || "1";
    return relativeDataPath(`division-stats/${season}-${level}-${conf}-${statClass}.json`);
  }
  if (url.pathname === "/api/schedule") {
    return relativeDataPath(`schedule/${season}.json`);
  }
  if (url.pathname === "/api/team") {
    return relativeDataPath(`teams/${season}/${params.get("team") || ""}.json`);
  }
  if (url.pathname === "/api/player") {
    const seasonType = params.get("season_type") || "regular";
    return relativeDataPath(`players/${seasonType}/${slugify(params.get("name") || "")}.json`);
  }
  return relativeDataPath("manifest.json");
}

function relativeDataPath(path) {
  return `data/${path}`;
}

function slugify(value) {
  return String(value || "")
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "") || "unknown";
}

function seasonSlug(season) {
  return season?.id === "0" || season?.current ? "current" : slugify(season?.name || season?.id || "current");
}

function compactSlug(value) {
  return slugify(value).replaceAll("-", "");
}

function slugMatches(value, slug) {
  return slugify(value) === slugify(slug) || compactSlug(value) === compactSlug(slug);
}

function seasonSlugMatches(season, slug) {
  const base = seasonSlug(season);
  const shortYear = base.replace(/20(\d{2})/g, "$1");
  return [base, shortYear].some((candidate) => slugMatches(candidate, slug));
}

function divisionSlug(division) {
  return slugify(division?.name || division?.id || "");
}

function teamSlug(teamOrName) {
  return slugify(typeof teamOrName === "string" ? teamOrName : teamOrName?.name || teamOrName?.id || "");
}

function profileSlug(value) {
  return slugify(value).replace(/^(skater|goalie)-/, "");
}

function showStatus(message, isError = false) {
  const status = $("#status");
  status.hidden = !message;
  status.textContent = message || "";
  status.style.background = isError ? "#ffe2df" : "#fff7d7";
  status.style.borderColor = isError ? "#d56b62" : "#e5c45e";
}

function number(value) {
  return value === null || value === undefined || value === "" ? "-" : value;
}

function normalize(value) {
  return String(value || "").toLowerCase();
}

function teamRecord(team) {
  return `${number(team.wins)}-${number(team.losses)}-${number(team.ties)}`;
}

function escapeAttr(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll('"', "&quot;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}

function routeValue(value, allowed, fallback) {
  return allowed.includes(value) ? value : fallback;
}

function routeViewFromParams(params) {
  const view = params.get("view");
  if (ROUTE_VIEWS.has(view)) return view;
  if (params.has("name")) return "players";
  if (params.has("team")) return "teams";
  if (params.has("leader") || params.has("sort")) return "leaders";
  if (params.has("mode")) return "schedule";
  return "standings";
}

function applyRouteFromUrl() {
  const params = new URLSearchParams(window.location.search);
  state.view = routeViewFromParams(params);
  state.routeParams = {
    season: params.get("season") || "current",
    division: params.get("division") || "",
    team: params.get("team") || "",
    scheduleTeam: params.get("scheduleTeam") || "",
    name: params.get("name") || "",
    sort: params.get("sort") || "",
  };
  state.leaderMode = routeValue(params.get("leader"), ["players", "goalies"], "players");
  state.leaderSortDirection = routeValue(params.get("dir"), ["asc", "desc"], "desc");
  state.scheduleMode = routeValue(params.get("mode"), ["all", "final", "upcoming"], "all");
}

async function resolveRouteSeason() {
  const requested = state.routeParams.season || "current";
  if (!requested || requested === "current" || requested === "0") {
    state.season = "0";
    state.requestedSeason = "0";
    return;
  }
  if (/^\d+$/.test(requested)) {
    state.season = requested;
    state.requestedSeason = requested;
    return;
  }
  const current = await api("/api/standings?season=0");
  const match = (current.seasons || []).find((season) => seasonSlugMatches(season, requested));
  state.season = match?.id || "0";
  state.requestedSeason = state.season;
}

function resolveRouteControls() {
  const route = state.routeParams || {};
  if (route.sort && $("#leaderSort")) {
    $("#leaderSort").value = route.sort;
  }
  if (state.view === "leaders") {
    renderLeaders();
    return;
  }
  const divisions = state.standings?.divisions || [];
  const routeDivision = route.division ? divisions.find((division) => slugMatches(divisionSlug(division), route.division) || division.id === route.division) : null;
  if (state.view === "schedule") {
    const scheduleDivision = route.division
      ? Array.from(new Set(state.schedule.map((game) => game.level).filter(Boolean))).find((division) => slugMatches(division, route.division) || division === route.division)
      : null;
    state.scheduleDivisionFilter = scheduleDivision || "all";
    const scheduleTeam = route.scheduleTeam || route.team;
    state.scheduleTeamFilter = scheduleTeam
      ? scheduleTeamsForDivision(state.scheduleDivisionFilter).find((team) => slugMatches(team, scheduleTeam) || team === scheduleTeam) || "all"
      : "all";
    renderScheduleFilters();
    renderSchedule();
    return;
  }
  if (state.view === "teams") {
    const teams = routeDivision ? routeDivision.teams : state.teams;
    const routeTeam = route.team ? teams.find((team) => slugMatches(teamSlug(team), route.team) || team.id === route.team) : null;
    const routeTeamDivision = routeTeam ? divisions.find((division) => division.teams.some((team) => team.id === routeTeam.id)) : null;
    state.teamDivisionFilter = routeDivision?.id || routeTeamDivision?.id || state.teamDivisionFilter || divisions[0]?.id || "all";
    state.teamPickerTeam = routeTeam?.id || state.teamPickerTeam || teams[0]?.id || "";
    renderTeams();
    if (routeTeam) openTeam(routeTeam.id, { silent: true, scroll: false, updateRoute: false });
    return;
  }
  if (state.view === "players") {
    state.playerDivisionFilter = routeDivision?.id || "all";
    const teams = playerTeamsForDivision(state.playerDivisionFilter);
    const routeTeam = route.team ? teams.find((team) => slugMatches(teamSlug(team), route.team) || team.id === route.team) : null;
    state.playerTeamFilter = routeTeam?.id || "all";
    renderPlayerFilters();
    if (route.name) {
      const routeName = playerOptionsForFilters().find((player) => slugMatches(profileSlug(player.option_value), route.name) || player.option_value === route.name);
      state.playerNameFilter = routeName?.option_value || "all";
    }
    renderPlayerFilters();
    renderPlayers();
  }
}

function currentSeasonParam() {
  const seasons = state.standings?.seasons || [];
  const selected = seasons.find((season) => season.id === state.requestedSeason || (state.requestedSeason === "0" && season.current));
  return seasonSlug(selected || { id: state.season, name: state.season });
}

function currentRouteUrl() {
  const params = new URLSearchParams();
  const existing = new URLSearchParams(window.location.search);
  if (existing.get("data") === "static") params.set("data", "static");
  params.set("view", state.view);
  if (currentSeasonParam() !== "current") params.set("season", currentSeasonParam());

  if (state.view === "leaders") {
    if (state.leaderMode !== "players") params.set("leader", state.leaderMode);
    const sort = $("#leaderSort")?.value;
    if (sort) params.set("sort", sort);
    if (state.leaderSortDirection !== "desc") params.set("dir", state.leaderSortDirection);
  }
  if (state.view === "schedule") {
    if (state.scheduleMode !== "all") params.set("mode", state.scheduleMode);
    if (state.scheduleDivisionFilter !== "all") params.set("division", slugify(state.scheduleDivisionFilter));
    if (state.scheduleTeamFilter !== "all") params.set("team", teamSlug(state.scheduleTeamFilter));
  }
  if (state.view === "teams") {
    const division = (state.standings?.divisions || []).find((entry) => entry.id === state.teamDivisionFilter);
    const team = state.teams.find((entry) => entry.id === state.teamPickerTeam);
    if (division) params.set("division", divisionSlug(division));
    if (team) params.set("team", teamSlug(team));
  }
  if (state.view === "players") {
    const division = (state.standings?.divisions || []).find((entry) => entry.id === state.playerDivisionFilter);
    const team = state.teams.find((entry) => entry.id === state.playerTeamFilter);
    if (division) params.set("division", divisionSlug(division));
    if (team) params.set("team", teamSlug(team));
    if (state.playerNameFilter !== "all") params.set("name", profileSlug(state.playerNameFilter));
  }
  const query = params.toString();
  return `${window.location.pathname}${query ? `?${query}` : ""}`;
}

function updateRoute({ replace = false } = {}) {
  if (isApplyingRoute) return;
  const url = currentRouteUrl();
  if (url === `${window.location.pathname}${window.location.search}`) return;
  window.history[replace ? "replaceState" : "pushState"]({ view: state.view }, "", url);
}

function updateTitle() {
  const labels = {
    standings: "Standings",
    leaders: "League Leaders",
    schedule: "Schedule",
    teams: "Teams",
    players: "Players",
  };
  document.title = `${labels[state.view] || "Stats"} | Oakland Beer League Stats`;
}

function switchView(viewName, options = {}) {
  state.view = ROUTE_VIEWS.has(viewName) ? viewName : "standings";
  $$(".tab").forEach((entry) => entry.classList.toggle("is-active", entry.dataset.view === viewName));
  $$(".view").forEach((view) => view.classList.toggle("is-active", view.id === `${state.view}View`));
  updateTitle();
  if (options.updateRoute !== false) updateRoute();
}

function allPlayers() {
  return Array.from(state.divisionStats.values()).flatMap((stats) => stats.players || []);
}

function allGoalies() {
  return Array.from(state.divisionStats.values()).flatMap((stats) => stats.goalies || []);
}

async function loadApp(force = false) {
  try {
    showStatus("Loading Oakland stats...");
    const requestedSeason = state.requestedSeason || state.season || "0";
    const standings = await api(`/api/standings?season=${encodeURIComponent(requestedSeason)}${force ? `&t=${Date.now()}` : ""}`);
    state.standings = standings;
    state.requestedSeason = standings.requested_season || requestedSeason;
    state.season = standings.season || requestedSeason;
    state.historyStartYear = standings.history_start_year || DEFAULT_HISTORY_START_YEAR;
    state.teams = standings.divisions.flatMap((division) => division.teams);
    if (!state.teamDivisionFilter) {
      state.teamDivisionFilter = standings.divisions[0]?.id || "all";
    }
    populateSeasons(standings.seasons);
    renderStandings();
    renderTeams();

    await loadDivisionStats();
    const schedule = await api(`/api/schedule?season=${encodeURIComponent(state.season)}`);
    state.schedule = schedule.games || [];
    renderLeaders();
    renderScheduleFilters();
    renderSchedule();
    renderPlayerFilters();
    renderPlayers();
    switchView(state.view, { updateRoute: false });
    resolveRouteControls();
    prewarmPlayerHistory();
    updateRoute({ replace: true });
    showStatus("");
  } catch (error) {
    showStatus(error.message, true);
  }
}

function populateSeasons(seasons) {
  const select = $("#seasonSelect");
  select.innerHTML = seasons
    .map((season) => `<option value="${season.id}" ${season.id === state.requestedSeason || (state.requestedSeason === "0" && season.current) ? "selected" : ""}>${season.name}</option>`)
    .join("");
}

async function loadDivisionStats() {
  const divisions = state.standings?.divisions || [];
  await Promise.all(
    divisions.map(async (division) => {
      const key = division.id;
      if (state.divisionStats.has(key)) return;
      const stats = await api(`/api/division-stats?season=${encodeURIComponent(division.season)}&level=${encodeURIComponent(division.level)}&conf=${encodeURIComponent(division.conf)}`);
      const teamByName = new Map(division.teams.map((team) => [team.name, team]));
      state.divisionStats.set(key, {
        players: (stats.players || []).map((player) => ({ ...player, team_id: teamByName.get(player.team)?.id || "", division: division.name, division_id: key })),
        goalies: (stats.goalies || []).map((goalie) => ({ ...goalie, team_id: teamByName.get(goalie.team)?.id || "", division: division.name, division_id: key })),
      });
    })
  );
}

function renderStandings() {
  const grid = $("#standingsGrid");
  const divisions = state.standings?.divisions || [];
  grid.innerHTML = divisions
    .map(
      (division) => `
        <article class="division-card">
          <header>
            <div>
              <h3>${division.name}</h3>
              <p>${division.teams.length} teams</p>
            </div>
          </header>
          <div class="table-wrap">
            <table class="standings-table">
              <colgroup>
                <col class="standings-team-col">
                <col class="standings-gp-col">
                <col class="standings-record-col">
                <col class="standings-goals-col">
                <col class="standings-goals-col">
                <col class="standings-diff-col">
                <col class="standings-points-col">
              </colgroup>
              <thead>
                <tr>
                  <th>Team</th>
                  <th class="number">GP</th>
                  <th class="number">Record</th>
                  <th class="number">GF</th>
                  <th class="number">GA</th>
                  <th class="number">Diff</th>
                  <th class="number">Pts</th>
                </tr>
              </thead>
              <tbody>
                ${division.teams
                  .map(
                    (team) => `
                      <tr>
                        <td><button class="team-button" data-team="${team.id}" type="button">${team.name}</button></td>
                        <td class="number">${number(team.gp)}</td>
                        <td class="number">${teamRecord(team)}</td>
                        <td class="number">${number(team.goals_for)}</td>
                        <td class="number">${number(team.goals_against)}</td>
                        <td class="number">${signedNumber(team.goal_diff)}</td>
                        <td class="number"><b>${number(team.points)}</b></td>
                      </tr>
                    `
                  )
                  .join("")}
              </tbody>
            </table>
          </div>
        </article>
      `
    )
    .join("");
}

function signedNumber(value) {
  if (value === null || value === undefined || value === "") return "-";
  const numericValue = Number(value);
  if (!Number.isFinite(numericValue)) return value;
  return numericValue > 0 ? `+${numericValue}` : String(numericValue);
}

function renderLeaders() {
  const query = normalize($("#leaderSearch").value);
  const mode = state.leaderMode;
  const allowedSorts = mode === "players" ? ["points", "division", "name", "team", "gp", "goals", "assists", "pims", "points_per_game"] : ["save_pct", "division", "name", "team", "gp", "shots", "goals_against", "goals_against_average", "shutouts"];
  const currentSort = $("#leaderSort").value;
  const sort = allowedSorts.includes(currentSort) ? currentSort : allowedSorts[0];
  state.leaderSortDirection = state.leaderSortDirection || defaultLeaderSortDirection(sort);
  const sortDirection = state.leaderSortDirection;
  const divisionRanks = new Map((state.standings?.divisions || []).map((division, index) => [division.id, index]));
  const rows = (mode === "players" ? allPlayers() : allGoalies())
    .filter((row) => !query || normalize(`${row.name} ${row.team} ${row.division}`).includes(query))
    .sort((a, b) => compareLeaderRows(a, b, sort, mode, divisionRanks, sortDirection));

  $("#leaderSort").innerHTML =
    mode === "players"
      ? sortOptions(allowedSorts, sort)
      : sortOptions(allowedSorts, sort);

  $("#leaderTable").innerHTML =
    mode === "players"
      ? renderTable(rows, [
          ["name", "Player"],
          ["team", "Team"],
          ["division", "Division"],
          ["gp", "GP"],
          ["goals", "G"],
          ["assists", "A"],
          ["pims", "PIM"],
          ["points_per_game", "Pts/G"],
          ["points", "Pts"],
        ], { sortable: true, activeSort: sort, sortDirection })
      : renderTable(rows, [
          ["name", "Goalie"],
          ["team", "Team"],
          ["division", "Division"],
          ["gp", "GP"],
          ["shots", "Shots"],
          ["goals_against", "GA"],
          ["goals_against_average", "GAA"],
          ["save_pct", "Save %"],
          ["shutouts", "SO"],
        ], { sortable: true, activeSort: sort, sortDirection });
}

function sortOptions(keys, selected) {
  const labels = {
    name: "Name",
    team: "Team",
    points: "Points",
    goals: "Goals",
    assists: "Assists",
    points_per_game: "Pts/Game",
    pims: "PIM",
    gp: "Games Played",
    division: "Division",
    save_pct: "Save %",
    goals_against_average: "GAA",
    wins: "Wins",
    shots: "Shots",
    shutouts: "Shutouts",
  };
  return keys.map((key) => `<option value="${key}" ${key === selected ? "selected" : ""}>Sort: ${labels[key] || key}</option>`).join("");
}

function defaultLeaderSortDirection(sort) {
  return ["name", "team", "division"].includes(sort) ? "asc" : "desc";
}

function compareLeaderRows(a, b, sort, mode, divisionRanks, direction = "desc") {
  const primaryStat = mode === "players" ? "points" : "save_pct";
  const directionMultiplier = direction === "asc" ? 1 : -1;
  if (sort === "division") {
    return (
      compareDivisionOrder(a, b, divisionRanks) * directionMultiplier ||
      compareNumericDesc(a, b, primaryStat) ||
      compareTextAsc(a.team, b.team) ||
      compareTextAsc(a.name, b.name)
    );
  }
  if (sort === "name" || sort === "team") {
    return (
      compareTextAsc(a[sort], b[sort]) * directionMultiplier ||
      compareDivisionOrder(a, b, divisionRanks) ||
      compareTextAsc(a.name, b.name)
    );
  }
  return (
    compareNumeric(a, b, sort, direction) ||
    compareDivisionOrder(a, b, divisionRanks) ||
    compareTextAsc(a.name, b.name)
  );
}

function compareDivisionOrder(a, b, divisionRanks) {
  const fallbackRank = Number.MAX_SAFE_INTEGER;
  const rankA = divisionRanks.get(a.division_id) ?? fallbackRank;
  const rankB = divisionRanks.get(b.division_id) ?? fallbackRank;
  return rankA - rankB || compareTextAsc(a.division, b.division);
}

function compareNumericDesc(a, b, key) {
  return Number(b[key] || 0) - Number(a[key] || 0);
}

function compareNumeric(a, b, key, direction = "desc") {
  const result = Number(a[key] || 0) - Number(b[key] || 0);
  return direction === "asc" ? result : -result;
}

function compareTextAsc(a, b) {
  return String(a || "").localeCompare(String(b || ""), undefined, { sensitivity: "base", numeric: true });
}

function renderTable(rows, columns, options = {}) {
  if (!rows.length) return `<div class="empty">No matching stats.</div>`;
  return `
    <table>
      <thead>
        <tr>${columns.map(([key, label], index) => renderTableHeader(key, label, index, options)).join("")}</tr>
      </thead>
      <tbody>
        ${rows
          .map(
            (row) => `
              <tr>
                ${columns
                  .map(([key], index) => `<td class="${index > 2 ? "number" : ""}">${key === "name" ? `<button class="team-button" data-player-name="${escapeAttr(row[key])}" data-player-team-id="${escapeAttr(row.team_id)}" type="button">${number(row[key])}</button>` : number(row[key])}</td>`)
                  .join("")}
              </tr>
            `
          )
          .join("")}
      </tbody>
    </table>
  `;
}

function renderTableHeader(key, label, index, options = {}) {
  const isNumber = index > 2;
  if (!options.sortable) return `<th class="${isNumber ? "number" : ""}">${label}</th>`;
  const isActive = options.activeSort === key;
  const direction = isActive ? options.sortDirection : defaultLeaderSortDirection(key);
  const sortLabel = `${label} ${direction === "asc" ? "ascending" : "descending"}`;
  return `
    <th class="${isNumber ? "number" : ""}">
      <button
        class="table-sort ${isActive ? "is-active" : ""}"
        data-leader-sort="${key}"
        type="button"
        aria-label="Sort by ${escapeAttr(sortLabel)}"
        aria-sort="${isActive ? (direction === "asc" ? "ascending" : "descending") : "none"}"
      >
        <span>${label}</span>
        <span class="sort-indicator" aria-hidden="true">${isActive ? (direction === "asc" ? "↑" : "↓") : ""}</span>
      </button>
    </th>
  `;
}

function renderSchedule() {
  const games = state.schedule
    .filter((game) => {
      if (state.scheduleMode === "final") return game.final;
      if (state.scheduleMode === "upcoming") return !game.final;
      return true;
    })
    .filter((game) => state.scheduleDivisionFilter === "all" || game.level === state.scheduleDivisionFilter)
    .filter((game) => state.scheduleTeamFilter === "all" || game.away_team === state.scheduleTeamFilter || game.home_team === state.scheduleTeamFilter);

  $("#scheduleList").innerHTML = games.length
    ? games.map(renderGame).join("")
    : `<div class="empty">No games match that filter.</div>`;
}

function renderScheduleFilters() {
  const divisions = Array.from(new Set(state.schedule.map((game) => game.level).filter(Boolean))).sort((a, b) => compareTextAsc(a, b));
  if (state.scheduleDivisionFilter !== "all" && !divisions.includes(state.scheduleDivisionFilter)) {
    state.scheduleDivisionFilter = "all";
  }

  const teams = scheduleTeamsForDivision(state.scheduleDivisionFilter);
  if (state.scheduleTeamFilter !== "all" && !teams.includes(state.scheduleTeamFilter)) {
    state.scheduleTeamFilter = "all";
  }

  $("#scheduleDivisionSelect").innerHTML = [
    `<option value="all">All divisions</option>`,
    ...divisions.map((division) => `<option value="${escapeAttr(division)}" ${division === state.scheduleDivisionFilter ? "selected" : ""}>${division}</option>`),
  ].join("");

  $("#scheduleTeamSelect").innerHTML = [
    `<option value="all">All teams</option>`,
    ...teams.map((team) => `<option value="${escapeAttr(team)}" ${team === state.scheduleTeamFilter ? "selected" : ""}>${team}</option>`),
  ].join("");
}

function scheduleTeamsForDivision(division) {
  return Array.from(
    new Set(
      state.schedule
        .filter((game) => division === "all" || game.level === division)
        .flatMap((game) => [game.away_team, game.home_team])
        .filter(Boolean)
    )
  ).sort((a, b) => compareTextAsc(a, b));
}

function renderGame(game) {
  return `
    <article class="game-row ${game.final ? "final" : "upcoming"}">
      <div class="game-meta">
        <b>${game.date}</b><br />
        ${game.time}<br />
        ${game.rink}
      </div>
      <div>
        <div class="matchup">
          <div class="team-line"><b>${game.away_team}</b><span class="score">${number(game.away_goals)}</span></div>
          <div class="team-line"><b>${game.home_team}</b><span class="score">${number(game.home_goals)}</span></div>
        </div>
        <div class="game-meta">${game.level} &middot; ${game.type}</div>
      </div>
      <span class="game-status">${game.final ? "Final" : "Upcoming"}</span>
    </article>
  `;
}

function renderTeams() {
  const divisions = state.standings?.divisions || [];
  const selectedDivision = divisions.find((division) => division.id === state.teamDivisionFilter) || divisions[0];
  const teams = selectedDivision ? selectedDivision.teams : state.teams;
  if (selectedDivision && !teams.some((team) => team.id === state.teamPickerTeam)) {
    state.teamPickerTeam = teams[0]?.id || "";
  }
  $("#teamDivisionSelect").innerHTML = divisions
    .map((division) => `<option value="${division.id}" ${division.id === state.teamDivisionFilter ? "selected" : ""}>${division.name}</option>`)
    .join("");
  $("#teamSelect").innerHTML = teams
    .map((team) => `<option value="${team.id}" ${team.id === state.teamPickerTeam ? "selected" : ""}>${team.name}</option>`)
    .join("");

  const selectedTeam = state.teams.find((team) => team.id === state.teamPickerTeam) || teams[0];
  $("#teamsGrid").innerHTML = selectedTeam
    ? [selectedTeam]
    .map(
      (team) => `
        <article class="team-card">
          <header>
            <div>
              <h3>${team.name}</h3>
              <p>${team.division}</p>
            </div>
            <b>${number(team.points)} pts</b>
          </header>
          <div class="stat-strip">
            <div><span class="label">GP</span><span class="value">${number(team.gp)}</span></div>
            <div><span class="label">W</span><span class="value">${number(team.wins)}</span></div>
            <div><span class="label">L</span><span class="value">${number(team.losses)}</span></div>
            <div><span class="label">T</span><span class="value">${number(team.ties)}</span></div>
          </div>
          <button data-load-team="${team.id}" type="button">Open Team</button>
        </article>
      `
    )
    .join("")
    : `<div class="empty">No teams in this division.</div>`;
}

async function openTeam(teamId, options = {}) {
  const team = state.teams.find((entry) => entry.id === teamId);
  if (!team) return;
  try {
    state.teamPickerTeam = teamId;
    state.teamDivisionFilter = state.standings?.divisions.find((division) => division.teams.some((entry) => entry.id === teamId))?.id || state.teamDivisionFilter;
    renderTeams();
    if (!options.silent) showStatus(`Loading ${team.name}...`);
    const detail = await api(`/api/team?season=${encodeURIComponent(state.season)}&team=${encodeURIComponent(teamId)}`);
    state.selectedTeam = { ...team, detail };
    renderTeamDetail();
    if (!options.silent) showStatus("");
    if (options.updateRoute !== false) updateRoute();
    if (options.scroll !== false) $("#teamDetail").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) {
    showStatus(error.message, true);
  }
}

function renderTeamDetail() {
  const selected = state.selectedTeam;
  if (!selected) return;
  const detail = selected.detail;
  const panel = $("#teamDetail");
  panel.hidden = false;
  panel.innerHTML = `
    <header>
      <div>
        <h3>${selected.name}</h3>
        <p>${selected.division}</p>
      </div>
      <b>${teamRecord(selected)}</b>
    </header>
    <div class="detail-body">
      <div class="metric-grid">
        <div class="metric"><span class="label">Points</span><span class="value">${number(selected.points)}</span></div>
        <div class="metric"><span class="label">Games</span><span class="value">${number(selected.gp)}</span></div>
        <div class="metric"><span class="label">Roster</span><span class="value">${detail.players.length}</span></div>
        <div class="metric"><span class="label">Goalies</span><span class="value">${detail.goalies.length}</span></div>
      </div>
      <section>
        <div class="table-section-head">
          <h3>Skaters</h3>
          <span>Swipe for more stats</span>
        </div>
        <div class="table-wrap">${renderTable(detail.players || [], [["name", "Player"], ["number", "#"], ["gp", "GP"], ["goals", "G"], ["assists", "A"], ["pims", "PIM"], ["shots", "Shots"], ["points", "Pts"]])}</div>
      </section>
      <section>
        <div class="table-section-head">
          <h3>Goalies</h3>
          <span>Swipe for more stats</span>
        </div>
        <div class="table-wrap">${renderTable(detail.goalies || [], [["name", "Goalie"], ["number", "#"], ["gp", "GP"], ["shots", "Shots"], ["goals_against", "GA"], ["goals_against_average", "GAA"], ["save_pct", "Save %"], ["wins", "W"]])}</div>
      </section>
      <section>
        <h3>Recent and Upcoming Games</h3>
        <div class="game-list">${(detail.games || []).map(renderGame).join("")}</div>
      </section>
    </div>
  `;
}

function renderPlayers() {
  const filteredSkaters = filterProfileRows(allPlayers(), "skater")
    .sort((a, b) => Number(b.points || 0) - Number(a.points || 0));
  const filteredGoalies = filterProfileRows(allGoalies(), "goalie")
    .sort((a, b) => Number(b.save_pct || 0) - Number(a.save_pct || 0));
  const shouldLimit = state.playerTeamFilter === "all" && state.playerNameFilter === "all";
  const skaters = shouldLimit ? filteredSkaters.slice(0, PLAYER_LANDING_LIMIT) : filteredSkaters;
  const goalies = shouldLimit ? filteredGoalies.slice(0, PLAYER_LANDING_LIMIT) : filteredGoalies;

  $("#playerGridSummary").textContent = shouldLimit
    ? `Top ${Math.min(PLAYER_LANDING_LIMIT, filteredSkaters.length)} skaters by points · Top ${Math.min(PLAYER_LANDING_LIMIT, filteredGoalies.length)} goalies by save percentage`
    : `${skaters.length} skater${skaters.length === 1 ? "" : "s"} · ${goalies.length} goalie${goalies.length === 1 ? "" : "s"}`;

  $("#playerGrid").innerHTML =
    skaters.length || goalies.length
      ? [
          renderProfileSection("Skaters", skaters, renderSkaterCard),
          renderProfileSection("Goalies", goalies, renderGoalieCard),
        ].join("")
      : `<div class="empty">No players match those filters.</div>`;
}

function filterProfileRows(rows, type) {
  return rows
    .filter((row) => state.playerDivisionFilter === "all" || row.division_id === state.playerDivisionFilter)
    .filter((row) => state.playerTeamFilter === "all" || row.team_id === state.playerTeamFilter)
    .filter((row) => state.playerNameFilter === "all" || profileOptionValue(row, type) === state.playerNameFilter);
}

function renderProfileSection(title, rows, cardRenderer) {
  if (!rows.length) {
    return "";
  }
  return `
    <section class="profile-section">
      <div class="profile-section-head">
        <h3>${title}</h3>
        <span>${rows.length}</span>
      </div>
      <div class="player-grid">${rows.map(cardRenderer).join("")}</div>
    </section>
  `;
}

function renderSkaterCard(player) {
  return `
    <article class="player-card is-clickable" data-player-card-name="${escapeAttr(player.name)}" data-player-card-team-id="${escapeAttr(player.team_id)}" role="button" tabindex="0" aria-label="Open profile for ${escapeAttr(player.name)}">
      <header>
        <div>
          <h3><span class="profile-card-name">${player.name}</span></h3>
          <p>${player.team} &middot; ${player.division}</p>
        </div>
        <b>#${number(player.number)}</b>
      </header>
      <div class="stat-strip">
        <div><span class="label">GP</span><span class="value player-stat">${number(player.gp)}</span></div>
        <div><span class="label">G</span><span class="value player-stat is-goals">${number(player.goals)}</span></div>
        <div><span class="label">A</span><span class="value player-stat is-assists">${number(player.assists)}</span></div>
        <div><span class="label">Pts</span><span class="value player-stat is-points">${number(player.points)}</span></div>
      </div>
    </article>
  `;
}

function renderGoalieCard(goalie) {
  return `
    <article class="player-card goalie-card is-clickable" data-player-card-name="${escapeAttr(goalie.name)}" data-player-card-team-id="${escapeAttr(goalie.team_id)}" role="button" tabindex="0" aria-label="Open profile for ${escapeAttr(goalie.name)}">
      <header>
        <div>
          <h3><span class="profile-card-name">${goalie.name}</span></h3>
          <p>${goalie.team} &middot; ${goalie.division}</p>
        </div>
        <b>G</b>
      </header>
      <div class="stat-strip">
        <div><span class="label">GP</span><span class="value player-stat">${number(goalie.gp)}</span></div>
        <div><span class="label">Shots</span><span class="value player-stat is-shots">${number(goalie.shots)}</span></div>
        <div><span class="label">GA</span><span class="value player-stat is-goals">${number(goalie.goals_against)}</span></div>
        <div><span class="label">Save %</span><span class="value player-stat is-points">${number(goalie.save_pct)}</span></div>
      </div>
    </article>
  `;
}

function renderPlayerFilters() {
  const divisions = state.standings?.divisions || [];
  if (state.playerDivisionFilter !== "all" && !divisions.some((division) => division.id === state.playerDivisionFilter)) {
    state.playerDivisionFilter = "all";
  }

  const teams = playerTeamsForDivision(state.playerDivisionFilter);
  if (state.playerTeamFilter !== "all" && !teams.some((team) => team.id === state.playerTeamFilter)) {
    state.playerTeamFilter = "all";
  }

  const players = playerOptionsForFilters();
  if (state.playerNameFilter !== "all" && !players.some((player) => player.option_value === state.playerNameFilter)) {
    state.playerNameFilter = "all";
  }

  $("#playerDivisionSelect").innerHTML = [
    `<option value="all">All divisions</option>`,
    ...divisions.map((division) => `<option value="${escapeAttr(division.id)}" ${division.id === state.playerDivisionFilter ? "selected" : ""}>${division.name}</option>`),
  ].join("");

  $("#playerTeamSelect").innerHTML = [
    `<option value="all">All teams</option>`,
    ...teams.map((team) => `<option value="${escapeAttr(team.id)}" ${team.id === state.playerTeamFilter ? "selected" : ""}>${team.name}</option>`),
  ].join("");

  $("#playerNameSelect").innerHTML = [
    `<option value="all">All players</option>`,
    ...players.map((player) => `<option value="${escapeAttr(player.option_value)}" ${player.option_value === state.playerNameFilter ? "selected" : ""}>${playerNameOptionLabel(player)}</option>`),
  ].join("");
}

function playerNameOptionLabel(player) {
  const role = player.profile_type === "goalie" ? "G" : "S";
  if (state.playerTeamFilter !== "all") {
    return `${player.name} (${role})`;
  }
  return `${player.name} / ${player.team} (${role})`;
}

function playerTeamsForDivision(divisionId) {
  const divisions = state.standings?.divisions || [];
  const teams = divisionId === "all" ? state.teams : divisions.find((division) => division.id === divisionId)?.teams || [];
  return [...teams].sort((a, b) => compareTextAsc(a.name, b.name));
}

function playerOptionsForFilters() {
  const skaters = allPlayers()
    .map((player) => ({ ...player, profile_type: "skater", option_value: profileOptionValue(player, "skater") }));
  const goalies = allGoalies()
    .map((goalie) => ({ ...goalie, profile_type: "goalie", option_value: profileOptionValue(goalie, "goalie") }));
  return [...skaters, ...goalies]
    .filter((player) => state.playerDivisionFilter === "all" || player.division_id === state.playerDivisionFilter)
    .filter((player) => state.playerTeamFilter === "all" || player.team_id === state.playerTeamFilter)
    .sort((a, b) => compareTextAsc(a.name, b.name));
}

function profileOptionValue(row, type) {
  return `${type}:${row.player_id}`;
}

async function openPlayer(playerName, teamId = "") {
  if (!playerName) return;
  const requestId = ++activePlayerRequest;
  const cacheKey = playerProfileCacheKey(playerName, teamId);
  const cachedProfile = playerProfileCache.get(cacheKey);
  state.playerProfileDivisionFilter = "all";
  state.playerProfileTeamFilter = "all";
  if (cachedProfile) {
    openPlayerDrawer();
    state.selectedPlayer = cachedProfile;
    renderPlayerDrawerProfile(cachedProfile);
    return;
  }
  showPlayerDrawerLoading(playerName);
  try {
    const profile = await api(`/api/player?name=${encodeURIComponent(playerName)}&season_type=${encodeURIComponent(state.playerSeasonType)}&team_id=${encodeURIComponent(teamId || "")}`);
    if (requestId !== activePlayerRequest) return;
    profile.team_id = teamId || "";
    playerProfileCache.set(cacheKey, profile);
    state.selectedPlayer = profile;
    renderPlayerDrawerProfile(profile);
  } catch (error) {
    if (requestId !== activePlayerRequest) return;
    renderPlayerDrawerError(error.message);
  }
}

function playerProfileCacheKey(playerName, teamId = "") {
  return [state.playerSeasonType, normalize(playerName), teamId || ""].join("::");
}

function prewarmPlayerHistory() {
  window.setTimeout(() => {
    api(`/api/prewarm-player-history?season_type=${encodeURIComponent(state.playerSeasonType)}`).catch(() => {});
  }, 1000);
}

function prefetchPlayerProfile(playerName, teamId = "") {
  if (!playerName) return;
  const cacheKey = playerProfileCacheKey(playerName, teamId);
  if (playerProfileCache.has(cacheKey) || playerProfilePrefetches.has(cacheKey)) return;
  playerProfilePrefetches.add(cacheKey);
  api(`/api/player?name=${encodeURIComponent(playerName)}&season_type=${encodeURIComponent(state.playerSeasonType)}&team_id=${encodeURIComponent(teamId || "")}`)
    .then((profile) => {
      profile.team_id = teamId || "";
      playerProfileCache.set(cacheKey, profile);
    })
    .catch(() => {})
    .finally(() => playerProfilePrefetches.delete(cacheKey));
}

function renderPlayerProfile() {
  const profile = state.selectedPlayer;
  const panel = $("#playerProfile");
  if (!profile) {
    panel.hidden = true;
    return;
  }
  panel.hidden = false;
  renderPlayerProfileInto(panel, profile, "playerSplitSelect");
}

function renderPlayerDrawerProfile(profile) {
  const body = $("#playerDrawerBody");
  renderPlayerProfileInto(body, profile, "playerDrawerSplitSelect", "playerDrawerTitle");
}

function renderPlayerProfileInto(panel, profile, splitSelectId, titleId = "") {
  validatePlayerProfileFilters(profile);
  const rows = filteredProfileRows(profile.skater_seasons || []);
  const goalieRows = filteredProfileRows(profile.goalie_seasons || []);
  const career = careerTotals(rows, "skater");
  const goalieCareer = careerTotals(goalieRows, "goalie");
  const showGoalieMetrics = goalieRows.length && (!rows.length || Number(goalieCareer.gp || 0) >= Number(career.gp || 0));
  const latest = latestProfileRow(rows, goalieRows) || latestProfileRow(profile.skater_seasons || [], profile.goalie_seasons || []) || {};
  const divisionOptions = profileScopeOptions(profile, "division");
  const teamOptions = profileScopeOptions(profile, "team");
  const divisionSelectId = `${splitSelectId}Division`;
  const teamSelectId = `${splitSelectId}Team`;
  const titleAttribute = titleId ? ` id="${titleId}"` : "";
  const availableSplits = profile.available_splits?.length ? profile.available_splits : ["regular", "playoffs"];
  panel.innerHTML = `
    <header class="player-hero">
      <div>
        <p class="eyebrow">Player Profile</p>
        <h2${titleAttribute}>${profile.name}</h2>
        <p>${number(latest.team)}${latest.division ? ` / ${latest.division}` : ""} · ${profile.history_start_year || state.historyStartYear} onward</p>
      </div>
      <label class="split-control">
        <span>Split</span>
        <select id="${splitSelectId}">
          ${availableSplits.map((split) => `<option value="${escapeAttr(split)}" ${profile.season_type === split ? "selected" : ""}>${split === "playoffs" ? "Playoffs" : "Regular Season"}</option>`).join("")}
        </select>
      </label>
    </header>
    <div class="profile-scope-controls">
      <label>
        <span>Division</span>
        <select id="${divisionSelectId}">
          <option value="all">All divisions</option>
          ${divisionOptions.map((option) => `<option value="${escapeAttr(option.value)}" ${option.value === state.playerProfileDivisionFilter ? "selected" : ""}>${escapeAttr(option.label)}</option>`).join("")}
        </select>
      </label>
      <label>
        <span>Team</span>
        <select id="${teamSelectId}">
          <option value="all">All teams</option>
          ${teamOptions.map((option) => `<option value="${escapeAttr(option.value)}" ${option.value === state.playerProfileTeamFilter ? "selected" : ""}>${escapeAttr(option.label)}</option>`).join("")}
        </select>
      </label>
    </div>
    <div class="metric-grid player-metrics">
      ${
        showGoalieMetrics
          ? `
            <div class="metric"><span class="label">GP</span><span class="value">${number(goalieCareer.gp)}</span></div>
            <div class="metric"><span class="label">Shots</span><span class="value">${number(goalieCareer.shots)}</span></div>
            <div class="metric"><span class="label">GA</span><span class="value">${number(goalieCareer.goals_against)}</span></div>
            <div class="metric"><span class="label">Save %</span><span class="value">${number(goalieCareer.save_pct)}</span></div>
          `
          : `
            <div class="metric"><span class="label">GP</span><span class="value">${number(career.gp)}</span></div>
            <div class="metric"><span class="label">Goals</span><span class="value">${number(career.goals)}</span></div>
            <div class="metric"><span class="label">Assists</span><span class="value">${number(career.assists)}</span></div>
            <div class="metric"><span class="label">Points</span><span class="value">${number(career.points)}</span></div>
          `
      }
    </div>
    ${rows.length ? `<section><h3>Skater Stats</h3><div class="table-wrap">${renderPlayerSeasonTable(rows, career)}</div></section>` : ""}
    ${goalieRows.length ? `<section><h3>Goalie Stats</h3><div class="table-wrap">${renderGoalieSeasonTable(goalieRows, goalieCareer)}</div></section>` : ""}
    ${!rows.length && !goalieRows.length ? `<div class="empty">No stats found for this player identity.</div>` : ""}
  `;
  panel.querySelector(`#${splitSelectId}`).addEventListener("change", (event) => {
    state.playerSeasonType = event.target.value;
    state.playerProfileDivisionFilter = "all";
    state.playerProfileTeamFilter = "all";
    openPlayer(profile.name, profile.team_id);
  });
  panel.querySelector(`#${divisionSelectId}`).addEventListener("change", (event) => {
    state.playerProfileDivisionFilter = event.target.value;
    state.playerProfileTeamFilter = "all";
    renderPlayerProfileInto(panel, profile, splitSelectId, titleId);
  });
  panel.querySelector(`#${teamSelectId}`).addEventListener("change", (event) => {
    state.playerProfileTeamFilter = event.target.value;
    renderPlayerProfileInto(panel, profile, splitSelectId, titleId);
  });
}

function latestProfileRow(skaterRows, goalieRows) {
  return [...skaterRows, ...goalieRows]
    .filter(Boolean)
    .sort((a, b) => Number(b.season_id || 0) - Number(a.season_id || 0))[0];
}

function allProfileRows(profile) {
  return [...(profile.skater_seasons || []), ...(profile.goalie_seasons || [])];
}

function filteredProfileRows(rows) {
  return rows
    .filter((row) => state.playerProfileDivisionFilter === "all" || profileScopeValue(row, "division") === state.playerProfileDivisionFilter)
    .filter((row) => state.playerProfileTeamFilter === "all" || profileScopeValue(row, "team") === state.playerProfileTeamFilter);
}

function validatePlayerProfileFilters(profile) {
  const divisionValues = new Set(profileScopeOptions(profile, "division").map((option) => option.value));
  if (state.playerProfileDivisionFilter !== "all" && !divisionValues.has(state.playerProfileDivisionFilter)) {
    state.playerProfileDivisionFilter = "all";
  }
  const teamValues = new Set(profileScopeOptions(profile, "team").map((option) => option.value));
  if (state.playerProfileTeamFilter !== "all" && !teamValues.has(state.playerProfileTeamFilter)) {
    state.playerProfileTeamFilter = "all";
  }
}

function profileScopeOptions(profile, type) {
  const rows = allProfileRows(profile)
    .filter((row) => type !== "team" || state.playerProfileDivisionFilter === "all" || profileScopeValue(row, "division") === state.playerProfileDivisionFilter);
  const seen = new Set();
  return rows
    .map((row) => ({ value: profileScopeValue(row, type), label: type === "division" ? row.division : row.team }))
    .filter((option) => option.value !== "all" && option.label)
    .filter((option) => {
      if (seen.has(option.value)) return false;
      seen.add(option.value);
      return true;
    })
    .sort((a, b) => compareTextAsc(a.label, b.label));
}

function profileScopeValue(row, type) {
  if (type === "division") {
    return row.division_id ? `division-id:${row.division_id}` : `division:${row.division || ""}`;
  }
  return row.team_id ? `team-id:${row.team_id}` : `team:${row.team || ""}`;
}

function sumProfileStat(rows, key) {
  return rows.reduce((total, row) => {
    const value = row[key];
    return typeof value === "number" ? total + value : total;
  }, 0);
}

function careerTotals(rows, mode) {
  if (!rows.length) return {};
  if (mode === "goalie") {
    const shots = sumProfileStat(rows, "shots");
    const goalsAgainst = sumProfileStat(rows, "goals_against");
    return {
      season: "Career",
      team: "Totals",
      division: "-",
      gp: sumProfileStat(rows, "gp"),
      shots,
      goals_against: goalsAgainst,
      goals_against_average: null,
      save_pct: shots ? Number(((shots - goalsAgainst) / shots).toFixed(3)) : null,
      shutouts: sumProfileStat(rows, "shutouts"),
    };
  }

  const gp = sumProfileStat(rows, "gp");
  const points = sumProfileStat(rows, "points");
  return {
    season: "Career",
    team: "Totals",
    division: "-",
    gp,
    goals: sumProfileStat(rows, "goals"),
    assists: sumProfileStat(rows, "assists"),
    points,
    plus_minus: sumProfileStat(rows, "plus_minus"),
    pims: sumProfileStat(rows, "pims"),
    hat: sumProfileStat(rows, "hat"),
    points_per_game: gp ? Number((points / gp).toFixed(2)) : null,
  };
}

function openPlayerDrawer() {
  const drawer = $("#playerDrawer");
  drawer.hidden = false;
  drawer.setAttribute("aria-hidden", "false");
  document.body.classList.add("drawer-open");
  requestAnimationFrame(() => drawer.classList.add("is-open"));
}

function showPlayerDrawerLoading(playerName) {
  openPlayerDrawer();
  $("#playerDrawerBody").innerHTML = `
    <div class="player-loading">
      <span class="loader-dot" aria-hidden="true"></span>
      <div>
        <p class="eyebrow">Loading Player</p>
        <h2 id="playerDrawerTitle">${playerName}</h2>
        <p>Pulling season-by-season stats now.</p>
      </div>
    </div>
  `;
}

function renderPlayerDrawerError(message) {
  $("#playerDrawerBody").innerHTML = `
    <div class="player-loading is-error">
      <div>
        <p class="eyebrow">Profile unavailable</p>
        <h2 id="playerDrawerTitle">Could not load player</h2>
        <p>${message}</p>
      </div>
    </div>
  `;
}

function closePlayerDrawer(options = {}) {
  const drawer = $("#playerDrawer");
  activePlayerRequest += 1;
  drawer.classList.remove("is-open");
  drawer.setAttribute("aria-hidden", "true");
  document.body.classList.remove("drawer-open");
  if (options.immediate) {
    drawer.hidden = true;
    return;
  }
  window.setTimeout(() => {
    if (!drawer.classList.contains("is-open")) {
      drawer.hidden = true;
    }
  }, 180);
}

function renderPlayerSeasonTable(rows, career) {
  if (!rows.length) return `<div class="empty">No stats for this split.</div>`;
  const allRows = [...rows, career].filter((row) => row && Object.keys(row).length);
  return `
    <table>
      <thead>
        <tr>
          <th>Season</th><th>Team</th><th>Division</th><th class="number">GP</th><th class="number">G</th><th class="number">A</th><th class="number">P</th><th class="number">+/-</th><th class="number">PIM</th><th class="number">Hat</th><th class="number">Pts/G</th>
        </tr>
      </thead>
      <tbody>
        ${allRows
          .map(
            (row) => `
              <tr class="${row.season === "Career" ? "career-row" : ""}">
                <td>${number(row.season)}</td>
                <td>${number(row.team)}</td>
                <td>${number(row.division)}</td>
                <td class="number">${number(row.gp)}</td>
                <td class="number">${number(row.goals)}</td>
                <td class="number">${number(row.assists)}</td>
                <td class="number">${number(row.points)}</td>
                <td class="number">${number(row.plus_minus)}</td>
                <td class="number">${number(row.pims)}</td>
                <td class="number">${number(row.hat)}</td>
                <td class="number">${number(row.points_per_game)}</td>
              </tr>
            `
          )
          .join("")}
      </tbody>
    </table>
  `;
}

function renderGoalieSeasonTable(rows, career) {
  const allRows = [...rows, career].filter((row) => row && Object.keys(row).length);
  return `
    <table>
      <thead>
        <tr>
          <th>Season</th><th>Team</th><th>Division</th><th class="number">GP</th><th class="number">Shots</th><th class="number">GA</th><th class="number">GAA</th><th class="number">Save %</th><th class="number">SO</th>
        </tr>
      </thead>
      <tbody>
        ${allRows
          .map(
            (row) => `
              <tr class="${row.season === "Career" ? "career-row" : ""}">
                <td>${number(row.season)}</td>
                <td>${number(row.team)}</td>
                <td>${number(row.division)}</td>
                <td class="number">${number(row.gp)}</td>
                <td class="number">${number(row.shots)}</td>
                <td class="number">${number(row.goals_against)}</td>
                <td class="number">${number(row.goals_against_average)}</td>
                <td class="number">${number(row.save_pct)}</td>
                <td class="number">${number(row.shutouts)}</td>
              </tr>
            `
          )
          .join("")}
      </tbody>
    </table>
  `;
}

function bindEvents() {
  $("#seasonSelect").addEventListener("change", (event) => {
    state.season = event.target.value;
    state.requestedSeason = event.target.value;
    state.routeParams = {};
    state.divisionStats.clear();
    state.scheduleDivisionFilter = "all";
    state.scheduleTeamFilter = "all";
    state.playerDivisionFilter = "all";
    state.playerTeamFilter = "all";
    state.playerNameFilter = "all";
    state.selectedTeam = null;
    state.selectedPlayer = null;
    playerProfileCache.clear();
    playerProfilePrefetches.clear();
    $("#teamDetail").hidden = true;
    $("#playerProfile").hidden = true;
    closePlayerDrawer({ immediate: true });
    loadApp();
  });

  $("#refreshButton").addEventListener("click", () => {
    state.divisionStats.clear();
    playerProfileCache.clear();
    playerProfilePrefetches.clear();
    loadApp(true);
  });

  $$(".tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      switchView(tab.dataset.view);
      if (tab.classList.contains("hero-tab")) {
        document.querySelector("main")?.scrollIntoView({ behavior: "smooth", block: "start" });
      }
    });
  });

  document.addEventListener("click", (event) => {
    const leaderMode = event.target.closest("[data-leader-mode]");
    if (leaderMode) {
      state.leaderMode = leaderMode.dataset.leaderMode;
      state.leaderSortDirection = "desc";
      $$("#leaderMode button").forEach((button) => button.classList.toggle("is-active", button === leaderMode));
      renderLeaders();
      updateRoute();
      return;
    }

    const leaderSort = event.target.closest("[data-leader-sort]");
    if (leaderSort) {
      const sort = leaderSort.dataset.leaderSort;
      const select = $("#leaderSort");
      if (select.value === sort) {
        state.leaderSortDirection = state.leaderSortDirection === "asc" ? "desc" : "asc";
      } else {
        state.leaderSortDirection = defaultLeaderSortDirection(sort);
      }
      select.value = sort;
      renderLeaders();
      updateRoute();
      return;
    }

    const scheduleMode = event.target.closest("[data-schedule-mode]");
    if (scheduleMode) {
      state.scheduleMode = scheduleMode.dataset.scheduleMode;
      $$("#scheduleMode button").forEach((button) => button.classList.toggle("is-active", button === scheduleMode));
      renderSchedule();
      updateRoute();
      return;
    }

    const teamButton = event.target.closest("[data-team], [data-load-team]");
    if (teamButton) {
      const teamId = teamButton.dataset.team || teamButton.dataset.loadTeam;
      switchView("teams", { updateRoute: false });
      openTeam(teamId);
      return;
    }

    const playerButton = event.target.closest("[data-player-name]");
    if (playerButton) {
      event.preventDefault();
      openPlayer(playerButton.dataset.playerName, playerButton.dataset.playerTeamId || "");
      return;
    }

    const playerCard = event.target.closest("[data-player-card-name]");
    if (playerCard) {
      openPlayer(playerCard.dataset.playerCardName, playerCard.dataset.playerCardTeamId || "");
      return;
    }
  });

  document.addEventListener("pointerover", (event) => {
    const playerButton = event.target.closest("[data-player-name]");
    if (playerButton) {
      prefetchPlayerProfile(playerButton.dataset.playerName, playerButton.dataset.playerTeamId || "");
      return;
    }
    const playerCard = event.target.closest("[data-player-card-name]");
    if (playerCard) {
      prefetchPlayerProfile(playerCard.dataset.playerCardName, playerCard.dataset.playerCardTeamId || "");
    }
  });

  document.addEventListener("focusin", (event) => {
    const playerButton = event.target.closest("[data-player-name]");
    if (playerButton) {
      prefetchPlayerProfile(playerButton.dataset.playerName, playerButton.dataset.playerTeamId || "");
      return;
    }
    const playerCard = event.target.closest("[data-player-card-name]");
    if (playerCard) {
      prefetchPlayerProfile(playerCard.dataset.playerCardName, playerCard.dataset.playerCardTeamId || "");
    }
  });

  $("#playerDrawerClose").addEventListener("click", () => closePlayerDrawer());
  $("#playerDrawerBackdrop").addEventListener("click", () => closePlayerDrawer());
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !$("#playerDrawer").hidden) {
      closePlayerDrawer();
    }

    const playerCard = event.target.closest("[data-player-card-name]");
    if (playerCard && (event.key === "Enter" || event.key === " ")) {
      event.preventDefault();
      openPlayer(playerCard.dataset.playerCardName, playerCard.dataset.playerCardTeamId || "");
    }
  });

  $("#teamDivisionSelect").addEventListener("change", (event) => {
    state.teamDivisionFilter = event.target.value;
    state.teamPickerTeam = "";
    state.selectedTeam = null;
    $("#teamDetail").hidden = true;
    renderTeams();
    updateRoute();
  });
  $("#teamSelect").addEventListener("change", (event) => {
    state.teamPickerTeam = event.target.value;
    state.selectedTeam = null;
    $("#teamDetail").hidden = true;
    renderTeams();
    updateRoute();
  });
  $("#openSelectedTeam").addEventListener("click", () => openTeam(state.teamPickerTeam));
  $("#leaderSearch").addEventListener("input", renderLeaders);
  $("#leaderSort").addEventListener("change", (event) => {
    state.leaderSortDirection = defaultLeaderSortDirection(event.target.value);
    renderLeaders();
    updateRoute();
  });
  $("#scheduleDivisionSelect").addEventListener("change", (event) => {
    state.scheduleDivisionFilter = event.target.value;
    state.scheduleTeamFilter = "all";
    renderScheduleFilters();
    renderSchedule();
    updateRoute();
  });
  $("#scheduleTeamSelect").addEventListener("change", (event) => {
    state.scheduleTeamFilter = event.target.value;
    renderSchedule();
    updateRoute();
  });
  $("#playerDivisionSelect").addEventListener("change", (event) => {
    state.playerDivisionFilter = event.target.value;
    state.playerTeamFilter = "all";
    state.playerNameFilter = "all";
    renderPlayerFilters();
    renderPlayers();
    updateRoute();
  });
  $("#playerTeamSelect").addEventListener("change", (event) => {
    state.playerTeamFilter = event.target.value;
    state.playerNameFilter = "all";
    renderPlayerFilters();
    renderPlayers();
    updateRoute();
  });
  $("#playerNameSelect").addEventListener("change", (event) => {
    state.playerNameFilter = event.target.value;
    renderPlayers();
    updateRoute();
  });
}

window.addEventListener("popstate", async () => {
  isApplyingRoute = true;
  try {
    applyRouteFromUrl();
    await resolveRouteSeason();
    state.divisionStats.clear();
    state.selectedTeam = null;
    state.selectedPlayer = null;
    $("#teamDetail").hidden = true;
    closePlayerDrawer({ immediate: true });
    await loadApp();
  } finally {
    isApplyingRoute = false;
  }
});

async function initApp() {
  applyRouteFromUrl();
  await resolveRouteSeason();
  bindEvents();
  await loadApp();
}

initApp();
