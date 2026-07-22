"""Public Coolify dashboard and restricted proxy for the private CDR API.

The DuckDB token is injected server-side and is never sent to the browser.
Only the API routes required by the dashboard are reachable through this
proxy; the raw SQL endpoint is intentionally not exposed.
"""
import hmac
import os
import urllib.error
import urllib.request

from flask import Flask, Response, jsonify, redirect, render_template, request, stream_with_context


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(
    __name__,
    template_folder=os.path.join(BASE_DIR, 'templates'),
    static_folder=os.path.join(BASE_DIR, 'static'),
)

BACKEND_URL = os.environ.get('CDR_BACKEND_URL', '').strip().rstrip('/')
BACKEND_TOKEN = os.environ.get('CDR_BACKEND_TOKEN', '').strip()
PUBLIC_USERNAME = os.environ.get('CDR_PUBLIC_USERNAME', '').strip()
PUBLIC_PASSWORD = os.environ.get('CDR_PUBLIC_PASSWORD', '')
PUBLIC_AUTH_DISABLED = os.environ.get('CDR_PUBLIC_AUTH_DISABLED', '').strip().lower() in {
    '1', 'true', 'yes', 'on',
}
PROXY_TIMEOUT = max(5, int(os.environ.get('CDR_PROXY_TIMEOUT', '370')))
MAX_REQUEST_BYTES = max(1024, int(os.environ.get('CDR_PROXY_MAX_REQUEST_BYTES', '1048576')))

ALLOWED_API_ROUTES = {
    ('POST', '/api/usa-codes'),
    ('POST', '/api/usa-customer-codes'),
    ('POST', '/api/usa-customer-codes/csv'),
    ('POST', '/api/stir-x5u'),
    ('POST', '/api/stir-x5u/csv'),
    ('POST', '/api/customers'),
    ('GET', '/api/cache-stats'),
    ('GET', '/api/db-stats'),
}


def _configured():
    return bool(
        BACKEND_URL
        and BACKEND_TOKEN
        and (PUBLIC_AUTH_DISABLED or (PUBLIC_USERNAME and PUBLIC_PASSWORD))
    )


def _valid_basic_auth():
    if PUBLIC_AUTH_DISABLED:
        return True
    auth = request.authorization
    if not auth:
        return False
    return (
        hmac.compare_digest((auth.username or '').encode(), PUBLIC_USERNAME.encode())
        and hmac.compare_digest((auth.password or '').encode(), PUBLIC_PASSWORD.encode())
    )


def _unauthorized():
    return Response(
        'Authentication required',
        401,
        {'WWW-Authenticate': 'Basic realm="CDR Direct", charset="UTF-8"'},
        mimetype='text/plain',
    )


@app.before_request
def require_public_auth():
    if request.path in {'/health', '/ready'}:
        return None
    if not _configured():
        return jsonify({'error': 'frontend proxy is not fully configured'}), 503
    if not _valid_basic_auth():
        return _unauthorized()
    return None


@app.after_request
def security_headers(response):
    response.headers.setdefault('X-Content-Type-Options', 'nosniff')
    response.headers.setdefault('X-Frame-Options', 'DENY')
    response.headers.setdefault('Referrer-Policy', 'no-referrer')
    response.headers.setdefault('Cache-Control', 'no-store')
    return response


@app.route('/health', methods=['GET'])
def health():
    return jsonify({'ok': True, 'service': 'cdr-direct-frontend'})


def _backend_request(path, method='GET', body=None, timeout=None):
    headers = {
        'Accept': request.headers.get('Accept', 'application/json'),
        'X-Auth-Token': BACKEND_TOKEN,
    }
    if body is not None:
        headers['Content-Type'] = request.headers.get('Content-Type', 'application/json')
    upstream_request = urllib.request.Request(
        BACKEND_URL + path,
        data=body,
        headers=headers,
        method=method,
    )
    return urllib.request.urlopen(upstream_request, timeout=timeout or PROXY_TIMEOUT)


@app.route('/ready', methods=['GET'])
def ready():
    if not _configured():
        return jsonify({'ok': False, 'service': 'cdr-direct-frontend'}), 503
    try:
        upstream = _backend_request('/ready', timeout=5)
        try:
            ok = upstream.getcode() == 200
        finally:
            upstream.close()
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
        ok = False
    return jsonify({'ok': ok, 'service': 'cdr-direct-frontend'}), (200 if ok else 503)


@app.route('/', methods=['GET'])
def root():
    return redirect('/ui')


@app.route('/ui', methods=['GET'])
def ui():
    return render_template('index.html', auth_mode='proxy')


def _proxy_response(upstream):
    status = upstream.getcode()
    headers = {}
    for name in ('Content-Type', 'Content-Disposition', 'Cache-Control'):
        value = upstream.headers.get(name)
        if value:
            headers[name] = value

    @stream_with_context
    def generate():
        try:
            while True:
                chunk = upstream.read(64 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            upstream.close()

    return Response(generate(), status=status, headers=headers)


@app.route('/api/<path:subpath>', methods=['GET', 'POST'])
def proxy_api(subpath):
    path = '/api/' + subpath
    if (request.method, path) not in ALLOWED_API_ROUTES:
        return jsonify({'error': 'route is not exposed by the frontend proxy'}), 404
    if request.content_length is not None and request.content_length > MAX_REQUEST_BYTES:
        return jsonify({'error': 'request body is too large'}), 413

    body = request.get_data(cache=False) if request.method == 'POST' else None
    query = request.query_string.decode('ascii', errors='ignore')
    upstream_path = path + (('?' + query) if query else '')
    try:
        upstream = _backend_request(upstream_path, request.method, body)
        return _proxy_response(upstream)
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        content_type = exc.headers.get('Content-Type', 'application/json')
        exc.close()
        return Response(payload, status=exc.code, content_type=content_type)
    except (urllib.error.URLError, TimeoutError, OSError):
        return jsonify({'error': 'private CDR service is unavailable'}), 502


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8090)
