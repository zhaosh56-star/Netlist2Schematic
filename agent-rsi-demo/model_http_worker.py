"""Private HTTP transport. Secrets arrive through stdin, never command arguments."""
import json
import sys
import urllib.error
import urllib.request

sys.stdin.reconfigure(encoding='utf-8')
sys.stdout.reconfigure(encoding='utf-8')


def main():
    data = json.load(sys.stdin)
    try:
        request = urllib.request.Request(data['url'], data=json.dumps(data['body'], ensure_ascii=False).encode('utf-8'),
                                         headers=data['headers'], method='POST')
        with urllib.request.urlopen(request, timeout=data['timeout']) as response:
            raw = response.read(3000001)
        if len(raw) > 3000000:
            raise ValueError('response too large')
        print(json.dumps({'response': json.loads(raw.decode('utf-8'))}, ensure_ascii=False))
    except urllib.error.HTTPError as exc:
        print(json.dumps({'error': 'HTTP ' + str(exc.code), 'retryable': exc.code in (408, 429, 500, 502, 503, 504)}))
    except Exception:
        print(json.dumps({'error': 'HTTP transport failed', 'retryable': True}))


if __name__ == '__main__':
    main()
