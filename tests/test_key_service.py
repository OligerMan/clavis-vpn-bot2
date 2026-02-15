"""Tests for KeyService multi-server key creation."""

import pytest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch, MagicMock

from database import init_test_db, User, Subscription, Key, Server

# Import KeyService directly from file to avoid services.__init__.py
# which imports NotificationService (requires telebot not in test env)
import importlib.util
import sys
from pathlib import Path

key_service_path = Path(__file__).parent.parent / "services" / "key_service.py"
spec = importlib.util.spec_from_file_location("key_service", key_service_path)
key_service_module = importlib.util.module_from_spec(spec)
sys.modules["key_service"] = key_service_module
spec.loader.exec_module(key_service_module)
KeyService = key_service_module.KeyService


@pytest.fixture
def db_session():
    """Create a fresh in-memory database for each test."""
    engine, TestSession = init_test_db()
    session = TestSession()
    yield session
    session.close()


@pytest.fixture
def user(db_session):
    """Create a test user."""
    user = User(telegram_id=123456789, username="testuser")
    db_session.add(user)
    db_session.commit()
    return user


@pytest.fixture
def subscription(db_session, user):
    """Create a test subscription."""
    sub = Subscription(
        user_id=user.id,
        expires_at=datetime.utcnow() + timedelta(days=90),
    )
    db_session.add(sub)
    db_session.commit()
    return sub


@pytest.fixture
def servers(db_session):
    """Create test servers (2 active xui, 1 inactive xui, 1 outline)."""
    server1 = Server(
        name="Frankfurt",
        host="fra.vpn.example.com",
        protocol="xui",
        api_url="https://fra.vpn.example.com:2053",
        is_active=True,
    )
    server2 = Server(
        name="Amsterdam",
        host="ams.vpn.example.com",
        protocol="xui",
        api_url="https://ams.vpn.example.com:2053",
        is_active=True,
    )
    server3 = Server(
        name="London (Inactive)",
        host="lon.vpn.example.com",
        protocol="xui",
        api_url="https://lon.vpn.example.com:2053",
        is_active=False,
    )
    server4 = Server(
        name="Outline Server",
        host="outline.example.com",
        protocol="outline",
        api_url="https://outline.example.com",
        is_active=True,
    )
    db_session.add_all([server1, server2, server3, server4])
    db_session.commit()
    return [server1, server2, server3, server4]


class TestGetAllActiveServers:
    """Tests for get_all_active_servers method."""

    def test_returns_all_active_xui_servers(self, db_session, servers):
        """Should return only active XUI servers."""
        active_servers = KeyService.get_all_active_servers(db_session)

        assert len(active_servers) == 2
        assert all(s.protocol == "xui" for s in active_servers)
        assert all(s.is_active for s in active_servers)
        server_names = {s.name for s in active_servers}
        assert server_names == {"Frankfurt", "Amsterdam"}

    def test_filters_inactive_servers(self, db_session, servers):
        """Should not return inactive servers."""
        active_servers = KeyService.get_all_active_servers(db_session)

        assert not any(s.name == "London (Inactive)" for s in active_servers)

    def test_filters_non_xui_servers(self, db_session, servers):
        """Should not return outline/other protocol servers."""
        active_servers = KeyService.get_all_active_servers(db_session)

        assert not any(s.protocol == "outline" for s in active_servers)

    def test_raises_when_no_servers(self, db_session):
        """Should raise ValueError when no servers available."""
        with pytest.raises(ValueError, match="No available servers"):
            KeyService.get_all_active_servers(db_session)

    def test_raises_when_only_inactive_servers(self, db_session):
        """Should raise ValueError when all servers are inactive."""
        server = Server(
            name="Inactive",
            host="test.com",
            protocol="xui",
            is_active=False,
        )
        db_session.add(server)
        db_session.commit()

        with pytest.raises(ValueError, match="No available servers"):
            KeyService.get_all_active_servers(db_session)


