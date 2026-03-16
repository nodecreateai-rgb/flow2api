from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api import admin


class DummyDB:
    def __init__(self, existing_token=None, stored_token=None, connection_token="conn-token"):
        self._existing_token = existing_token
        self._stored_token = stored_token
        self._connection_token = connection_token

    async def get_plugin_config(self):
        return SimpleNamespace(connection_token=self._connection_token, auto_enable_on_update=False)

    async def get_token_by_email(self, email):
        return self._existing_token

    async def get_token(self, token_id):
        return self._stored_token


class DummyTokenManager:
    def __init__(self, st_to_at_result=None):
        self.flow_client = SimpleNamespace(st_to_at=self._st_to_at)
        self._st_to_at_result = st_to_at_result or {}
        self.update_calls = []

    async def _st_to_at(self, session_token):
        return self._st_to_at_result

    async def update_token(self, **kwargs):
        self.update_calls.append(kwargs)

    async def enable_token(self, token_id):
        return None


def build_client(db, token_manager):
    app = FastAPI()
    admin.set_dependencies(token_manager, pm=None, database=db, cm=None)
    app.include_router(admin.router)
    return TestClient(app)


def test_plugin_update_token_rejects_missing_expires():
    existing = SimpleNamespace(id=8, is_active=True)
    db = DummyDB(existing_token=existing)
    token_manager = DummyTokenManager(
        st_to_at_result={
            "access_token": "new-at",
            "user": {"email": "user@example.com"},
        }
    )
    client = build_client(db, token_manager)

    response = client.post(
        "/api/plugin/update-token",
        json={"session_token": "fresh-st"},
        headers={"Authorization": "Bearer conn-token"},
    )

    assert response.status_code == 400
    assert "did not return expires" in response.json()["detail"]
    assert token_manager.update_calls == []


def test_plugin_update_token_rejects_expired_expires():
    existing = SimpleNamespace(id=8, is_active=True)
    expired = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
    db = DummyDB(existing_token=existing)
    token_manager = DummyTokenManager(
        st_to_at_result={
            "access_token": "new-at",
            "expires": expired,
            "user": {"email": "user@example.com"},
        }
    )
    client = build_client(db, token_manager)

    response = client.post(
        "/api/plugin/update-token",
        json={"session_token": "fresh-st"},
        headers={"Authorization": "Bearer conn-token"},
    )

    assert response.status_code == 400
    assert "expired access token" in response.json()["detail"]
    assert token_manager.update_calls == []


def test_plugin_update_token_fails_when_persistence_does_not_match_returned_values():
    existing = SimpleNamespace(id=8, is_active=True)
    returned_expires = datetime.now(timezone.utc) + timedelta(hours=2)
    stored = SimpleNamespace(
        id=8,
        st="fresh-st",
        at="stale-at",
        at_expires=returned_expires - timedelta(hours=1),
    )
    db = DummyDB(existing_token=existing, stored_token=stored)
    token_manager = DummyTokenManager(
        st_to_at_result={
            "access_token": "new-at",
            "expires": returned_expires.isoformat().replace("+00:00", "Z"),
            "user": {"email": "user@example.com"},
        }
    )
    client = build_client(db, token_manager)

    response = client.post(
        "/api/plugin/update-token",
        json={"session_token": "fresh-st"},
        headers={"Authorization": "Bearer conn-token"},
    )

    assert response.status_code == 500
    assert "AT persistence verification failed" in response.json()["detail"]
    assert len(token_manager.update_calls) == 1
