from concurrent.futures import ThreadPoolExecutor
import sqlite3

from argon2 import PasswordHasher
import pytest

from app.dashboard_auth import DashboardAuth, AuthFailure, SESSION_SECONDS, RENEW_SECONDS


def create(root, clock):
    return DashboardAuth(root, clock=lambda: clock[0],
                         hasher=PasswordHasher(time_cost=1, memory_cost=1024, parallelism=1))


def test_private_setup_is_single_use_and_password_and_session_are_hashed(tmp_path):
    clock = [1000]
    auth = create(tmp_path, clock)
    token = auth.prepare_setup()
    password = 'synthetic setup passphrase'
    with pytest.raises(AuthFailure):
        auth.setup('invalid', 'owner', password, 'fixture')
    assert not auth.configured()
    session = auth.setup(token, 'Owner', password, 'fixture')
    assert auth.configured() and auth.authenticate(session['token'])['username'] == 'owner'
    with pytest.raises(AuthFailure):
        auth.setup(token, 'second-user', password, 'fixture')
    with sqlite3.connect(auth.path) as db:
        owner = db.execute('SELECT username,password_hash FROM owner').fetchall()
        assert len(owner) == 1 and owner[0][1].startswith('$argon2id$')
        assert db.execute('SELECT token_hash FROM sessions').fetchone()[0] != session['token']
        assert db.execute('SELECT COUNT(*) FROM bootstrap').fetchone()[0] == 0
    raw = auth.path.read_bytes()
    assert password.encode() not in raw and session['token'].encode() not in raw and token.encode() not in raw


def test_login_survives_recreation_and_renews_after_a_day(tmp_path):
    clock = [1000]
    auth = create(tmp_path, clock)
    auth.configure('owner', 'synthetic remembered password')
    session = auth.login('OWNER', 'synthetic remembered password', 'fixture')
    restarted = create(tmp_path, clock)
    assert restarted.authenticate(session['token'])['renew'] is False
    clock[0] += RENEW_SECONDS + 1
    renewed = restarted.authenticate(session['token'])
    assert renewed['renew'] is True and renewed['expires_at'] == clock[0] + SESSION_SECONDS
    clock[0] += SESSION_SECONDS + 1
    assert restarted.authenticate(session['token']) is None


def test_local_password_change_and_logout_revoke_sessions(tmp_path):
    clock = [1000]
    auth = create(tmp_path, clock)
    auth.configure('owner', 'synthetic first password')
    session = auth.login('owner', 'synthetic first password', 'fixture')
    auth.configure('owner', 'synthetic replacement password')
    assert auth.authenticate(session['token']) is None
    with pytest.raises(AuthFailure):
        auth.login('owner', 'synthetic first password', 'fixture')
    fresh = auth.login('owner', 'synthetic replacement password', 'fixture')
    auth.logout(fresh['token'])
    assert auth.authenticate(fresh['token']) is None


def test_setup_expiration_and_rate_limit_survive_restarts(tmp_path):
    clock = [1000]
    auth = create(tmp_path, clock)
    token = auth.prepare_setup()
    clock[0] += 24 * 3600 + 1
    with pytest.raises(AuthFailure):
        auth.setup(token, 'owner', 'synthetic long password', 'fixture')
    for _ in range(7):
        with pytest.raises(AuthFailure):
            auth.login('owner', 'wrong', 'fixture')
    restarted = create(tmp_path, clock)
    with pytest.raises(AuthFailure) as failure:
        restarted.login('owner', 'wrong', 'fixture')
    assert failure.value.status == 429
    clock[0] += 61
    with pytest.raises(AuthFailure) as failure:
        restarted.login('owner', 'wrong', 'fixture')
    assert failure.value.status == 401


def test_parallel_setup_cannot_create_two_owners(tmp_path):
    clock = [1000]
    auth = create(tmp_path, clock)
    token = auth.prepare_setup()
    def setup(i):
        try:
            return auth.setup(token, 'owner' + str(i), 'synthetic long setup password', str(i))
        except AuthFailure:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(setup, [1, 2]))
    assert sum(value is not None for value in results) == 1
    with auth.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM owner').fetchone()[0] == 1
