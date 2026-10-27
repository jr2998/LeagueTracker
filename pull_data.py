"""Pull ESPN fantasy football data and write data.json for the dashboard.
Local use: copy .env.example to .env, fill it in, then `python pull_data.py`.
Each section is isolated: if one fails, the rest still run and the error is
recorded in data.json (shown as a banner on the site) and printed in the log."""
import os, re, sys, json, datetime, traceback, contextlib
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass
from zoneinfo import ZoneInfo
from espn_api.football import League

LEAGUE_ID = int(os.environ["LEAGUE_ID"])
YEAR = int(os.environ.get("YEAR", datetime.date.today().year))
# Thursday of NFL week 1. VERIFY: it decides which weeks count as complete and maps trade dates to weeks.
SEASON_START = datetime.date.fromisoformat(os.environ.get("SEASON_START", "2026-09-10"))
CREDS = dict(espn_s2=os.environ.get("ESPN_S2"), swid=os.environ.get("SWID"))

SLOT = {"QB": "QB", "RB": "RB", "WR": "WR", "TE": "TE",
        "RB/WR/TE": "FLEX", "D/ST": "DST", "K": "K"}      # check your league's slot names
fix = lambda p: {"D/ST": "DST"}.get(p, p)
pname = lambda x: getattr(x, "name", None) or str(x)      # activity actions hold Player objects
norm = lambda s: re.sub(r"[^a-z0-9]", "", str(s).lower())

errors = []
@contextlib.contextmanager
def guard(name):
    try:
        yield
    except Exception as ex:
        errors.append(f"{name}: {type(ex).__name__}: {ex}")
        print(f"ERROR in {name}:"); traceback.print_exc()

league = League(league_id=LEAGUE_ID, year=YEAR, **CREDS)

# ---- weekly scores, matchups, starters (COMPLETED weeks only) ----
ET = ZoneInfo("America/New_York")
NOW = datetime.datetime.now(ET)
def week_complete(w):   # complete once Monday night is over (Tuesday 12:00am Eastern)
    monday = SEASON_START + datetime.timedelta(days=(w - 1) * 7 + 4)
    return NOW >= datetime.datetime.combine(monday + datetime.timedelta(days=1), datetime.time.min, tzinfo=ET)

reg = league.settings.reg_season_count
done = [w for w in range(1, reg + 1) if week_complete(w)]
last = min(max(done, default=0), league.current_week)
print(f"pulling completed weeks 1-{last} (now {NOW:%Y-%m-%d %H:%M} ET, season start {SEASON_START})")

weeks = []
for w in range(1, last + 1):
    games, scores, starters, benches = [], {}, {}, {}
    for bs in league.box_scores(w):
        if not (hasattr(bs.home_team, "team_name") and hasattr(bs.away_team, "team_name")):
            continue  # bye
        games.append([bs.home_team.team_name, bs.away_team.team_name])
        for team, lineup in [(bs.home_team, bs.home_lineup), (bs.away_team, bs.away_lineup)]:
            d, st, bn = {v: 0.0 for v in SLOT.values()}, [], []
            for pl in lineup:
                slot = SLOT.get(pl.slot_position)
                if slot is None:
                    if pl.slot_position == "BE":   # bench only (IR players cannot be started)
                        bn.append({"n": pl.name, "p": fix(pl.position), "pts": pl.points})
                    continue
                d[slot] += pl.points
                st.append({"n": pl.name, "p": fix(pl.position), "s": slot, "pts": pl.points})
            scores[team.team_name] = {k: round(v, 1) for k, v in d.items()}
            starters[team.team_name] = st
            benches[team.team_name] = bn
    if scores and sum(sum(v.values()) for v in scores.values()) > 0:
        weeks.append({"week": w, "games": games, "scores": scores, "starters": starters, "bench": benches})
print(f"weeks: {[x['week'] for x in weeks]}")
if not weeks:
    sys.exit("No completed weeks found; not overwriting data.json. Check SEASON_START and credentials.")

# ---- draft + player pool (season points) ----
draft, pool = [], {}
with guard("draft"):
    n_teams = len(league.teams)
    for d in league.draft:
        try:
            info = league.player_info(playerId=d.playerId)
            pos, pts = fix(info.position), info.total_points
        except Exception as ex:
            print("player_info failed for", d.playerName, ex); pos, pts = "", 0
        draft.append({"pick": (d.round_num - 1) * n_teams + d.round_pick,
                      "team": d.team.team_name, "n": d.playerName, "p": pos})
        pool[d.playerName] = {"n": d.playerName, "p": pos, "pts": pts}
with guard("rosters"):
    for t in league.teams:
        for pl in t.roster:
            pool.setdefault(pl.name, {"n": pl.name, "p": fix(pl.position), "pts": pl.total_points})
print(f"draft picks: {len(draft)}, pool players: {len(pool)}")

