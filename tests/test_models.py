"""Tests for database models."""

import pytest
from datetime import datetime, timedelta

from database import (
    init_test_db,
    User,
    Subscription,
    Key,
    Server,
    UserConfig,
    RoutingList,
    TrafficLog,
    Transaction,
)


@pytest.fixture
def db_session():
    """Create a fresh in-memory database for each test."""
    engine, TestSession = init_test_db()
    session = TestSession()
    yield session
    session.close()


class TestUser:
    def test_create_user(self, db_session):
        user = User(telegram_id=123456789, username="testuser")
        db_session.add(user)
        db_session.commit()

        assert user.id is not None
        assert user.telegram_id == 123456789
        assert user.username == "testuser"
        assert user.created_at is not None

    def test_user_unique_telegram_id(self, db_session):
        user1 = User(telegram_id=123456789)
        db_session.add(user1)
        db_session.commit()

        user2 = User(telegram_id=123456789)
        db_session.add(user2)
        with pytest.raises(Exception):  # IntegrityError
            db_session.commit()


class TestSubscription:
    def test_create_subscription(self, db_session):
        user = User(telegram_id=123456789)
        db_session.add(user)
        db_session.commit()

        sub = Subscription(
            user_id=user.id,
            name="Main",
            expires_at=datetime.utcnow() + timedelta(days=90),
        )
        db_session.add(sub)
        db_session.commit()

        assert sub.id is not None
        assert sub.token is not None  # Auto-generated
        assert len(sub.token) == 36  # UUID format
        assert sub.device_limit == 5
        assert sub.is_test is False
        assert sub.is_active is True

    def test_subscription_expiry(self, db_session):
        user = User(telegram_id=123456789)
        db_session.add(user)
        db_session.commit()

        # Expired subscription
        expired_sub = Subscription(
            user_id=user.id,
            expires_at=datetime.utcnow() - timedelta(days=1),
        )
        db_session.add(expired_sub)
        db_session.commit()

        assert expired_sub.is_expired is True
        assert expired_sub.days_until_expiry < 0

        # Active subscription
        active_sub = Subscription(
            user_id=user.id,
            expires_at=datetime.utcnow() + timedelta(days=30),
        )
        db_session.add(active_sub)
        db_session.commit()

        assert active_sub.is_expired is False
        assert active_sub.days_until_expiry >= 29

    def test_subscription_url(self, db_session):
        user = User(telegram_id=123456789)
        db_session.add(user)
        db_session.commit()

        sub = Subscription(
            user_id=user.id,
            expires_at=datetime.utcnow() + timedelta(days=90),
        )
        db_session.add(sub)
        db_session.commit()

        url = sub.get_subscription_url("https://vpn.example.com")
        assert url == f"https://vpn.example.com/sub/{sub.token}"


class TestKey:
    def test_create_key(self, db_session):
        user = User(telegram_id=123456789)
        db_session.add(user)
        db_session.commit()

        sub = Subscription(
            user_id=user.id,
            expires_at=datetime.utcnow() + timedelta(days=90),
        )
        db_session.add(sub)
        db_session.commit()

        key = Key(
            subscription_id=sub.id,
            protocol="xui",
            remote_key_id="abc123",
            key_data="vless://uuid@server.com:443?type=tcp#Frankfurt",
            remarks="Frankfurt Server",
        )
        db_session.add(key)
        db_session.commit()

        assert key.id is not None
        assert key.protocol == "xui"
        assert key.is_active is True

    def test_multiple_keys_per_subscription(self, db_session):
        user = User(telegram_id=123456789)
        db_session.add(user)
        db_session.commit()

        sub = Subscription(
            user_id=user.id,
            expires_at=datetime.utcnow() + timedelta(days=90),
        )
        db_session.add(sub)
        db_session.commit()

        key1 = Key(
            subscription_id=sub.id,
            protocol="xui",
            key_data="vless://key1@server1.com:443",
            remarks="Server 1",
        )
        key2 = Key(
            subscription_id=sub.id,
            protocol="outline",
            key_data="ss://key2@server2.com:443",
            remarks="Server 2",
        )
        db_session.add_all([key1, key2])
        db_session.commit()

        assert len(sub.keys) == 2


