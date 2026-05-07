"""Sample package entry points.

The extractor should preserve this module-level docstring as structural text.
"""

import json
import os
import requests as http_client

from .worker import Worker
from sample.helpers import helper


class BaseService:
    pass


class Service(BaseService):
    def __init__(self, worker: Worker):
        self.worker = worker

    @router.get("/items")
    async def run(
        self,
        value: str,
        limit: int = 3,
    ) -> dict[str, int]:
        cleaned = helper(value)
        return {"length": len(cleaned), "limit": limit}


class Outer:
    class Inner(Model):
        pass

