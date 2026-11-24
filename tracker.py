import os, re, time, json, urllib.request, urllib.error
from datetime import datetime
from zoneinfo import ZoneInfo

# Daily summary is posted when the day rolls over in this time zone
TZ = ZoneInfo(os.environ.get('TRACKER_TZ', 'America/Chicago'))

# Secrets (set in GitHub, never in this file)
WEBHOOK_URL = os.environ.get('DISCORD_WEBHOOK', '').strip()
BOT_TOKEN = os.environ.get('DISCORD_BOT_TOKEN', '').strip()
BOT_NAME = os.environ.get('BOT_NAME', 'ARK Tracker')  # name shown on every message

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


def send_to_discord(msg, ping=False):
    if not WEBHOOK_URL:
        print(f'[webhook] not set, would send: {msg}')
        return
    try:
        # only generator alerts may ping roles; everything else never pings anyone
        body = {'content': msg[:2000], 'username': BOT_NAME,
                'allowed_mentions': {'parse': ['roles'] if ping else []}}
        http_json(WEBHOOK_URL, 'POST', body, headers={'User-Agent': DISCORD_UA}, timeout=10)
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
    if 'empty' in saved:  # older 0-only tracking -> general low-count stretches
        st = saved.setdefault('stretch', {})
        for num, since in saved.pop('empty').items():
            st.setdefault(num, {'count': 0, 'since': since})
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
        '`!info` (or `!info 1234`) map, in-game day, game version, and whether character/item/dino uploads are allowed\n'
        '`!today` population stats so far today\n'
        '`!empty` when each server sat at 0 players today + hours it is usually empty\n'
        '`!at 1234 1` when server 1234 had exactly 1 player today, how long, and hours that usually happens (works for 0-3)\n'
        '`!low 1234` time spent at 0, 1, 2 and 3 players today\n'
        '`!hours 1234` average players and % of time at 0 / 1 player for every hour of the day\n'
        '`!find low island na` lowest-pop official servers matching your words (`high`, `empty`, map, region, pvp/pve, small)\n'
        '`!addgen base1 500` track a TEK generator (add a 3rd number for hours per element, default 18)\n'
        '`!gen base1 500` update after refilling · `!gens` list all · `!removegen base1`\n'
        '`!genrole @Role` who to ping when low · `!genwarn 12` warn this many hours before empty')

LOW_MAX = 3              # stretches are tracked for player counts 0..LOW_MAX
HISTORY_DAYS = 14        # how many days of hourly data to keep
USUALLY_PCT = 0.6        # an hour counts as "usually at N" if it was at N this much of the time


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


def players_word(n):
    return f'{n} player' if n == 1 else f'{n} players'


def close_stretch(state, num, end_dt):
    """End the open 'server sat at N players' stretch and log it in today's stats."""
    st = state.setdefault('stretch', {}).pop(num, None)
    if not st:
        return
    s = state.get('daily', {}).get('servers', {}).get(num)
    if s is not None:
        s.setdefault('stretches', []).append([st['count'], st['since'], end_dt.isoformat()])


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

    # stretches at a low player count (0..LOW_MAX)
    stretch = state.setdefault('stretch', {})
    if num in stretch and stretch[num]['count'] != cur:
        close_stretch(state, num, now)
    if cur <= LOW_MAX and num not in stretch:
        stretch[num] = {'count': cur, 'since': now.isoformat()}

    # hourly history: [samples, samples at 0, total players, samples at 1, at 2, at 3]
    hist = state.setdefault('history', {}).setdefault(num, {})
    day = hist.setdefault(today_str(), [[0] * (3 + LOW_MAX) for _ in range(24)])
    slot = day[now.hour]
    slot.extend([0] * (3 + LOW_MAX - len(slot)))  # older entries only had 3 fields
    slot[0] += 1
    slot[2] += cur
    if cur == 0:
        slot[1] += 1
    elif cur <= LOW_MAX:
        slot[2 + cur] += 1


def record_change(state, num, delta):
    s = state.get('daily', {}).get('servers', {}).get(num)
    if s:
        s['joins' if delta > 0 else 'leaves'] += abs(delta)


