import base64
import json
import os
import urllib.parse
import urllib.request
from pathlib import Path

env_path = Path(__file__).resolve().parent.parent.parent / ".env"
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
page_id = values.get('TWILIO_MESSENGER_PAGE_ID') or os.environ.get('TWILIO_MESSENGER_PAGE_ID')
to_psid = values.get('TWILIO_MESSENGER_TO_PSID') or os.environ.get('TWILIO_MESSENGER_TO_PSID')

if not sid or not token or not page_id or not to_psid:
    print(json.dumps({
        'ok': False,
        'error': 'MISSING_MESSENGER_CONFIG',
        'required': [
            'TWILIO_ACCOUNT_SID', 'TWILIO_AUTH_TOKEN',
            'TWILIO_MESSENGER_PAGE_ID', 'TWILIO_MESSENGER_TO_PSID',
        ],
    }, ensure_ascii=False))
    raise SystemExit(1)

body = urllib.parse.urlencode({
    'To': f'messenger:{to_psid}',
    'From': f'messenger:{page_id}',
    'Body': 'Bonjour, ceci est un test Messenger depuis Jarvis OS.',
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
            'to': f'messenger:{to_psid}',
            'from': f'messenger:{page_id}',
            'sid': payload.get('sid'),
            'status': payload.get('status'),
            'direction': payload.get('direction'),
            'body': payload.get('body'),
        }, ensure_ascii=False))
except urllib.error.HTTPError as e:
    body_text = e.read().decode('utf-8', 'ignore')
    print(json.dumps(
        {
            'ok': False,
            'to': f'messenger:{to_psid}',
            'status_code': e.code,
            'body': body_text,
        },
        ensure_ascii=False,
    ))
except Exception as exc:
    print(json.dumps({'ok': False, 'error': str(exc)}, ensure_ascii=False))