# ---- transactions: trades + waiver/FA adds (VERIFY against debug_trades.json) ----
def lookup_pos(name):
    if name in pool:
        return pool[name]["p"]
    try:
        r = league.player_info(name=name)
        r = r[0] if isinstance(r, list) else r
        return fix(r.position) if r else ""
    except Exception:
        return ""

def to_week(day):   # weeks roll over Tuesday: a Wednesday-before-week-3 move maps to week 3
    return max(1, ((day - SEASON_START).days - 5) // 7 + 2)

trades, waivers, raw = [], [], []
with guard("transactions"):
    activity = league.recent_activity(size=1000)
    print(f"activity records: {len(activity)}")
    adds, drops = [], []
    for a in activity:
        day = datetime.date.fromtimestamp(a.date / 1000)
        acts = [(t.team_name, str(act), pname(name), bid) for t, act, name, bid in a.actions]
        tr = [(t, n) for t, act, n, _ in acts if "TRADE" in act.upper()]
        if tr:
            raw.append({"date": str(a.date), "actions": acts})
            teams = sorted({t for t, _ in tr})
            if len(teams) == 2:  # only 2-team trades supported
                # ASSUMPTION: the team on each action is the team that received the player
                trades.append({"date": day.isoformat(), "week": to_week(day), "teams": teams,
                               "moves": [{"n": n, "p": lookup_pos(n), "to": t} for t, n in tr]})
        for t, act, n, bid in acts:
            u = act.upper()
            if "ADDED" in u:
                adds.append((a.date, t, n, bid, u))
            elif "DROPPED" in u:
                drops.append((a.date, t, n))
    for ts, team, name, bid, u in sorted(adds, key=lambda x: x[0]):
        day = datetime.date.fromtimestamp(ts / 1000)
        later = [d[0] for d in drops if d[1] == team and d[2] == name and d[0] > ts]
        dw = to_week(datetime.date.fromtimestamp(min(later) / 1000)) if later else None
        waivers.append({"date": day.isoformat(), "week": to_week(day), "team": team, "n": name,
                        "p": lookup_pos(name), "bid": bid or 0,
                        "type": "WAIVER" if "WAIVER" in u else "FA", "drop_week": dw})
    json.dump(raw, open("debug_trades.json", "w"), indent=2, default=str)
print(f"trades: {len(trades)}, waiver/FA adds: {len(waivers)}")

# ---- HFT tracker: this season's team vs. a past season's team ----
HFT_MATCH = norm(os.environ.get("HFT_TEAM", "haddock"))
BENCH_MATCH = norm(os.environ.get("BENCH_TEAM", "slapdock"))
BENCH_YEAR = int(os.environ.get("BENCH_YEAR", 2023))
BENCH_LEAGUE_ID = int(os.environ.get("BENCH_LEAGUE_ID", LEAGUE_ID))
find_team = lambda lg, m: next((t.team_name for t in lg.teams if m in norm(t.team_name)), None)

hft, hft_note = None, ""
with guard("hft"):
    hft_team = find_team(league, HFT_MATCH)
    if not hft_team:
        hft_note = f"No team in {YEAR} matching '{HFT_MATCH}'. Teams: " + ", ".join(t.team_name for t in league.teams)
    else:
        try:
            old = League(league_id=BENCH_LEAGUE_ID, year=BENCH_YEAR, **CREDS)
        except Exception as ex:
            old = None
            hft_note = f"Could not load {BENCH_YEAR} league {BENCH_LEAGUE_ID}: {type(ex).__name__}: {ex}"
        if old is not None:
            bname = find_team(old, BENCH_MATCH)
            if not bname:
                hft_note = f"No team in {BENCH_YEAR} matching '{BENCH_MATCH}'. Teams: " + ", ".join(t.team_name for t in old.teams)
            else:
                bscores = []
                for w in range(1, old.settings.reg_season_count + 1):
                    for m in old.scoreboard(w):
                        if getattr(m.home_team, "team_name", None) == bname:
                            bscores.append(round(m.home_score, 1))
                        elif getattr(m.away_team, "team_name", None) == bname:
                            bscores.append(round(m.away_score, 1))
                if bscores:
                    hft = {"team": hft_team, "bench": {"name": bname, "year": BENCH_YEAR, "scores": bscores}}
                else:
                    hft_note = f"Found {bname} but no {BENCH_YEAR} scores."
if hft_note:
    print("HFT:", hft_note)
print(f"HFT: {'ok' if hft else 'missing'}")

json.dump({"updated": datetime.date.today().isoformat(),
           "generated": NOW.strftime("%Y-%m-%d %H:%M ET"),
           "last_complete_week": last, "reg_weeks": reg,
           "teams": [t.team_name for t in league.teams],
           "weeks": weeks, "draft": draft, "players": list(pool.values()),
           "trades": trades, "waivers": waivers, "hft": hft, "hft_note": hft_note,
           "debug": {"errors": errors}},
          open("data.json", "w"))
print("wrote data.json", "(with errors: %s)" % errors if errors else "")
