from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
import random

CardKind = Literal["number", "modifier", "action"]


@dataclass(frozen=True, slots=True)
class Card:
    kind: CardKind
    value: int | None = None
    name: str = ""
    effect: str | None = None

    @property
    def label(self) -> str:
        if self.kind == "number":
            return str(self.value)
        return self.name


def build_deck(decks: int = 1) -> list[Card]:
    """Build a standard Flip 7 deck, repeated `decks` times."""
    if decks < 1:
        raise ValueError("decks must be >= 1")

    cards: list[Card] = []

    # Number cards: 1x0, 1x1, 2x2, ... 12x12 = 79 cards.
    cards.append(Card("number", 0, "0"))
    for value in range(1, 13):
        cards.extend(Card("number", value, str(value)) for _ in range(value))

    # Score modifiers: one each.
    for modifier in (2, 4, 6, 8, 10):
        cards.append(Card("modifier", modifier, f"+{modifier}", "flat"))
    cards.append(Card("modifier", None, "x2", "double_numbers"))

    # Action cards: three each.
    for _ in range(3):
        cards.append(Card("action", None, "Freeze", "freeze"))
        cards.append(Card("action", None, "Flip Three", "flip_three"))
        cards.append(Card("action", None, "Second Chance", "second_chance"))

    cards *= decks
    random.shuffle(cards)
    return cards


def standard_deck_size() -> int:
    return 94
