"""Pull ESPN fantasy football data and write data.json for the dashboard.
Local use: copy .env.example to .env, fill it in, then `python pull_data.py`."""
import os, json, datetime
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass
from espn_api.football import League

LEAGUE_ID = int(os.environ["LEAGUE_ID"])
YEAR = int(os.environ.get("YEAR", datetime.date.today().year))
# Thursday of NFL week 1. VERIFY this for your season; it is used to map trade dates to weeks.
SEASON_START = datetime.date.fromisoformat(os.environ.get("SEASON_START", "2026-09-10"))

league = League(
    league_id=LEAGUE_ID,
    year=YEAR,
    espn_s2=os.environ.get("ESPN_S2"),
    swid=os.environ.get("SWID")
)

SLOT = {
    "QB": "QB", "RB": "RB", "WR": "WR", "TE": "TE",
    "RB/WR/TE": "FLEX", "D/ST": "DST", "K": "K"
}
fix = lambda p: {"D/ST": "DST"}.get(p, p)

# ---- weekly scores, matchups, starters ----
weeks = []
last = min(league.current_week, league.settings.reg_season_count)
for w in range(1, last + 1):
    games, scores, starters = [], {}, {}
    for bs in league.box_scores(w):
        if not (hasattr(bs.home_team, "team_name") and hasattr(bs.away_team, "team_name")):
            continue  # bye week
        games.append([bs.home_team.team_name, bs.away_team.team_name])

        for team, lineup in [(bs.home_team, bs.home_lineup), (bs.away_team, bs.away_lineup)]:
            d = {v: 0.0 for v in SLOT.values()}
            st = []
            for pl in lineup:
                slot = SLOT.get(pl.slot_position)
                if slot is None:
                    continue  # bench / IR
                d[slot] += pl.points
                st.append({
                    "n": pl.name,
                    "p": fix(pl.position),
                    "s": slot,
                    "pts": pl.points
                })
            scores[team.team_name] = {k: round(v, 1) for k, v in d.items()}
            starters[team.team_name] = st

    if scores and sum(sum(v.values()) for v in scores.values()) > 0:
        weeks.append({
            "week": w,
            "games": games,
            "scores": scores,
            "starters": starters
        })

print(f"weeks: {len(weeks)}")

# ---- draft + player pool (season points) ----
draft, pool = [], {}
n_teams = len(league.teams)

for d in league.draft:
    try:
        info = league.player_info(playerId=d.playerId)
        pos, pts = fix(info.position), info.total_points
    except Exception as ex:
        print("player_info failed for", d.playerName, ex)
        pos, pts = "", 0

    draft.append({
        "pick": (d.round_num - 1) * n_teams + d.round_pick,
        "team": d.team.team_name,
        "n": d.playerName,
        "p": pos
    })

    pool[d.playerName] = {"n": d.playerName, "p": pos, "pts": pts}

# Add current rosters to pool
for t in league.teams:
    for pl in t.roster:
        pool.setdefault(pl.name, {
            "n": pl.name,
            "p": fix(pl.position),
            "pts": pl.total_points
        })

print(f"draft picks: {len(draft)}, pool players: {len(pool)}")

# ---- trades ----
def lookup_pos(name):
    # Normalize Player objects → string
    if hasattr(name, "name"):
        name = name.name

    if name in pool:
        return pool[name]["p"]

    try:
        r = league.player_info(name=name)
        r = r[0] if isinstance(r, list) else r
        return fix(r.position) if r else ""
    except Exception:
        return ""

def to_week(day):
    return max(1, ((day - SEASON_START).days - 5) // 7 + 2)

raw, trades = [], []

for a in league.recent_activity(size=500):
    acts = []
    for t, act, name, _ in a.actions:
        if "TRADE" in str(act).upper():
            # Normalize name to string
            if hasattr(name, "name"):
                name = name.name
            acts.append((t.team_name, act, name))

    if not acts:
        continue

    raw.append({"date": a.date, "actions": acts})

    teams = sorted({t for t, _, _ in acts})
    if len(teams) != 2:
        continue  # only 2-team trades supported

    day = datetime.date.fromtimestamp(a.date / 1000)

    trades.append({
        "date": day.isoformat(),
        "week": to_week(day),
        "teams": teams,
        "moves": [
            {"n": n, "p": lookup_pos(n), "to": t}
            for t, _, n in acts
        ]
    })

json.dump(raw, open("debug_trades.json", "w"), indent=2, default=str)
print(f"trades: {len(trades)}")

# ---- final output ----
json.dump({
    "updated": datetime.date.today().isoformat(),
    "teams": [t.team_name for t in league.teams],
    "weeks": weeks,
    "draft": draft,
    "players": list(pool.values()),
    "trades": trades
}, open("data.json", "w"))

print("wrote data.json")
