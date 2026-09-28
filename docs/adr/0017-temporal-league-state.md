# ADR 0017 — Temporal league state

**Status:** accepted (v0.1.1)

## Decision
* `League` is stable identity only: id, name, season, number of teams and managed team.
* Everything a commissioner or platform can change is a temporal observation with the league
  as its subject:
  * scoring, roster, waiver, season (playoffs, lock, trade deadline) and draft settings;
  * per-team `WaiverBudgetState`;
  * `FantasyRoster`;
  * `PlatformEligibility`, per player;
  * `DraftBoardState`.
* `reconstruct_league_state(reader, league)` reads through the knowledge session and rebuilds
  the rules that were **in effect** at the cutoff. A change announced earlier but effective
  later is not applied. `LeagueState.rules()` fails loudly if any part of the rule set is
  unknown.
* Rule changes are recorded as new observations. Earlier decisions and replays keep
  reconstructing the old rules.
