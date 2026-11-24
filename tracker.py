import os, re, time, json, urllib.request, urllib.error
from datetime import datetime
from zoneinfo import ZoneInfo

# Daily summary is posted when the day rolls over in this time zone
TZ = ZoneInfo(os.environ.get('TRACKER_TZ', 'America/Chicago'))

# Secrets (set in GitHub, never in this file)
WEBHOOK_URL = os.environ.get('DISCORD_WEBHOOK', '').strip()
BOT_TOKEN = os.environ.get('DISCORD_BOT_TOKEN', '').strip()

# Starting list -- only used the very first time; after that !add / !remove in Discord control it
DEFAULT_SERVERS = ['9652', '9349', '9086']
SERVER_POLL_SECONDS = 60   # how often player counts are checked
COMMAND_POLL_SECONDS = 15  # how often the channel is checked for !commands
# How long one cloud run lasts before handing off to the next (GitHub max job time is 6h)
RUN_SECONDS = int(os.environ.get('RUN_SECONDS', 5 * 3600 + 30 * 60))
STATE_FILE = os.environ.get('STATE_FILE', 'state.json')

LIST_URL = 'https://cdn2.arkdedicated.com/servers/asa/officialserverlist.json'
DISCORD_API = 'https://discord.com/api/v10'
HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/130.0 Safari/537.36'}


def http_json(url, method='GET', body=None, headers=None, timeout=30):
    data = json.dumps(body).encode('utf-8') if body is not None else None
    h = {**HEADERS, **(headers or {})}
    if data is not None:
        h['Content-Type'] = 'application/json'
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        return json.loads(raw) if raw else None


def send_to_discord(msg):
    if not WEBHOOK_URL:
        print(f'[webhook] not set, would send: {msg}')
        return
    try:
        http_json(WEBHOOK_URL, 'POST', {'content': msg}, headers={'User-Agent': DISCORD_UA}, timeout=10)
    except Exception as e:
        print(f'[webhook] error: {e}')


def fetch_list():
    return http_json(LIST_URL)


def find_server(servers, number):
    # match the number at the very end of the name, so 9652 doesn't match 19652
    pat = re.compile(rf'(?<!\d){number}$')
    for s in servers:
        if pat.search(str(s.get('Name', '')).strip()):
            return s
    return None


def new_entry(num):
    return {'last': None, 'down': False, 'name': num}


def load_state():
    try:
        with open(STATE_FILE, encoding='utf-8') as f:
            saved = json.load(f)
    except Exception:
        saved = {}
    if 'servers' not in saved:  # old format: {num: {...}}
        old = {k: v for k, v in saved.items() if k.isdigit()}
        saved = {'servers': old or {n: new_entry(n) for n in DEFAULT_SERVERS}, 'last_msg_id': None}
    return saved