def periods_today(state, num, count, daily=None, live=True):
    """Stretches at exactly `count` players today, as (start, end, ongoing)."""
    s = (daily or state.get('daily', {})).get('servers', {}).get(num, {})
    out = [(datetime.fromisoformat(a), datetime.fromisoformat(b), False)
           for c, a, b in s.get('stretches', []) if c == count]
    if count == 0:  # from the older 0-only format
        out += [(datetime.fromisoformat(a), datetime.fromisoformat(b), False)
                for a, b in s.get('empty_periods', [])]
    st = state.get('stretch', {}).get(num)
    if live and st and st['count'] == count:
        out.append((datetime.fromisoformat(st['since']), now_tz(), True))
    return sorted(out)


def total_secs(periods):
    return sum((b - a).total_seconds() for a, b, _ in periods)


def format_periods(periods, limit=6):
    parts = [f'{fmt_t(a)}–{"now" if live else fmt_t(b)} ({fmt_dur((b - a).total_seconds())})'
             for a, b, live in periods]
    if len(parts) > limit:
        # show the longest ones
        keep = sorted(range(len(periods)), key=lambda i: -(periods[i][1] - periods[i][0]).total_seconds())[:limit]
        parts = [parts[i] for i in sorted(keep)] + [f'+{len(parts) - limit} shorter']
    return ', '.join(parts)


def hourly_stats(state, num):
    """Per hour of day across kept history: dict with avg players and fraction of time at 0..LOW_MAX."""
    days = state.get('history', {}).get(num, {})
    out = []
    for h in range(24):
        slots = [d[h] + [0] * (3 + LOW_MAX - len(d[h])) for d in days.values()]
        n = sum(x[0] for x in slots)
        at = [sum(x[1] for x in slots)] + [sum(x[2 + k] for x in slots) for k in range(1, LOW_MAX + 1)]
        out.append({'n': n, 'avg': sum(x[2] for x in slots) / n if n else None,
                    'at': [a / n if n else None for a in at]})
    return out, len(days)


def usually_ranges(stats, count):
    """Merge hours usually at `count` players into ranges like '3 AM–6 AM (92%)'."""
    ranges, start, pcts = [], None, []
    for h in range(25):
        pct = stats[h]['at'][count] if h < 24 else None
        if pct is not None and pct >= USUALLY_PCT:
            if start is None:
                start, pcts = h, []
            pcts.append(pct)
        elif start is not None:
            ranges.append(f'{fmt_hour(start)}–{fmt_hour(h % 24)} ({sum(pcts) / len(pcts):.0%})')
            start = None
    return ranges


def format_at(state, num, count):
    name = state['servers'].get(num, {}).get('name', num)
    periods = periods_today(state, num, count)
    lines = [f'**{name}**']
    if periods:
        longest = max(periods, key=lambda p: p[1] - p[0])
        lines.append(f'  At {players_word(count)} today: **{fmt_dur(total_secs(periods))}** total, '
                     f'{len(periods)} time{"s" if len(periods) != 1 else ""}, longest '
                     f'{fmt_dur((longest[1] - longest[0]).total_seconds())}')
        lines.append(f'  {format_periods(periods)}')
    else:
        lines.append(f'  Never at {players_word(count)} today.')
    stats, ndays = hourly_stats(state, num)
    ranges = usually_ranges(stats, count)
    label = 'Usually empty' if count == 0 else f'Usually at {players_word(count)}'
    if ndays < 2:
        lines.append(f'  {label}: still learning ({ndays} day of data so far).')
    elif ranges:
        lines.append(f'  {label} (last {ndays} days): {", ".join(ranges)}')
    else:
        lines.append(f'  {label} (last {ndays} days): no hour where that reliably happens.')
    return '\n'.join(lines)


def format_low(state, num):
    name = state['servers'].get(num, {}).get('name', num)
    lines = [f'**{name}**: time at low player counts today']
    for c in range(LOW_MAX + 1):
        p = periods_today(state, num, c)
        if p:
            longest = max(p, key=lambda x: x[1] - x[0])
            lines.append(f'  {players_word(c)}: **{fmt_dur(total_secs(p))}** ({len(p)}x, longest '
                         f'{fmt_dur((longest[1] - longest[0]).total_seconds())})')
        else:
            lines.append(f'  {players_word(c)}: none')
    return '\n'.join(lines)


