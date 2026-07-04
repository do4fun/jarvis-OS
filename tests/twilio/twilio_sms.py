import base64
import json
import os
import urllib.parse
import urllib.request
from pathlib import Path

env_path = Path('.env')
values = {}
if env_path.exists():
    for line in env_path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        k, v = line.split('=', 1)
        values[k.strip()] = v.strip().strip('"').strip("'")

sid = values.get('TWILIO_ACCOUNT_SID') or os.environ.get('TWILIO_ACCOUNT_SID')
token = values.get('TWILIO_AUTH_TOKEN') or os.environ.get('TWILIO_AUTH_TOKEN')
from_number = '+14375252879'
to_number = '+15146217663'

if not sid or not token:
    print(json.dumps({'ok': False, 'error': 'MISSING_CREDENTIALS'}))
    raise SystemExit(1)

body = urllib.parse.urlencode({
    'To': to_number,
    'From': from_number,
    'Body': 'Bonjour, ceci est un test SMS depuis Jarvis OS.',
}).encode()

req = urllib.request.Request(
    f'https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json',
    data=body,
    method='POST',
    headers={
        'Authorization': 'Basic ' + base64.b64encode(f'{sid}:{token}'.encode()).decode(),
        'Content-Type': 'application/x-www-form-urlencoded',
    },
)

try:
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read().decode())
        print(json.dumps({
            'ok': True,
            'to': to_number,
            'from': from_number,
            'sid': payload.get('sid'),
            'status': payload.get('status'),
            'direction': payload.get('direction'),
            'body': payload.get('body'),
        }, ensure_ascii=False))
except urllib.error.HTTPError as e:
    body_text = e.read().decode('utf-8', 'ignore')
    print(json.dumps(
        {'ok': False, 'to': to_number, 'status_code': e.code, 'body': body_text},
        ensure_ascii=False,
    ))
except Exception as exc:
    print(json.dumps({'ok': False, 'error': str(exc)}, ensure_ascii=False))