def save_state(state):
    with open(STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(state, f, indent=2)


# ---------- Discord commands (!add / !remove / !list) ----------

DISCORD_UA = 'DiscordBot (https://github.com/cavanesskevin-ctrl/ark-tracker, 1.0)'


def bot_get(path):
    return http_json(DISCORD_API + path, headers={'Authorization': f'Bot {BOT_TOKEN}',
                                                 'User-Agent': DISCORD_UA})


def get_channel_id():
    # the channel the webhook posts to is the channel we read commands from
    try:
        cid = http_json(WEBHOOK_URL, headers={'User-Agent': DISCORD_UA})['channel_id']
        me = bot_get('/users/@me')
        print(f'[commands] bot {me.get("username")} reading channel {cid}')
        bot_get(f'/channels/{cid}')  # fails here if the bot can't see the channel
        return cid
    except urllib.error.HTTPError as e:
        print(f'[commands] could not use channel: HTTP {e.code} {e.read()[:300]!r}')
        send_to_discord(f'⚠️ Commands are off: the bot can\'t access this channel (HTTP {e.code}). '
                        'Make sure the ARK Tracker bot is in the server and can view this channel.')
        return None
    except Exception as e:
        print(f'[commands] could not look up channel: {e}')
        return None


HELP = ('**ARK tracker commands**\n'
        '`!add 1234` start tracking server 1234\n'
        '`!remove 1234` stop tracking it\n'
        '`!list` show tracked servers\n'
        '`!today` population stats so far today\n'
        '`!empty` when each server sat at 0 players today + hours it is usually empty\n'
        '`!empty 1234` same, for one server\n'
        '`!hours 1234` average players and % of time empty for every hour of the day')

HISTORY_DAYS = 14        # how many days of hourly data to keep
USUALLY_EMPTY_PCT = 0.8  # an hour counts as "usually empty" if it was at 0 this much of the time


# ---------- daily summary ----------

def now_tz():
    return datetime.now(TZ)


def today_str():
    return now_tz().strftime('%Y-%m-%d')


def fmt_t(dt):
    return dt.strftime('%I:%M %p').lstrip('0')


def fmt_hour(h):
    return f'{(h % 12) or 12} {"AM" if h < 12 else "PM"}'


def fmt_dur(secs):
    h, m = divmod(int(secs) // 60, 60)
    return f'{h}h {m}m' if h else f'{m}m'


def close_empty(state, num, end_dt):
    """End an open 'server at 0' stretch and log it in today's stats."""
    since = state.setdefault('empty', {}).pop(num, None)
    if not since:
        return
    s = state.get('daily', {}).get('servers', {}).get(num)
    if s is not None:
        s.setdefault('empty_periods', []).append([since, end_dt.isoformat()])


def record_sample(state, num, name, cur):
    now = now_tz()
    d = state.setdefault('daily', {'date': today_str(), 'servers': {}})
    s = d['servers'].setdefault(num, {'name': name, 'peak': cur, 'peak_at': '', 'low': cur, 'low_at': '',
                                      'sum': 0, 'samples': 0, 'joins': 0, 'leaves': 0})
    s['name'] = name
    if cur >= s['peak'] or not s['peak_at']:
        s['peak'], s['peak_at'] = cur, fmt_t(now)
    if cur <= s['low'] or not s['low_at']:
        s['low'], s['low_at'] = cur, fmt_t(now)
    s['sum'] += cur
    s['samples'] += 1

    # stretches at 0 players
    empty = state.setdefault('empty', {})
    if cur == 0 and num not in empty:
        empty[num] = now.isoformat()
    elif cur > 0:
        close_empty(state, num, now)

    # hourly history: [samples, samples at 0, total players]
    hist = state.setdefault('history', {}).setdefault(num, {})
    day = hist.setdefault(today_str(), [[0, 0, 0] for _ in range(24)])
    slot = day[now.hour]
    slot[0] += 1
    slot[1] += cur == 0
    slot[2] += cur


def empty_periods_today(state, num):
    """Completed stretches today plus the one still going (if any), as (start, end, ongoing)."""
    s = state.get('daily', {}).get('servers', {}).get(num, {})
    out = [(datetime.fromisoformat(a), datetime.fromisoformat(b), False) for a, b in s.get('empty_periods', [])]
    since = state.get('empty', {}).get(num)
    if since:
        out.append((datetime.fromisoformat(since), now_tz(), True))
    return out


def format_periods(periods, limit=6):
    parts = [f'{fmt_t(a)}–{"now" if live else fmt_t(b)} ({fmt_dur((b - a).total_seconds())})'
             for a, b, live in periods]
    if len(parts) > limit:
        parts = parts[:limit] + [f'+{len(parts) - limit} more']
    return ', '.join(parts)


def hourly_stats(state, num):
    """Per hour of day across kept history: (avg players, fraction of time at 0, samples, days)."""
    days = state.get('history', {}).get(num, {})
    out = []
    for h in range(24):
        n = sum(d[h][0] for d in days.values())
        z = sum(d[h][1] for d in days.values())
        tot = sum(d[h][2] for d in days.values())
        out.append((tot / n if n else None, z / n if n else None, n))
    return out, len(days)


def usually_empty_ranges(stats):
    """Merge hours that are usually at 0 into ranges like '3 AM–6 AM (92%)'."""
    ranges, start, pcts = [], None, []
    for h in range(25):
        pct = stats[h][1] if h < 24 else None
        if pct is not None and pct >= USUALLY_EMPTY_PCT:
            if start is None:
                start, pcts = h, []
            pcts.append(pct)
        elif start is not None:
            ranges.append(f'{fmt_hour(start)}–{fmt_hour(h % 24)} ({sum(pcts) / len(pcts):.0%})')
            start = None
    return ranges


def format_empty(state, num):
    name = state['servers'].get(num, {}).get('name', num)
    periods = empty_periods_today(state, num)
    total = sum((b - a).total_seconds() for a, b, _ in periods)
    lines = [f'**{name}**']
    if periods:
        lines.append(f'  At 0 today: **{fmt_dur(total)}** total. {format_periods(periods)}')
    else:
        lines.append('  Not at 0 at any point today.')
    stats, ndays = hourly_stats(state, num)
    ranges = usually_empty_ranges(stats)
    if ndays < 2:
        lines.append(f'  Usually empty: still learning ({ndays} day of data so far).')
    elif ranges:
        lines.append(f'  Usually empty (last {ndays} days): {", ".join(ranges)}')
    else:
        lines.append(f'  Usually empty (last {ndays} days): no hour is reliably at 0.')
    return '\n'.join(lines)


def format_hours(state, num):
    name = state['servers'].get(num, {}).get('name', num)
    stats, ndays = hourly_stats(state, num)
    rows = []
    for h, (avg, pct, n) in enumerate(stats):
        if n == 0:
            rows.append(f'{fmt_hour(h):>5}    -   no data yet')
        else:
            bar = '█' * round(pct * 10) + '░' * (10 - round(pct * 10))
            rows.append(f'{fmt_hour(h):>5} {avg:5.1f}  {bar} {pct:4.0%}')
    return (f'**{name}** by hour ({ndays} day{"s" if ndays != 1 else ""} of data, Central time)\n'
            '```\n Hour   Avg  Time at 0 players\n' + '\n'.join(rows) + '\n```')


def record_change(state, num, delta):
    s = state.get('daily', {}).get('servers', {}).get(num)
    if s:
        s['joins' if delta > 0 else 'leaves'] += abs(delta)


def format_summary(d, title, state=None, live=False):
    if not d or not d.get('servers'):
        return f'{title}\nNo data yet.'
    lines = [title]
    for num, s in d['servers'].items():
        avg = s['sum'] / s['samples'] if s['samples'] else 0
        lines.append(f'**{s["name"]}**\n'
                     f'  Peak **{s["peak"]}** at {s["peak_at"]} · Low {s["low"]} at {s["low_at"]} · Avg {avg:.1f}\n'
                     f'  Joins {s["joins"]} · Leaves {s["leaves"]}')
        periods = [(datetime.fromisoformat(a), datetime.fromisoformat(b), False)
                   for a, b in s.get('empty_periods', [])]
        if live and state is not None and state.get('empty', {}).get(num):
            periods.append((datetime.fromisoformat(state['empty'][num]), now_tz(), True))
        if periods:
            total = sum((b - a).total_seconds() for a, b, _ in periods)
            lines.append(f'  At 0 players: **{fmt_dur(total)}**. {format_periods(periods, limit=4)}')
    return '\n'.join(lines)


def maybe_post_daily(state):
    d = state.get('daily')
    if d and d['date'] != today_str():
        # split any 0-player stretch at midnight so each day gets its own part
        midnight = now_tz().replace(hour=0, minute=0, second=0, microsecond=0)
        still_empty = list(state.get('empty', {}))
        for num in still_empty:
            close_empty(state, num, midnight)
        day = datetime.strptime(d['date'], '%Y-%m-%d').strftime('%A, %b %d').replace(' 0', ' ')
        send_to_discord(format_summary(d, f'📅 **Daily population summary: {day}**'))
        state['daily'] = {'date': today_str(), 'servers': {}}
        for num in still_empty:
            state['empty'][num] = midnight.isoformat()
        # keep only the last HISTORY_DAYS days of hourly data
        for days in state.get('history', {}).values():
            for old in sorted(days)[:-HISTORY_DAYS]:
                del days[old]


def handle_command(state, text):
    parts = text.strip().split()
    if not parts or not parts[0].startswith('!'):
        return False
    cmd = parts[0].lower()
    arg = parts[1] if len(parts) > 1 else ''
    servers = state['servers']

    if cmd == '!add':
        if not re.fullmatch(r'\d{3,6}', arg):
            send_to_discord('Usage: `!add 1234` (the number at the end of the server name)')
        elif arg in servers:
            send_to_discord(f'Already tracking **{servers[arg]["name"]}**.')
        else:
            servers[arg] = new_entry(arg)
            send_to_discord(f'✅ Added **{arg}**. Player count shows up within a minute.')
        return True
    if cmd in ('!remove', '!del', '!delete'):
        if arg in servers:
            name = servers.pop(arg)['name']
            send_to_discord(f'🗑️ Stopped tracking **{name}**.')
        else:
            send_to_discord(f'Not tracking `{arg or "?"}`. Use `!list` to see what is tracked.')
        return True
    if cmd == '!list':
        if not servers:
            send_to_discord('Not tracking any servers. Add one with `!add 1234`.')
        else:
            lines = [f'• **{s["name"]}**: {s["last"] if s["last"] is not None else "?"} online'
                     for s in servers.values()]
            send_to_discord('**Tracked servers**\n' + '\n'.join(lines))
        return True
    if cmd in ('!today', '!summary', '!stats'):
        send_to_discord(format_summary(state.get('daily'), '📊 **Today so far**', state, live=True))
        return True
    if cmd in ('!empty', '!zero', '!dead'):
        nums = [arg] if arg else list(servers)
        if arg and arg not in servers:
            send_to_discord(f'Not tracking `{arg}`. Use `!list` to see what is tracked.')
        elif not nums:
            send_to_discord('Not tracking any servers.')
        else:
            send_to_discord('🕳️ **Time at 0 players**\n' + '\n'.join(format_empty(state, n) for n in nums))
        return True
    if cmd == '!hours':
        if arg not in servers:
            send_to_discord('Usage: `!hours 1234` (one of the tracked servers, see `!list`)')
        else:
            send_to_discord(format_hours(state, arg))
        return True
    if cmd == '!help':
        send_to_discord(HELP)
        return True
    return False


def check_commands(state, channel_id):
    """Returns True if the server list changed."""
    last_id = state.get('last_msg_id')
    try:
        if last_id:
            msgs = bot_get(f'/channels/{channel_id}/messages?after={last_id}&limit=50')
        else:
            # first run: just remember where the channel is, don't replay old messages
            msgs = bot_get(f'/channels/{channel_id}/messages?limit=1')
            if msgs:
                state['last_msg_id'] = msgs[0]['id']
            return False
    except urllib.error.HTTPError as e:
        print(f'[commands] Discord HTTP {e.code}: {e.read()[:200]!r}')
        return False
    except Exception as e:
        print(f'[commands] {type(e).__name__}: {e}')
        return False

    changed = False
    for m in sorted(msgs, key=lambda m: int(m['id'])):
        state['last_msg_id'] = m['id']
        if m.get('webhook_id') or m.get('author', {}).get('bot'):
            continue
        before = set(state['servers'])
        if handle_command(state, m.get('content', '')):
            print(f'[commands] {m["author"].get("username")}: {m.get("content")}')
            changed = changed or set(state['servers']) != before
    return changed


# ---------- player counts ----------

def check_servers(state):
    stamp = time.strftime('%H:%M:%S')
    try:
        servers = fetch_list()
    except Exception as e:
        print(f'{stamp} [list] {type(e).__name__}: {e}')
        return

    maybe_post_daily(state)
    for num, st in state['servers'].items():
        s = find_server(servers, num)
        if s is None:
            print(f'{stamp} {num}: not in list')
            close_empty(state, num, now_tz())  # offline isn't "empty", stop the stretch
            if not st['down'] and st['last'] is not None:
                send_to_discord(f'⚠️ **{st["name"]}** disappeared from the server list (offline?).')
            elif st['last'] is None and not st['down']:
                send_to_discord(f'❓ Could not find a server ending in **{num}**. Check the number, or remove it with `!remove {num}`.')
            st['down'] = True
            continue

        st['name'] = s.get('Name', num)
        cur = int(s.get('NumPlayers', 0))
        mx = s.get('MaxPlayers', '?')
        last = st['last']
        print(f'{stamp} {st["name"]}: {cur}/{mx}')
        record_sample(state, num, st['name'], cur)
        if last is not None and not st['down'] and cur != last:
            record_change(state, num, cur - last)

        if last is None or st['down']:
            send_to_discord(f'🟢 **Tracking {st["name"]}**\nCurrent Players: {cur} / {mx}')
        elif cur > last:
            send_to_discord(f'🚨 **{st["name"]}** Player Joined! Online: {cur} / {mx} (+{cur - last})')
        elif cur < last:
            send_to_discord(f'👋 **{st["name"]}** Player Left. Online: {cur} / {mx} (-{last - cur})')
        st['last'] = cur
        st['down'] = False


# ---------- main loop ----------

state = load_state()
channel_id = get_channel_id() if (BOT_TOKEN and WEBHOOK_URL) else None
print(f'ARK tracker running... watching {", ".join(state["servers"]) or "nothing"}'
      f'; commands {"on" if channel_id else "off"}')

end_at = time.time() + RUN_SECONDS
next_server_check = 0
while time.time() < end_at:
    if channel_id and check_commands(state, channel_id):
        next_server_check = 0  # new server added: check it right away
    if time.time() >= next_server_check:
        check_servers(state)
        next_server_check = time.time() + SERVER_POLL_SECONDS
    save_state(state)
    time.sleep(COMMAND_POLL_SECONDS if channel_id else SERVER_POLL_SECONDS)

print('Run finished, next run takes over.')