def format_hours(state, num):
    name = state['servers'].get(num, {}).get('name', num)
    stats, ndays = hourly_stats(state, num)
    rows = []
    for h, st in enumerate(stats):
        if st['n'] == 0:
            rows.append(f'{fmt_hour(h):>5}    -    -    -   no data yet')
        else:
            z, one = st['at'][0], st['at'][1]
            bar = '█' * round((z + one) * 10) + '░' * (10 - round((z + one) * 10))
            rows.append(f'{fmt_hour(h):>5} {st["avg"]:5.1f} {z:4.0%} {one:4.0%} {bar}')
    return (f'**{name}** by hour ({ndays} day{"s" if ndays != 1 else ""} of data, Central time)\n'
            '```\n Hour   Avg   @0   @1  time at 0-1 players\n' + '\n'.join(rows) + '\n```')


def format_summary(d, title, state=None, live=False):
    if not d or not d.get('servers'):
        return f'{title}\nNo data yet.'
    lines = [title]
    for num, s in d['servers'].items():
        avg = s['sum'] / s['samples'] if s['samples'] else 0
        lines.append(f'**{s["name"]}**\n'
                     f'  Peak **{s["peak"]}** at {s["peak_at"]} · Low {s["low"]} at {s["low_at"]} · Avg {avg:.1f}\n'
                     f'  Joins {s["joins"]} · Leaves {s["leaves"]}')
        st = state or {}
        for c in (0, 1):
            p = periods_today(st, num, c, daily=d, live=live)
            if p:
                lines.append(f'  At {players_word(c)}: **{fmt_dur(total_secs(p))}**. {format_periods(p, limit=3)}')
    return '\n'.join(lines)


def maybe_post_daily(state):
    d = state.get('daily')
    if d and d['date'] != today_str():
        # split any open low-count stretch at midnight so each day gets its own part
        midnight = now_tz().replace(hour=0, minute=0, second=0, microsecond=0)
        open_stretches = {n: st['count'] for n, st in state.get('stretch', {}).items()}
        for num in open_stretches:
            close_stretch(state, num, midnight)
        day = datetime.strptime(d['date'], '%Y-%m-%d').strftime('%A, %b %d').replace(' 0', ' ')
        send_to_discord(format_summary(d, f'📅 **Daily population summary: {day}**', state))
        state['daily'] = {'date': today_str(), 'servers': {}}
        for num, c in open_stretches.items():
            state['stretch'][num] = {'count': c, 'since': midnight.isoformat()}
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
            lines = []
            for s in servers.values():
                up = s.get('info', {}).get('uploads')
                lines.append(f'• **{s["name"]}**: {s["last"] if s["last"] is not None else "?"} online'
                             + (f' · uploads: {"".join("✅" if v else "❌" for v in up.values())} (chars/items/dinos)'
                                if up else ''))
            send_to_discord('**Tracked servers**\n' + '\n'.join(lines))
        return True
    if cmd in ('!info', '!server'):
        nums = [arg] if arg else list(servers)
        if arg and arg not in servers:
            send_to_discord(f'Not tracking `{arg}`. Use `!list` to see what is tracked.')
        else:
            send_to_discord('\n'.join(format_info(servers[n]) for n in nums) or 'Not tracking any servers.')
        return True
    if cmd in ('!today', '!summary', '!stats'):
        send_to_discord(format_summary(state.get('daily'), '📊 **Today so far**', state, live=True))
        return True
    if cmd in ('!empty', '!zero', '!dead', '!at', '!one', '!solo'):
        count = 1 if cmd in ('!one', '!solo') else 0
        if cmd == '!at':
            n = parts[2] if len(parts) > 2 else ''
            if not n.isdigit() or int(n) > LOW_MAX:
                send_to_discord(f'Usage: `!at 1234 1` (server number, then a player count from 0 to {LOW_MAX})')
                return True
            count = int(n)
        nums = [arg] if arg else list(servers)
        if arg and arg not in servers:
            send_to_discord(f'Not tracking `{arg}`. Use `!list` to see what is tracked.')
        elif not nums:
            send_to_discord('Not tracking any servers.')
        else:
            icon = '🕳️' if count == 0 else '👤'
            send_to_discord(f'{icon} **Time at {players_word(count)}**\n'
                            + '\n'.join(format_at(state, n, count) for n in nums))
        return True
    if cmd == '!low':
        nums = [arg] if arg else list(servers)
        if arg and arg not in servers:
            send_to_discord(f'Not tracking `{arg}`. Use `!list` to see what is tracked.')
        else:
            send_to_discord('\n'.join(format_low(state, n) for n in nums) or 'Not tracking any servers.')
        return True
    if cmd == '!hours':
        if arg not in servers:
            send_to_discord('Usage: `!hours 1234` (one of the tracked servers, see `!list`)')
        else:
            send_to_discord(format_hours(state, arg))
        return True
    if cmd in ('!addgen', '!gen', '!refill', '!removegen', '!delgen', '!gens', '!genrole', '!genwarn'):
        gen_command(state, cmd, parts)
        return True
    if cmd == '!find':
        find_command(parts)
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
        LAST_LIST['servers'], LAST_LIST['at'] = servers, time.time()
    except Exception as e:
        print(f'{stamp} [list] {type(e).__name__}: {e}')
        return

    maybe_post_daily(state)
    for num, st in state['servers'].items():
        s = find_server(servers, num)
        if s is None:
            print(f'{stamp} {num}: not in list')
            close_stretch(state, num, now_tz())  # offline isn't "empty", stop the stretch
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
        check_server_info(st, s)