class TestCreateSubscriptionKeys:
    """Tests for create_subscription_keys method."""

    def test_creates_keys_on_all_servers(self, db_session, subscription, servers):
        """Should create a key on each active server."""
        # Mock XUIClient to avoid real API calls
        with patch("key_service.XUIClient") as MockXUIClient:
            # Setup mock to return different keys for each server
            mock_clients = []
            for i, server in enumerate(servers[:2]):  # Only active xui servers
                mock_client = Mock()
                mock_key = Key(
                    subscription_id=subscription.id,
                    server_id=server.id,
                    protocol="xui",
                    remote_key_id=f"remote_key_{i}",
                    key_data=f"vless://uuid{i}@{server.host}:443",
                    remarks=f"Key for {server.name}",
                )
                mock_client.create_key.return_value = mock_key
                mock_clients.append(mock_client)

            MockXUIClient.side_effect = mock_clients

            # Create keys
            keys = KeyService.create_subscription_keys(
                db_session, subscription, 123456789
            )

            # Verify keys created on all active servers
            assert len(keys) == 2
            assert MockXUIClient.call_count == 2

            # Verify keys saved to database
            db_keys = db_session.query(Key).filter(
                Key.subscription_id == subscription.id
            ).all()
            assert len(db_keys) == 2

    def test_returns_created_keys(self, db_session, subscription, servers):
        """Should return list of created Key objects."""
        with patch("key_service.XUIClient") as MockXUIClient:
            mock_client = Mock()
            mock_key1 = Key(
                subscription_id=subscription.id,
                server_id=servers[0].id,
                protocol="xui",
                key_data="vless://key1",
            )
            mock_key2 = Key(
                subscription_id=subscription.id,
                server_id=servers[1].id,
                protocol="xui",
                key_data="vless://key2",
            )
            mock_client.create_key.side_effect = [mock_key1, mock_key2]
            MockXUIClient.return_value = mock_client

            keys = KeyService.create_subscription_keys(
                db_session, subscription, 123456789
            )

            assert isinstance(keys, list)
            assert len(keys) == 2
            assert all(isinstance(k, Key) for k in keys)

    def test_handles_partial_failure(self, db_session, subscription, servers):
        """Should continue if one server fails, create keys on others."""
        with patch("key_service.XUIClient") as MockXUIClient:
            # First server fails, second succeeds
            mock_client1 = Mock()
            mock_client1.create_key.side_effect = Exception("Server 1 unreachable")

            mock_client2 = Mock()
            mock_key2 = Key(
                subscription_id=subscription.id,
                server_id=servers[1].id,
                protocol="xui",
                key_data="vless://key2",
            )
            mock_client2.create_key.return_value = mock_key2

            MockXUIClient.side_effect = [mock_client1, mock_client2]

            # Should succeed with 1 key
            keys = KeyService.create_subscription_keys(
                db_session, subscription, 123456789
            )

            assert len(keys) == 1
            assert keys[0].server_id == servers[1].id

    def test_raises_when_all_servers_fail(self, db_session, subscription, servers):
        """Should raise ValueError when all servers fail."""
        with patch("key_service.XUIClient") as MockXUIClient:
            mock_client = Mock()
            mock_client.create_key.side_effect = Exception("Server unreachable")
            MockXUIClient.return_value = mock_client

            with pytest.raises(ValueError, match="Failed to create keys on all servers"):
                KeyService.create_subscription_keys(
                    db_session, subscription, 123456789
                )

    def test_commits_to_database(self, db_session, subscription, servers):
        """Should commit keys to database."""
        with patch("key_service.XUIClient") as MockXUIClient:
            mock_client = Mock()
            mock_key = Key(
                subscription_id=subscription.id,
                server_id=servers[0].id,
                protocol="xui",
                key_data="vless://test",
            )
            mock_client.create_key.return_value = mock_key
            MockXUIClient.return_value = mock_client

            keys = KeyService.create_subscription_keys(
                db_session, subscription, 123456789
            )

            # Verify commit happened by querying fresh from DB
            db_keys = db_session.query(Key).filter(
                Key.subscription_id == subscription.id
            ).all()
            assert len(db_keys) >= 1

    def test_refreshes_keys_after_commit(self, db_session, subscription, servers):
        """Should refresh keys to get database-generated IDs."""
        with patch("key_service.XUIClient") as MockXUIClient:
            mock_client = Mock()
            mock_key = Key(
                subscription_id=subscription.id,
                server_id=servers[0].id,
                protocol="xui",
                key_data="vless://test",
            )
            mock_client.create_key.return_value = mock_key
            MockXUIClient.return_value = mock_client

            keys = KeyService.create_subscription_keys(
                db_session, subscription, 123456789
            )

            # After refresh, key should have database ID
            assert all(k.id is not None for k in keys)

    def test_calls_xui_client_with_correct_params(self, db_session, subscription, servers):
        """Should pass subscription and telegram_id to XUIClient.create_key."""
        with patch("key_service.XUIClient") as MockXUIClient:
            mock_client = Mock()
            mock_key = Key(
                subscription_id=subscription.id,
                server_id=servers[0].id,
                protocol="xui",
                key_data="vless://test",
            )
            mock_client.create_key.return_value = mock_key
            MockXUIClient.return_value = mock_client

            user_telegram_id = 987654321
            KeyService.create_subscription_keys(
                db_session, subscription, user_telegram_id
            )

            # Verify create_key called with correct params
            assert mock_client.create_key.call_count >= 1
            call_args = mock_client.create_key.call_args
            assert call_args[0][0] == subscription
            assert call_args[0][1] == user_telegram_id

    def test_logs_success_for_each_server(self, db_session, subscription, servers):
        """Should log successful key creation for each server."""
        with patch("key_service.XUIClient") as MockXUIClient:
            with patch("key_service.logger") as mock_logger:
                mock_client = Mock()
                mock_key1 = Key(
                    subscription_id=subscription.id,
                    server_id=servers[0].id,
                    protocol="xui",
                    key_data="vless://key1",
                )
                mock_key2 = Key(
                    subscription_id=subscription.id,
                    server_id=servers[1].id,
                    protocol="xui",
                    key_data="vless://key2",
                )
                mock_client.create_key.side_effect = [mock_key1, mock_key2]
                MockXUIClient.return_value = mock_client

                KeyService.create_subscription_keys(
                    db_session, subscription, 123456789
                )

                # Should log success for each server
                assert mock_logger.info.call_count == 2
                log_messages = [call[0][0] for call in mock_logger.info.call_args_list]
                assert any("Frankfurt" in msg for msg in log_messages)
                assert any("Amsterdam" in msg for msg in log_messages)

    def test_logs_warning_on_failure(self, db_session, subscription, servers):
        """Should log warning when individual server fails."""
        with patch("key_service.XUIClient") as MockXUIClient:
            with patch("key_service.logger") as mock_logger:
                # First server fails
                mock_client1 = Mock()
                mock_client1.create_key.side_effect = Exception("Connection timeout")

                # Second server succeeds
                mock_client2 = Mock()
                mock_key = Key(
                    subscription_id=subscription.id,
                    server_id=servers[1].id,
                    protocol="xui",
                    key_data="vless://key2",
                )
                mock_client2.create_key.return_value = mock_key

                MockXUIClient.side_effect = [mock_client1, mock_client2]

                KeyService.create_subscription_keys(
                    db_session, subscription, 123456789
                )

                # Should log warning for failed server
                assert mock_logger.warning.call_count >= 1
                warning_msg = mock_logger.warning.call_args[0][0]
                assert "Frankfurt" in warning_msg or "Failed" in warning_msg


