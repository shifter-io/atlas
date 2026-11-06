"""Node agent tests' shared fake: a gateway that rotates exit addresses."""

from helpers import Gateway


class RotatingGateway(Gateway):
    """Answers every proxied request with the next exit address from `pool`."""

    def __init__(self, pool):
        super().__init__()
        self.pool = pool
        self.served = 0

    async def respond(self, writer):
        self.body = f"{self.pool(self.served)}\n".encode()
        self.served += 1
        await super().respond(writer)
