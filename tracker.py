import os, re, time, json, urllib.request

# Webhook comes from the DISCORD_WEBHOOK secret (never put it in this file)
WEBHOOK_URL = os.environ.get('DISCORD_WEBHOOK', '').strip()

# Official server numbers to watch (the number at the end of the server name)
SERVER_NUMBERS = ['9652', '9349', '9086']
POLL_SECONDS = 60
# How long one cloud run lasts before handing off to the next (GitHub max job time is 6h)
RUN_SECONDS = int(os.environ.get('RUN_SECONDS', 5 * 3600 + 30 * 60))
STATE_FILE = os.environ.get('STATE_FILE', 'state.json')

LIST_URL = 'https://cdn2.arkdedicated.com/servers/asa/officialserverlist.json'
HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/130.0 Safari/537.36'}


def send_to_discord(msg):
    if not WEBHOOK_URL:
        print(f'[webhook] not set, would send: {msg}')
        return
    try:
        data = json.dumps({'content': msg}).encode('utf-8')
        req = urllib.request.Request(WEBHOOK_URL, data=data,
                                     headers={'Content-Type': 'application/json', **HEADERS})
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f'[webhook] error: {e}')


def fetch_list():
    req = urllib.request.Request(LIST_URL, headers=HEADERS)
    return json.load(urllib.request.urlopen(req, timeout=30))


def find_server(servers, number):
    pat = re.compile(rf'(?<!\d){number}$')
    for s in servers:
        if pat.search(str(s.get('Name', '')).strip()):
            return s
    return None


def load_state():
    try:
        with open(STATE_FILE, encoding='utf-8') as f:
            saved = json.load(f)
    except Exception:
        saved = {}
    return {n: saved.get(n, {'last': None, 'down': False, 'name': n}) for n in SERVER_NUMBERS}


def save_state(state):
    with open(STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(state, f, indent=2)


state = load_state()
end_at = time.time() + RUN_SECONDS
print(f'ARK tracker running... watching {", ".join(SERVER_NUMBERS)}')

while time.time() < end_at:
    stamp = time.strftime('%H:%M:%S')
    try:
        servers = fetch_list()
    except Exception as e:
        print(f'{stamp} [list] {type(e).__name__}: {e}')
        time.sleep(POLL_SECONDS)
        continue

    for num in SERVER_NUMBERS:
        st = state[num]
        s = find_server(servers, num)
        if s is None:
            print(f'{stamp} {num}: not in list')
            if not st['down'] and st['last'] is not None:
                send_to_discord(f'⚠️ **{st["name"]}** disappeared from the server list (offline?).')
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

    save_state(state)
    time.sleep(POLL_SECONDS)

print('Run finished, next run takes over.')