class TestGetSubscriptionTraffic:
    """Tests for get_subscription_traffic method."""

    def test_aggregates_traffic_from_all_keys(self, db_session, subscription, servers):
        """Should sum traffic from all keys across servers."""
        # Create keys
        key1 = Key(
            subscription_id=subscription.id,
            server_id=servers[0].id,
            protocol="xui",
            key_data="vless://key1",
        )
        key2 = Key(
            subscription_id=subscription.id,
            server_id=servers[1].id,
            protocol="xui",
            key_data="vless://key2",
        )
        db_session.add_all([key1, key2])
        db_session.commit()

        with patch("key_service.XUIClient") as MockXUIClient:
            # Mock traffic responses
            mock_client = Mock()
            traffic1 = Mock()
            traffic1.upload_bytes = 1024 ** 3  # 1 GB
            traffic1.download_bytes = 2 * 1024 ** 3  # 2 GB

            traffic2 = Mock()
            traffic2.upload_bytes = 500 * 1024 ** 2  # 0.5 GB
            traffic2.download_bytes = 1500 * 1024 ** 2  # 1.5 GB

            mock_client.get_traffic.side_effect = [traffic1, traffic2]
            MockXUIClient.return_value = mock_client

            result = KeyService.get_subscription_traffic(db_session, subscription)

            # Total: upload = 1.5 GB, download = 3.5 GB, total = 5 GB
            # Use approximate comparison for floating point
            assert result["upload_gb"] == pytest.approx(1.49, rel=0.01)  # ~1.5 GB
            assert result["download_gb"] == pytest.approx(3.47, rel=0.01)  # ~3.5 GB
            assert result["total_gb"] == pytest.approx(4.96, rel=0.01)  # ~5 GB


class TestDeleteSubscriptionKeys:
    """Tests for delete_subscription_keys method."""

    def test_deletes_keys_on_all_servers(self, db_session, subscription, servers):
        """Should delete keys from all servers."""
        # Create keys
        key1 = Key(
            subscription_id=subscription.id,
            server_id=servers[0].id,
            protocol="xui",
            remote_key_id="key1",
            key_data="vless://key1",
        )
        key2 = Key(
            subscription_id=subscription.id,
            server_id=servers[1].id,
            protocol="xui",
            remote_key_id="key2",
            key_data="vless://key2",
        )
        db_session.add_all([key1, key2])
        db_session.commit()

        with patch("key_service.XUIClient") as MockXUIClient:
            mock_client = Mock()
            MockXUIClient.return_value = mock_client

            KeyService.delete_subscription_keys(db_session, subscription)

            # Verify delete_key called for each key
            assert mock_client.delete_key.call_count == 2

            # Verify keys marked inactive
            db_keys = db_session.query(Key).filter(
                Key.subscription_id == subscription.id
            ).all()
            assert all(not k.is_active for k in db_keys)