class TestServer:
    def test_create_server(self, db_session):
        server = Server(
            name="Frankfurt",
            host="fra.vpn.example.com",
            protocol="xui",
            api_url="https://fra.vpn.example.com:2053",
            capacity=100,
        )
        db_session.add(server)
        db_session.commit()

        assert server.id is not None
        assert server.is_active is True
        assert server.current_load == 0
        assert server.has_capacity is True


class TestUserConfig:
    def test_create_user_config(self, db_session):
        user = User(telegram_id=123456789)
        db_session.add(user)
        db_session.commit()

        config = UserConfig(user_id=user.id)
        db_session.add(config)
        db_session.commit()

        assert config.get_bypass_domains() == []
        assert config.get_blocked_domains() == []
        assert config.get_proxied_domains() == []

    def test_user_config_domains(self, db_session):
        user = User(telegram_id=123456789)
        db_session.add(user)
        db_session.commit()

        config = UserConfig(user_id=user.id)
        config.set_bypass_domains(["google.ru", "yandex.ru"])
        config.set_blocked_domains(["ads.com"])
        config.set_enabled_lists([1, 2, 3])
        db_session.add(config)
        db_session.commit()

        # Refresh from DB
        db_session.refresh(config)

        assert config.get_bypass_domains() == ["google.ru", "yandex.ru"]
        assert config.get_blocked_domains() == ["ads.com"]
        assert config.get_enabled_lists() == [1, 2, 3]


class TestRoutingList:
    def test_create_routing_list(self, db_session):
        ru_bypass = RoutingList(
            name="ru_bypass",
            display_name="Russian sites",
            type="bypass",
            is_default=True,
        )
        ru_bypass.set_domains(["yandex.ru", "sberbank.ru", "gosuslugi.ru"])
        db_session.add(ru_bypass)
        db_session.commit()

        assert ru_bypass.id is not None
        assert ru_bypass.get_domains() == ["yandex.ru", "sberbank.ru", "gosuslugi.ru"]

    def test_routing_list_add_remove_domain(self, db_session):
        ads = RoutingList(
            name="ads_block",
            display_name="Ad blocker",
            type="block",
        )
        db_session.add(ads)
        db_session.commit()

        ads.add_domain("ads.google.com")
        ads.add_domain("tracker.example.com")
        assert len(ads.get_domains()) == 2

        ads.remove_domain("ads.google.com")
        assert ads.get_domains() == ["tracker.example.com"]


