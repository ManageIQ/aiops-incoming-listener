from asyncio import Future

import pytest
from aiohttp import ClientResponseError, ClientConnectionError, ClientError

# R0201 = Method could be a function Used when a method doesn't use its bound
# instance, and so could be written as a function.
# pylint: disable=R0201


@pytest.mark.asyncio
class TestHitNext:
    """Test. `hit_next` function."""

    async def test_server_available(self, app, stub_server):
        """Server is available, any message should get accepted."""
        resp = await app.hit_next('STUB_ID', {'url': 'http://STUB.URL'})
        first_request = stub_server['requests'][0]

        assert len(stub_server['requests']) == 1
        assert first_request['raw'].method == 'POST'
        assert first_request['content']['url'] == 'http://STUB.URL'
        assert first_request['content']['origin'] == 'STUB_TOPIC'
        assert resp.status == 200

    async def test_forward_rh_account(self, app, stub_server):
        """Provide 'rh_account' in metadata."""
        resp = await app.hit_next(
            'STUB_ID',
            {'url': 'http://STUB.URL', 'rh_account': 'STUB_ACCOUNT'}
        )
        request = stub_server['requests'][0]

        assert request['content']['metadata']['rh_account'] == 'STUB_ACCOUNT'
        assert resp.status == 200

    async def test_forward_b64_identity(self, app, stub_server):
        """Provide 'b64_identity' in resp.request_info.headers."""
        resp = await app.hit_next(
            'STUB_ID',
            {
                'url': 'http://STUB.URL',
                'b64_identity': 'abcde'
            }
        )

        request = stub_server['requests'][0]

        assert request['raw'].headers['x-rh-identity'] == 'abcde'
        assert resp.request_info.headers['x-rh-identity'] == 'abcde'
        assert resp.status == 200

    @pytest.mark.parametrize('app,stub_server', [
        ('INVALID_ENDPOINT', {'status': 404})
    ], indirect=True)
    async def test_invalid_endpoint(self, app, stub_server):
        """Hit invalid endpoint."""
        with pytest.raises(ClientResponseError):
            await app.hit_next('STUB_ID', {'url': 'http://STUB.URL'})

        assert len(stub_server['requests']) == app.MAX_RETRIES
        for req in stub_server['requests']:
            assert req['raw'].method == 'POST'
            assert str(req['raw'].rel_url) == '/INVALID_ENDPOINT'

    async def test_invalid_server(self, app):
        """Server doesn't exist."""
        with pytest.raises(ClientConnectionError):
            await app.hit_next('STUB_ID', {'url': 'http://STUB.URL'})


@pytest.mark.asyncio
class TestProcessMessage:
    """Test `process_message` function."""

    @pytest.mark.parametrize('message', (
        b'{}',
        b'',
        b'{"not_an_url":"SOME_VALUE"}',
        b'ABCD',
        b'{"invalid:"json"}'
    ), indirect=True)
    async def test_invalid_message(self, app, message):
        """Check if an invalid message can be parsed.

        Invalid message is understood as:
        - invalid JSON
        - valid JSON without `url` key present
        """
        success = await app.process_message(message)
        assert not success

    async def test_valid_message(self, app, message, mocker):
        """Ensure that a valid message can be processed."""
        mock = mocker.patch('kafka_app.hit_next', return_value=Future())
        mock.return_value.set_result(True)
        success = await app.process_message(message)

        assert success
        mock.assert_called_once()

    @pytest.mark.parametrize('message', (
        b'{"a":"REQUIRED"}',
        b'{"b":"REQUIRED"}',
        b'{"a":"REQUIRED","B":"REQUIRED"}',
        b'{"a":"REQUIRED","b":""}',
    ), indirect=True)
    async def test_custom_key_invalid(self, app, message, monkeypatch, mocker):
        """Custom `VALIDATE_PRESENCE` settings catches invalid message."""
        monkeypatch.setattr('kafka_app.VALIDATE_PRESENCE', {'a', 'b'})
        mock = mocker.patch('kafka_app.hit_next', return_value=Future())
        mock.return_value.set_result(True)
        success = await app.process_message(message)

        assert not success

    @pytest.mark.parametrize('message', (
        b'{"a":"REQUIRED","b":"REQUIRED"}',
    ), indirect=True)
    async def test_custom_key_valid(self, app, message, monkeypatch, mocker):
        """Custom `VALIDATE_PRESENCE` settings works for a valid message."""
        monkeypatch.setattr('kafka_app.VALIDATE_PRESENCE', {'a', 'b'})
        mock = mocker.patch('kafka_app.hit_next', return_value=Future())
        mock.return_value.set_result(True)
        success = await app.process_message(message)

        assert success

    async def test_unable_to_hit_next(self, app, message, mocker):
        """Test when unable to pass message to next service."""
        mocker.patch('kafka_app.hit_next', side_effect=ClientError())
        success = await app.process_message(message)

        assert not success


@pytest.mark.asyncio
class TestConsumeMessages:
    """Test `consume_messages` function."""

    async def test_consumer_start_stop(self, app, kafka_consumer):
        """Test AIOKafkaConsumer start/stop lifecycle."""
        await app.consume_messages()

        kafka_consumer.assert_called_once()
        kafka_consumer.return_value.start.assert_called_once()
        kafka_consumer.return_value.stop.assert_called_once()

    @pytest.mark.parametrize('kafka_consumer,count', (
        (list(), 0),
        ((1, 2, 3), 3),
    ), indirect=('kafka_consumer',))
    async def test_messages_consumed(self, mocker, app, kafka_consumer, count):
        """Check if given amount of messages is really consumed."""
        # pylama:ignore=W0613

        mock = mocker.patch('kafka_app.process_message', return_value=Future())
        mock.return_value.set_result(True)

        await app.consume_messages()
        assert mock.call_count == count

    async def test_multiple_topics(self, app, kafka_consumer, monkeypatch):
        """Test that multiple topic are propagated to Kafka."""
        monkeypatch.setattr('kafka_app.TOPIC', ['A_TOPIC', 'B_TOPIC'])
        await app.consume_messages()

        assert kafka_consumer.call_args[0][0] == ['A_TOPIC', 'B_TOPIC']


class TestMain:
    """Test init setup of the service."""

    @pytest.mark.parametrize('variable', (
        'KAFKA_SERVER', 'KAFKA_TOPIC', 'NEXT_SERVICE_URL'
    ))
    def test_missing_env_variables(self, app, mocker, monkeypatch, variable):
        """Should exit when required env variable is missing."""
        mocker.patch.object(app, '__name__', 'kafka_app')
        monkeypatch.delenv(variable)

        with pytest.raises(SystemExit) as err:
            app.main()

        assert err.value.code == 1

    def test_consumer_started(self, app, mocker):
        """Check that event loop was started when env is OK."""
        mocker.patch.object(app, '__name__', 'kafka_app')
        mock_consumer = mocker.patch('kafka_app.consume_messages')
        mock_consumer.return_value = Future()
        mock_loop = mocker.patch('kafka_app.MAIN_LOOP')

        app.main()

        mock_consumer.assert_called_once()
        mock_loop.run_until_complete.assert_called_once()
