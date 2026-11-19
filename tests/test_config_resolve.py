"""RabbitMQ URL precedence for the CLI and Client.from_config."""
from libby.config_resolve import DEFAULT_RABBITMQ_URL, RABBITMQ_URL_ENV, resolve_rabbitmq_url


def test_env_sits_between_the_flag_and_the_config(monkeypatch):
    monkeypatch.setenv(RABBITMQ_URL_ENV, "amqp://env")
    config = {"rabbitmq_url": "amqp://file"}
    assert resolve_rabbitmq_url("amqp://flag", config) == "amqp://flag"
    assert resolve_rabbitmq_url(None, config) == "amqp://env"


def test_config_then_default_without_the_env(monkeypatch):
    monkeypatch.delenv(RABBITMQ_URL_ENV, raising=False)
    assert resolve_rabbitmq_url(None, {"rabbitmq_url": "amqp://file"}) == "amqp://file"
    assert resolve_rabbitmq_url(None, {}) == DEFAULT_RABBITMQ_URL
