from .helpers import helper


class Worker:
    def process(self, value: str) -> str:
        return helper(value)