# ---------- uploads / version / day counter ----------

UPLOAD_KEYS = [('AllowDownloadChars', 'Characters'), ('AllowDownloadItems', 'Items'), ('AllowDownloadDinos', 'Dinos')]


def server_info(s):
    def num(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return None
    return {'uploads': {label: bool(num(s.get(k))) for k, label in UPLOAD_KEYS},
            'version': f'{s.get("BuildId", "?")}.{s.get("MinorBuildId", "?")}',
            'day': num(s.get('DayTime')),
            'map': str(s.get('MapName', '')).replace('_WP', '')}


def uploads_text(up):
    return ' · '.join(f'{k} {"✅" if v else "❌"}' for k, v in up.items())


def check_server_info(st, s):
    """Alert when uploads, game version or the day counter change. Saves the latest info on the server entry."""
    new, old = server_info(s), st.get('info')
    st['info'] = new
    if not old:
        return
    name = st['name']
    changed = [k for k in new['uploads'] if old['uploads'].get(k) != new['uploads'][k]]
    if changed:
        parts = [f'{k} {"ON ✅" if new["uploads"][k] else "OFF ❌"}' for k in changed]
        send_to_discord(f'📦 **{name}** uploads changed: {", ".join(parts)}\n'
                        f'Now: {uploads_text(new["uploads"])}')
    if old['version'] != new['version'] and '?' not in new['version']:
        send_to_discord(f'🔄 **{name}** updated: v{old["version"]} → v{new["version"]}')
    if old.get('day') and new['day'] is not None and new['day'] < old['day']:
        send_to_discord(f'🔙 **{name}** in-game day went backwards (Day {old["day"]} → Day {new["day"]}). '
                        'The server loaded an older save: likely a **rollback** (or a wipe if it dropped a lot).')


# ---------- TEK generator timers ----------

GEN_HOURS_PER_ELEMENT = 18.0   # default burn rate at default range; set per generator if yours differs
GEN_WARN_HOURS = 12            # default warning before a generator runs out


def gen_remaining(g):
    """Elements left right now (can be fractional) and seconds until empty."""
    burned = (time.time() - g['set_at']) / 3600 / g['hpe']
    left = max(0.0, g['elements'] - burned)
    return left, left * g['hpe'] * 3600


def fmt_left(secs):
    if secs <= 0:
        return 'EMPTY'
    d, rem = divmod(int(secs), 86400)
    h, m = divmod(rem // 60, 60)
    return f'{d}d {h}h' if d else f'{h}h {m}m'


def format_gens(state):
    gens = state.get('gens', {})
    if not gens:
        return 'No generators yet. Add one with `!addgen base1 500`.'
    cfg = state.get('gen_cfg', {})
    lines = ['⚡ **TEK generators**']
    for name, g in sorted(gens.items(), key=lambda kv: gen_remaining(kv[1])[1]):
        left, secs = gen_remaining(g)
        empty_at = datetime.fromtimestamp(time.time() + secs, TZ)
        icon = '🔴' if secs <= 0 else '🟠' if secs < cfg.get('warn', GEN_WARN_HOURS) * 3600 else '🟢'
        when = '' if secs <= 0 else f' · empty {empty_at.strftime("%a %b %d").replace(" 0", " ")} {fmt_t(empty_at)}'
        status = 'EMPTY' if secs <= 0 else f'{fmt_left(secs)} left'
        lines.append(f'{icon} **{name}**: ~{int(left)} element · {status}{when}'
                     + (f' · {g["hpe"]:g}h/element' if g['hpe'] != GEN_HOURS_PER_ELEMENT else ''))
    lines.append(f'Warning at {cfg.get("warn", GEN_WARN_HOURS)}h left'
                 + (f', pinging {cfg["role"]}' if cfg.get('role') else ' (set a role with `!genrole @Role`)'))
    return '\n'.join(lines)


def check_gens(state):
    cfg = state.get('gen_cfg', {})
    warn = cfg.get('warn', GEN_WARN_HOURS) * 3600
    ping = (cfg.get('role', '') + ' ') if cfg.get('role') else ''
    for name, g in state.get('gens', {}).items():
        left, secs = gen_remaining(g)
        if secs <= 0 and not g.get('alerted_empty'):
            send_to_discord(f'🔴 {ping}TEK generator **{name}** is OUT of element! Refill it, then `!gen {name} <count>`.',
                            ping=True)
            g['alerted_empty'] = g['alerted_low'] = True
        elif 0 < secs < warn and not g.get('alerted_low'):
            send_to_discord(f'🟠 {ping}TEK generator **{name}** is low: ~{int(left)} element, {fmt_left(secs)} left.',
                            ping=True)
            g['alerted_low'] = True


def gen_command(state, cmd, parts):
    gens = state.setdefault('gens', {})
    cfg = state.setdefault('gen_cfg', {})
    if cmd in ('!addgen', '!gen', '!refill'):
        if len(parts) < 3 or not re.fullmatch(r'\d{1,5}', parts[2]):
            send_to_discord('Usage: `!addgen base1 500` (name, then element count). '
                            'Optional burn rate: `!addgen base1 500 6` = 1 element every 6h.')
            return
        name = parts[1][:30]
        hpe = gens.get(name, {}).get('hpe', GEN_HOURS_PER_ELEMENT)
        if len(parts) > 3:
            try:
                hpe = max(0.1, float(parts[3]))
            except ValueError:
                send_to_discord('Burn rate must be a number of hours per element, e.g. `6` or `18`.')
                return
        gens[name] = {'elements': int(parts[2]), 'set_at': time.time(), 'hpe': hpe}
        left, secs = gen_remaining(gens[name])
        send_to_discord(f'⚡ **{name}** set to {parts[2]} element: lasts about **{fmt_left(secs)}**.')
    elif cmd in ('!removegen', '!delgen'):
        name = parts[1] if len(parts) > 1 else ''
        if gens.pop(name, None) is not None:
            send_to_discord(f'🗑️ Removed generator **{name}**.')
        else:
            send_to_discord(f'No generator named `{name or "?"}`. See `!gens`.')
    elif cmd == '!gens':
        send_to_discord(format_gens(state))
    elif cmd == '!genrole':
        m = re.search(r'<@&\d+>', ' '.join(parts[1:]))
        if m:
            cfg['role'] = m.group(0)
            send_to_discord(f'Generator alerts will ping {cfg["role"]}.', ping=False)
        elif len(parts) > 1 and parts[1].lower() in ('off', 'none'):
            cfg.pop('role', None)
            send_to_discord('Generator alerts will not ping anyone.')
        else:
            send_to_discord('Usage: `!genrole @Role` (mention the role) or `!genrole off`.')
    elif cmd == '!genwarn':
        if len(parts) > 1 and parts[1].isdigit():
            cfg['warn'] = int(parts[1])
            for g in gens.values():
                g['alerted_low'] = False
            send_to_discord(f'Generators will warn at {cfg["warn"]}h left.')
        else:
            send_to_discord('Usage: `!genwarn 12` (hours before empty).')


# ---------- server finder ----------

LAST_LIST = {'at': 0, 'servers': []}
REGIONS = {'na', 'eu', 'oc', 'asia', 'sa'}


def get_list_cached(max_age=90):
    if time.time() - LAST_LIST['at'] > max_age:
        LAST_LIST['servers'] = fetch_list()
        LAST_LIST['at'] = time.time()
    return LAST_LIST['servers']


def find_command(parts):
    words = [w.lower() for w in parts[1:]]
    mode = 'low'
    for m in ('low', 'high', 'empty'):
        if m in words:
            mode = m
            words.remove(m)
    try:
        servers = get_list_cached()
    except Exception as e:
        send_to_discord(f'Could not load the server list right now ({type(e).__name__}). Try again in a minute.')
        return
    hits = []
    for s in servers:
        name = str(s.get('Name', ''))
        low = name.lower()
        if s.get('HasPassword') or not name:
            continue
        ok = True
        for w in words:
            if w in REGIONS or w == 'as':
                ok = low.startswith(('asia' if w == 'as' else w) + '-')
            elif w in ('small', 'smalltribes'):
                ok = 'smalltribes' in low
            elif w in ('pve', 'pvp'):
                ok = f'-{w}-' in low
            else:
                ok = w in low.replace('-', '') or w in str(s.get('MapName', '')).lower()
            if not ok:
                break
        if ok:
            hits.append(s)
    if mode == 'empty':
        hits = [s for s in hits if int(s.get('NumPlayers', 0)) == 0]
    hits.sort(key=lambda s: int(s.get('NumPlayers', 0)), reverse=(mode == 'high'))
    if not hits:
        send_to_discord(f'No official servers match `{" ".join(parts[1:]) or "(nothing)"}`.')
        return
    shown = hits[:15]
    rows = [f'{int(s.get("NumPlayers", 0)):>2}/{s.get("MaxPlayers", "?"):<3} {s.get("Name")}' for s in shown]
    title = {'low': 'Lowest pop', 'high': 'Highest pop', 'empty': 'Empty right now'}[mode]
    send_to_discord(f'🔎 **{title}**: {" ".join(words) or "all officials"} ({len(hits)} match, showing {len(shown)})\n'
                    '```\n' + '\n'.join(rows) + '\n```Track one with `!add <number>`.')


def format_info(st):
    i = st.get('info')
    if not i:
        return f'**{st["name"]}**: no info yet (server not seen since tracking started)'
    return (f'**{st["name"]}** ({i["map"]})\n'
            f'  Players: {st["last"] if st["last"] is not None else "?"} · Day {i["day"] if i["day"] is not None else "?"} '
            f'· v{i["version"]}{" · ⚠️ offline" if st.get("down") else ""}\n'
            f'  Uploads: {uploads_text(i["uploads"])}')


# ---------- main loop ----------

state = load_state()
channel_id = get_channel_id() if (BOT_TOKEN and WEBHOOK_URL) else None
print(f'ARK tracker running... watching {", ".join(state["servers"]) or "nothing"}'
      f'; commands {"on" if channel_id else "off"}')

end_at = time.time() + RUN_SECONDS
next_server_check = 0
import traceback
reported_errors = set()

while time.time() < end_at:
    # a bug in one part must never stop the whole tracker: log it, tell Discord once, keep going
    for step in ('commands', 'gens', 'servers'):
        try:
            if step == 'commands' and channel_id and check_commands(state, channel_id):
                next_server_check = 0  # new server added: check it right away
            elif step == 'gens':
                check_gens(state)
            elif step == 'servers' and time.time() >= next_server_check:
                next_server_check = time.time() + SERVER_POLL_SECONDS
                check_servers(state)
        except Exception as e:
            traceback.print_exc()
            key = f'{step}:{type(e).__name__}:{e}'
            if key not in reported_errors:
                reported_errors.add(key)
                send_to_discord(f'🐞 Tracker hit an error in **{step}** (still running): `{type(e).__name__}: {e}`'[:1900])
    try:
        save_state(state)
    except Exception:
        traceback.print_exc()
    time.sleep(COMMAND_POLL_SECONDS if channel_id else SERVER_POLL_SECONDS)

print('Run finished, next run takes over.')
