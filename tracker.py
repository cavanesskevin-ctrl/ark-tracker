import os, re, time, json, urllib.request, urllib.error

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
        '`!list` show tracked servers')


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

    for num, st in state['servers'].items():
        s = find_server(servers, num)
        if s is None:
            print(f'{stamp} {num}: not in list')
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
