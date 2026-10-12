"""A class whose name ends in ``Engine``, and its ordinary ``run`` method.

``game = GameEngine(config)`` used to bind ``game`` for the whole file, because the
binding pattern accepted any name containing ``Engine(``.  Every later ``game.run()``
is an ordinary method call and must not be reported.
"""

from __future__ import annotations


class GameEngine:
    def __init__(self, config) -> None:
        self.config = config

    def run(self, turns: int = 1) -> dict:
        return {"turns": turns, "config": self.config}


def play(config) -> dict:
    game = GameEngine(config)
    return game.run(turns=3)