class TestTrafficLog:
    def test_create_traffic_log(self, db_session):
        user = User(telegram_id=123456789)
        db_session.add(user)
        db_session.commit()

        sub = Subscription(
            user_id=user.id,
            expires_at=datetime.utcnow() + timedelta(days=90),
        )
        db_session.add(sub)
        db_session.commit()

        key = Key(
            subscription_id=sub.id,
            protocol="xui",
            key_data="vless://test",
        )
        db_session.add(key)
        db_session.commit()

        log = TrafficLog(
            key_id=key.id,
            date=datetime.utcnow(),
            upload_diff=1024 * 1024,  # 1 MB
            download_diff=100 * 1024 * 1024,  # 100 MB
        )
        db_session.add(log)
        db_session.commit()

        assert log.total_diff == 101 * 1024 * 1024

    def test_key_update_traffic(self, db_session):
        """Test daily diff calculation via Key.update_traffic()"""
        user = User(telegram_id=123456789)
        db_session.add(user)
        db_session.commit()

        sub = Subscription(
            user_id=user.id,
            expires_at=datetime.utcnow() + timedelta(days=90),
        )
        db_session.add(sub)
        db_session.commit()

        key = Key(
            subscription_id=sub.id,
            protocol="xui",
            key_data="vless://test",
        )
        db_session.add(key)
        db_session.commit()

        # Day 1: 3x-ui reports 100MB total
        diff1 = key.update_traffic(100 * 1024 * 1024)
        assert diff1 == 100 * 1024 * 1024
        assert key.last_traffic_total == 100 * 1024 * 1024

        # Day 2: 3x-ui reports 250MB total
        diff2 = key.update_traffic(250 * 1024 * 1024)
        assert diff2 == 150 * 1024 * 1024
        assert key.last_traffic_total == 250 * 1024 * 1024

        # Day 3: 3x-ui reports 400MB total
        diff3 = key.update_traffic(400 * 1024 * 1024)
        assert diff3 == 150 * 1024 * 1024

    def test_cleanup_old_records(self, db_session):
        """Test deletion of records older than 30 days."""
        user = User(telegram_id=123456789)
        db_session.add(user)
        db_session.commit()

        sub = Subscription(
            user_id=user.id,
            expires_at=datetime.utcnow() + timedelta(days=90),
        )
        db_session.add(sub)
        db_session.commit()

        key = Key(
            subscription_id=sub.id,
            protocol="xui",
            key_data="vless://test",
        )
        db_session.add(key)
        db_session.commit()

        # Create old record (35 days ago)
        old_log = TrafficLog(
            key_id=key.id,
            date=datetime.utcnow() - timedelta(days=35),
            upload_diff=1000,
            download_diff=1000,
        )
        # Create recent record (5 days ago)
        recent_log = TrafficLog(
            key_id=key.id,
            date=datetime.utcnow() - timedelta(days=5),
            upload_diff=2000,
            download_diff=2000,
        )
        db_session.add_all([old_log, recent_log])
        db_session.commit()

        assert db_session.query(TrafficLog).count() == 2

        # Cleanup
        deleted = TrafficLog.cleanup_old_records(db_session, days=30)
        db_session.commit()

        assert deleted == 1
        assert db_session.query(TrafficLog).count() == 1
        remaining = db_session.query(TrafficLog).first()
        assert remaining.download_diff == 2000  # Recent one remains


class TestTransaction:
    def test_create_transaction(self, db_session):
        user = User(telegram_id=123456789)
        db_session.add(user)
        db_session.commit()

        tx = Transaction(
            user_id=user.id,
            amount=17500,  # 175 RUB in kopeks
            plan="90_days",
        )
        db_session.add(tx)
        db_session.commit()

        assert tx.id is not None
        assert tx.status == "pending"
        assert tx.amount_rub == 175.0
        assert tx.completed_at is None

    def test_transaction_complete(self, db_session):
        user = User(telegram_id=123456789)
        db_session.add(user)
        db_session.commit()

        tx = Transaction(
            user_id=user.id,
            amount=60000,  # 600 RUB
            plan="365_days",
        )
        db_session.add(tx)
        db_session.commit()

        tx.complete()
        db_session.commit()

        assert tx.status == "completed"
        assert tx.completed_at is not None


class TestRelationships:
    def test_user_cascade_delete(self, db_session):
        """Deleting user should cascade to subscriptions, keys, etc."""
        user = User(telegram_id=123456789)
        db_session.add(user)
        db_session.commit()

        sub = Subscription(
            user_id=user.id,
            expires_at=datetime.utcnow() + timedelta(days=90),
        )
        db_session.add(sub)
        db_session.commit()

        key = Key(
            subscription_id=sub.id,
            protocol="xui",
            key_data="vless://test",
        )
        db_session.add(key)
        db_session.commit()

        config = UserConfig(user_id=user.id)
        db_session.add(config)
        db_session.commit()

        # Delete user
        db_session.delete(user)
        db_session.commit()

        # All related objects should be deleted
        assert db_session.query(Subscription).count() == 0
        assert db_session.query(Key).count() == 0
        assert db_session.query(UserConfig).count() == 0
