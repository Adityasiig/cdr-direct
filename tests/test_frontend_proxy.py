import base64
import unittest
from unittest import mock

import frontend_proxy


class FakeUpstream:
    def __init__(self, payload=b'{"ok":true}', status=200, content_type='application/json'):
        self.payload = payload
        self.status = status
        self.headers = {'Content-Type': content_type}
        self.closed = False

    def getcode(self):
        return self.status

    def read(self, _size=-1):
        payload, self.payload = self.payload, b''
        return payload

    def close(self):
        self.closed = True


class FrontendProxyTests(unittest.TestCase):
    def setUp(self):
        frontend_proxy.app.config.update(TESTING=True)
        self.patchers = [
            mock.patch.object(frontend_proxy, 'BACKEND_URL', 'https://cdr-api.example.com'),
            mock.patch.object(frontend_proxy, 'BACKEND_TOKEN', 'backend-secret'),
            mock.patch.object(frontend_proxy, 'PUBLIC_USERNAME', 'viewer'),
            mock.patch.object(frontend_proxy, 'PUBLIC_PASSWORD', 'public-secret'),
            mock.patch.object(frontend_proxy, 'PUBLIC_AUTH_DISABLED', False),
        ]
        for patcher in self.patchers:
            patcher.start()
        self.client = frontend_proxy.app.test_client()

    def tearDown(self):
        for patcher in reversed(self.patchers):
            patcher.stop()

    @staticmethod
    def auth_headers(extra=None):
        encoded = base64.b64encode(b'viewer:public-secret').decode('ascii')
        headers = {'Authorization': 'Basic ' + encoded}
        headers.update(extra or {})
        return headers

    def test_dashboard_requires_public_login_and_uses_proxy_auth_mode(self):
        self.assertEqual(self.client.get('/ui').status_code, 401)
        response = self.client.get('/ui', headers=self.auth_headers())
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'name="cdr-auth-mode" content="proxy"', response.data)

    def test_proxy_injects_backend_token_and_does_not_forward_browser_token(self):
        captured = {}

        def fake_urlopen(upstream_request, timeout):
            captured['request'] = upstream_request
            captured['timeout'] = timeout
            return FakeUpstream(b'{"rows":[]}')

        headers = self.auth_headers({
            'Content-Type': 'application/json',
            'X-Auth-Token': 'untrusted-browser-token',
        })
        with mock.patch.object(frontend_proxy.urllib.request, 'urlopen', side_effect=fake_urlopen):
            response = self.client.post(
                '/api/usa-customer-codes', data=b'{}', headers=headers,
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {'rows': []})
        self.assertEqual(captured['request'].get_header('X-auth-token'), 'backend-secret')
        self.assertIsNone(captured['request'].get_header('Authorization'))

    def test_proxy_exposes_only_dashboard_routes(self):
        response = self.client.get('/api/not-allowed', headers=self.auth_headers())
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.client.post('/sql', headers=self.auth_headers()).status_code, 404)

    def test_readiness_checks_private_backend(self):
        with mock.patch.object(
            frontend_proxy.urllib.request, 'urlopen', return_value=FakeUpstream(),
        ):
            response = self.client.get('/ready')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['ok'], True)


if __name__ == '__main__':
    unittest.main()
