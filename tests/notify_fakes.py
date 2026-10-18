"""Fake notification providers: tests never touch the network."""
import asyncio

from app.notify.providers import NotifyError


class FakeProvider:
    """Records messages. `fail` is raised on send; `hang` never finishes."""

    def __init__(self, fail: Exception | None = None, hang: bool = False):
        self.fail = fail
        self.hang = hang
        self.sent = []  # (config, Message)

    async def send(self, config, message, http):
        if self.hang:
            await asyncio.sleep(3600)
        if self.fail is not None:
            raise self.fail
        self.sent.append((config, message))

    def validate(self, config):
        return {}


def providers_for(fake: FakeProvider) -> dict:
    return {"acs": fake, "smtp2go": fake, "smtp": fake}


def failing(reason: str = "The email service rejected the message") -> FakeProvider:
    return FakeProvider(fail=NotifyError(reason))
