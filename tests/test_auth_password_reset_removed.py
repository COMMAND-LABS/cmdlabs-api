"""The password-reset routes are gone.

Sign-in is by emailed one-time code. The reset routes set a password nothing
read, and the request route was unauthenticated, unthrottled and revealed
whether an email had an account. They must not come back by accident.
"""
from httpx import ASGITransport, AsyncClient

from src.main import app


async def test_password_reset_routes_are_not_mounted():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        for path, body in (
            ("/api/auth/request-password-reset", {"email": "someone@example.com"}),
            ("/api/auth/reset-password", {"accountId": 1, "resetToken": "t", "newPassword": "p"}),
        ):
            resp = await c.post(path, json=body)
            assert resp.status_code == 404, (path, resp.status_code)
