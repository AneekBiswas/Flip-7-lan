from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
import random

from .cards import Card, build_deck


@dataclass
class Player:
    player_id: str
    name: str
    is_host: bool = False
    total_score: int = 0
    color: str = "white"

    # Round-only state.
    status: str = "waiting"  # waiting | active | stayed | busted | flip7
    numbers: list[int] = field(default_factory=list)
    modifiers: list[str] = field(default_factory=list)
    second_chance: bool = False
    last_round_score: int = 0

    def reset_round(self) -> None:
        self.status = "active"
        self.numbers.clear()
        self.modifiers.clear()
        self.second_chance = False
        self.last_round_score = 0


class Game:
    """
    Server-authoritative Flip 7 game state.

    The initial draw pile is 94 cards for 3-8 players and 188 cards for
    9-16 players. Per the requested house rule, any refill is exactly one
    freshly shuffled 94-card deck, regardless of the number of players.
    """

    MIN_PLAYERS = 3
    MAX_PLAYERS = 16
    WIN_SCORE = 200

    def __init__(self, players: list[Player], log_fn, deck_notice_fn, target_score: int = WIN_SCORE):
        self.players = players
        self.log_fn = log_fn
        self.deck_notice_fn = deck_notice_fn

        self.round_number = 0
        self.dealer_index = 0
        self.current_index: int | None = None

        self.draw_pile: list[Card] = []
        if target_score < 1:
            raise ValueError("Target score must be at least 1.")

        self.target_score = target_score
        self.phase = "lobby"

        self.initial_deal_index = 0
        self.pending_action: dict[str, Any] | None = None
        self.pending_forced: dict[str, Any] | None = None
        self.forced_stack: list[dict[str, Any]] = []

        self._next_card_id = 0

    # ---------- public helpers ----------

    def player_by_id(self, player_id: str) -> Player:
        for p in self.players:
            if p.player_id == player_id:
                return p
        raise KeyError(player_id)

    def remove_player(self, player_id: str) -> tuple[Player, bool]:
        """Remove a player permanently from the table.

        Returns:
            (removed_player, was_current_player)
        """
        index = next(
            (
                i
                for i, player in enumerate(self.players)
                if player.player_id == player_id
            ),
            None,
        )

        if index is None:
            raise KeyError(player_id)

        removed = self.players.pop(index)
        was_current = self.current_index == index

        if not self.players:
            self.dealer_index = 0
            self.current_index = None
            return removed, was_current

        # Keep dealer_index pointing to the same logical player.
        if index < self.dealer_index:
            self.dealer_index -= 1
        elif index == self.dealer_index:
            self.dealer_index %= len(self.players)

        # Keep current_index consistent after the list shrinks.
        if self.current_index is not None:
            if self.current_index > index:
                self.current_index -= 1
            elif self.current_index == index:
                # Put the cursor immediately before the removed player
                # so _advance_turn() selects the next player.
                self.current_index = (index - 1) % len(self.players)

        return removed, was_current

    def active_players(self) -> list[Player]:
        return [p for p in self.players if p.status == "active"]

    def standings(self) -> list[dict[str, Any]]:
        ordered = sorted(
            self.players,
            key=lambda p: (-p.total_score, p.name.lower())
        )
        return [
            {
                "rank": i,
                "player_id": p.player_id,
                "name": p.name,
                "color": p.color,
                "score": p.total_score,
            }
            for i, p in enumerate(ordered, 1)
        ]

    def public_players(self) -> list[dict[str, Any]]:
        return [
            {
                "player_id": p.player_id,
                "name": p.name,
                "color": p.color,
                "is_host": p.is_host,
                "status": p.status,
                "score": p.total_score,
                "last_round_score": p.last_round_score,
            }
            for p in self.players
        ]

    # ---------- deck ----------

    def _refill_one_deck(self) -> None:
        self.draw_pile = build_deck(1)
        self.deck_notice_fn(len(self.draw_pile))

    def _pop_card(self) -> Card:
        if not self.draw_pile:
            self._refill_one_deck()

        card = self.draw_pile.pop()

        # House rule requested by the user: refill immediately when the
        # stack becomes empty, using exactly one 94-card deck.
        if not self.draw_pile:
            self._refill_one_deck()

        return card

    # ---------- round lifecycle ----------

    def start_game(self) -> None:
        if self.phase != "lobby":
            raise ValueError("The game has already started.")

        if not self.MIN_PLAYERS <= len(self.players) <= self.MAX_PLAYERS:
            raise ValueError(
                f"Need {self.MIN_PLAYERS}-{self.MAX_PLAYERS} players to start."
            )

        self.dealer_index = 0
        initial_decks = 1 if len(self.players) <= 8 else 2
        self.draw_pile = build_deck(initial_decks)
        self.round_number = 0
        self.phase = "dealing"
        self._start_round()

    def _start_round(self) -> None:
        self.round_number += 1

        for p in self.players:
            p.reset_round()

        self.current_index = None
        self.pending_action = None
        self.pending_forced = None
        self.forced_stack.clear()
        self.initial_deal_index = 0
        self.phase = "dealing"

        dealer = self.players[self.dealer_index]
        self.log_fn(
            f"Round {self.round_number} begins. Dealer: {dealer.name}."
        )
        self._deal_initial_next()

    def _deal_initial_next(self) -> None:
        if self._round_over():
            self.end_round()
            return

        if self.initial_deal_index >= len(self.players):
            self.phase = "turns"
            self._set_first_turn()
            return

        player = self.players[
            (self.dealer_index + self.initial_deal_index) % len(self.players)
        ]
        self.initial_deal_index += 1

        if player.status != "active":
            self._deal_initial_next()
            return
        
        self._deal_to_player(
            player,
            source_player=player,
            resume={"kind": "initial_deal"},
            forced=False,
        )

    def _set_first_turn(self) -> None:
        start = (self.dealer_index + 1) % len(self.players)
        for offset in range(len(self.players)):
            idx = (start + offset) % len(self.players)
            if self.players[idx].status == "active":
                self.current_index = idx
                self._announce_turn()
                return
        self.end_round()

    def _announce_turn(self) -> None:
        if self.current_index is None:
            return
        player = self.players[self.current_index]
        self.log_fn(f"It is {player.name}'s turn.")
        if player.status != "active":
            self._advance_turn()
            return

    def _advance_turn(self) -> None:
        if self._round_over():
            self.end_round()
            return

        if self.current_index is None:
            self._set_first_turn()
            return

        n = len(self.players)
        for step in range(1, n + 1):
            idx = (self.current_index + step) % n
            if self.players[idx].status == "active":
                self.current_index = idx
                self._announce_turn()
                return

        self.end_round()

    def handle_choice(self, player_id: str, choice: str) -> None:
        if self.phase != "turns" or self.current_index is None:
            raise ValueError("It is not time to choose hit or stand.")

        current = self.players[self.current_index]
        if current.player_id != player_id:
            raise ValueError("It is not your turn.")
        if current.status != "active":
            raise ValueError("You are no longer active in this round.")

        choice = choice.strip().lower()
        if choice == "hit":
            self.log_fn(f"{current.name} chose HIT.")
            self._deal_to_player(
                current,
                source_player=current,
                resume={"kind": "advance_turn"},
                forced=False,
            )
        elif choice == "stand":
            # A Second Chance is a real card in front of the player, so it
            # is legal to Stay even if it is their only card.
            if (
                not current.numbers
                and not current.modifiers
                and not current.second_chance
            ):
                raise ValueError("You cannot stand before receiving a card.")
            self.log_fn(f"{current.name} chose STAND.")
            self._bank_player(current, reason="stood")
            self._advance_turn()
        else:
            raise ValueError("Enter 'hit' or 'stand'.")

    # ---------- card resolution ----------

    def _deal_to_player(
        self,
        target: Player,
        source_player: Player,
        resume: dict[str, Any],
        forced: bool,
    ) -> None:
        card = self._pop_card()
        self.log_fn(f"Player {target.name} has been dealt a {card.label}.")

        if forced:
            self._resolve_forced_card(target, source_player, card)
        else:
            self._resolve_card(target, source_player, card, resume)

    def _resolve_card(
        self,
        target: Player,
        source_player: Player,
        card: Card,
        resume: dict[str, Any],
    ) -> None:
        if card.kind == "number":
            if card.value in target.numbers:
                if target.second_chance:
                    target.second_chance = False
                    self.log_fn(
                        f"{target.name} used Second Chance to avoid a duplicate "
                        f"{card.label}; the duplicate and Second Chance are discarded."
                    )
                    self._discard_cards(card)
                    self._resume(resume)
                    return

                target.status = "busted"
                self.log_fn(f"Player {target.name} BUSTS!")
                target.last_round_score = 0
                self._resume(resume)
                return

            target.numbers.append(card.value)  # type: ignore[arg-type]
            if len(set(target.numbers)) == 7:
                target.status = "flip7"
                self.log_fn(
                    f"🎉 {target.name} FLIP 7! Seven unique numbers (+15 bonus)."
                )
                self.end_round(flip7_player=target)
                return

            self._resume(resume)
            return

        if card.kind == "modifier":
            target.modifiers.append(card.name)
            self._resume(resume)
            return

        # Action card.
        self._queue_or_prompt_action(
            card=card,
            owner=source_player,
            resume=resume,
        )

    def _resolve_forced_card(
        self,
        target: Player,
        source_player: Player,
        card: Card,
    ) -> None:
        ctx = self.pending_forced
        if ctx is None:
            raise RuntimeError("Forced-draw state missing.")

        # Any card counts toward the three forced flips.
        ctx["remaining"] -= 1

        if card.kind == "number":
            if card.value in target.numbers:
                if target.second_chance:
                    target.second_chance = False
                    self.log_fn(
                        f"{target.name} used Second Chance during Flip Three; "
                        f"the duplicate is discarded and the forced draw continues."
                    )
                    self._discard_cards(card)
                    if ctx["remaining"] > 0:
                        self._forced_draw_next()
                    else:
                        self._process_forced_queue()
                    return
                target.status = "busted"
                target.last_round_score = 0
                self.log_fn(
                    f"Player {target.name} BUSTS during Flip Three!"
                )
                self._finish_forced_draw()
                return

            target.numbers.append(card.value)  # type: ignore[arg-type]
            if len(set(target.numbers)) == 7:
                target.status = "flip7"
                self.log_fn(
                    f"🎉 {target.name} FLIP 7 during Flip Three! (+15 bonus)."
                )
                self.end_round(flip7_player=target)
                return

        elif card.kind == "modifier":
            target.modifiers.append(card.name)

        elif card.effect == "second_chance":
            if not target.second_chance:
                target.second_chance = True
                self.log_fn(f"{target.name} keeps a Second Chance.")
            else:
                self._assign_second_chance_or_discard(
                    owner=source_player,
                    source_name=target.name,
                    resume={"kind": "forced_queue"}
                )
                return

        elif card.effect in {"freeze", "flip_three"}:
            # Official rule: these are resolved after the current three-card
            # sequence, not in the middle of it.
            ctx["queued_actions"].append(
                {"effect": card.effect, "owner_id": source_player.player_id}
            )

        if ctx["remaining"] > 0:
            self._forced_draw_next()
        else:
            self._process_forced_queue()

    def _queue_or_prompt_action(
        self,
        card: Card,
        owner: Player,
        resume: dict[str, Any],
    ) -> None:
        # Second Chance has special assignment rules.
        if card.effect == "second_chance":
            if not owner.second_chance:
                owner.second_chance = True
                self.log_fn(f"{owner.name} keeps a Second Chance.")
                self._resume(resume)
                return

            self._assign_second_chance_or_discard(
                owner=owner,
                source_name=owner.name,
                resume=resume,
            )
            return

        # Freeze/Flip Three target an active player. If only one is active,
        # they must target themselves.
        self.pending_action = {
            "effect": card.effect,
            "owner_id": owner.player_id,
            "resume": resume,
        }
        self._prompt_action_target()

    def _assign_second_chance_or_discard(
        self,
        owner: Player,
        source_name: str,
        resume: dict[str, Any] | None = None,
    ) -> None:
        candidates = [p for p in self.active_players() if not p.second_chance]

        if not candidates:
            self.log_fn(
                f"{source_name}'s extra Second Chance is discarded "
                f"(no eligible active player)."
            )
            if resume:
                self._resume(resume)
            else:
                self._process_forced_queue()
            return

        if len(candidates) == 1:
            candidates[0].second_chance = True
            self.log_fn(
                f"{source_name} gives Second Chance to {candidates[0].name}."
            )
            if resume:
                self._resume(resume)
            else:
                self._process_forced_queue()
            return

        self.pending_action = {
            "effect": "second_chance",
            "owner_id": owner.player_id,
            "resume": resume or {"kind": "forced_queue"},
        }
        self._prompt_action_target(candidates=candidates)

    def _prompt_action_target(self, candidates: list[Player] | None = None) -> None:
        action = self.pending_action
        if not action:
            return

        if candidates is None:
            candidates = self.active_players()

        owner = self.player_by_id(action["owner_id"])

        if not candidates:
            self.log_fn(
                f"{owner.name}'s action has no valid target and is discarded."
            )
            self._complete_pending_action(None)
            return

        if len(candidates) == 1:
            target = candidates[0]
            self._complete_pending_action(target.player_id)
            return

        self.phase = "action_target"

    def handle_action_target(self, player_id: str, target_id: str) -> None:
        if self.phase != "action_target" or not self.pending_action:
            raise ValueError("No action target is currently requested.")

        action = self.pending_action
        if action["owner_id"] != player_id:
            raise ValueError("You do not control this action.")

        target = self.player_by_id(target_id)
        if target.status != "active":
            raise ValueError("Target must be an active player.")

        if action["effect"] == "second_chance" and target.second_chance:
            raise ValueError("That player already has a Second Chance.")

        self._complete_pending_action(target.player_id)

    def _complete_pending_action(self, target_id: str | None) -> None:
        action = self.pending_action
        self.pending_action = None
        if not action:
            return

        owner = self.player_by_id(action["owner_id"])
        effect = action["effect"]
        resume = action["resume"]

        if target_id is None:
            self._resume(resume)
            return

        target = self.player_by_id(target_id)

        if effect == "second_chance":
            target.second_chance = True
            self.log_fn(
                f"{owner.name} gives Second Chance to {target.name}."
            )
            self._resume(resume)
            return

        if effect == "freeze":
            self.log_fn(f"{owner.name} uses Freeze on {target.name}.")
            self._bank_player(target, reason=f"frozen by {owner.name}")
            self._resume(resume)
            return

        if effect == "flip_three":
            self.log_fn(f"{owner.name} uses Flip Three on {target.name}.")
            new_ctx = {
                "player_id": target.player_id,
                "remaining": 3,
                "queued_actions": [],
                "resume": resume,
            }
            if self.pending_forced is not None:
                self.forced_stack.append(self.pending_forced)

            self.pending_forced = new_ctx
            self.phase = "forced_draw"
            self._forced_draw_next()
            return

        raise RuntimeError(f"Unknown action: {effect}")

    # ---------- forced drawing ----------

    def _forced_draw_next(self) -> None:
        if self.phase != "forced_draw" or not self.pending_forced:
            return

        ctx = self.pending_forced
        target = self.player_by_id(ctx["player_id"])

        if target.status != "active":
            self._finish_forced_draw()
            return

        if ctx["remaining"] <= 0:
            self._process_forced_queue()
            return

        self._deal_to_player(
            target,
            source_player=target,
            resume={"kind": "forced_card"},
            forced=True,
        )

    def _process_forced_queue(self) -> None:
        ctx = self.pending_forced
        if ctx is None:
            return

        if ctx["queued_actions"] and self.player_by_id(
            ctx["player_id"]
        ).status == "active":
            action = ctx["queued_actions"].pop(0)
            owner = self.player_by_id(action["owner_id"])

            # Action is controlled by the player who received it. If that
            # player has busted/frozen by the time it resolves, the card is
            # discarded because they are no longer active.
            if owner.status != "active":
                self.log_fn(
                    f"{owner.name}'s queued {action['effect'].replace('_', ' ').title()} "
                    f"is discarded because they are no longer active."
                )
                self._process_forced_queue()
                return

            self.pending_action = {
                "effect": action["effect"],
                "owner_id": owner.player_id,
                "resume": {"kind": "forced_queue"},
            }
            self._prompt_action_target()
            return

        self._finish_forced_draw()

    def _finish_forced_draw(self) -> None:
        ctx = self.pending_forced
        if ctx is None:
            return

        resume = ctx["resume"]
        self.pending_forced = None

        if self.forced_stack:
            parent = self.forced_stack.pop()
            self.pending_forced = parent
            self.phase = "forced_draw"
            self._process_forced_queue_or_resume_parent()
            return

        self._resume(resume)

    def _process_forced_queue_or_resume_parent(self) -> None:
        ctx = self.pending_forced
        if ctx is None:
            return
        if ctx["queued_actions"]:
            self._process_forced_queue()
        elif ctx["remaining"] > 0:
            self._forced_draw_next()
        else:
            self._finish_forced_draw()

    # ---------- continuations ----------

    def _resume(self, resume: dict[str, Any]) -> None:
        kind = resume["kind"]

        if kind == "initial_deal":
            self.phase = "dealing"
            self._deal_initial_next()
            return

        if kind == "advance_turn":
            self.phase = "turns"
            self._advance_turn()
            return

        if kind == "forced_card":
            self.phase = "forced_draw"
            if self.pending_forced:
                if self.pending_forced["remaining"] <= 0:
                    self._process_forced_queue()
                else:
                    self._forced_draw_next()
            return

        if kind == "forced_queue":
            self.phase = "forced_draw"
            self._process_forced_queue_or_resume_parent()
            return

        raise RuntimeError(f"Unknown continuation: {kind}")

    # ---------- scoring/end ----------

    def _score_player(self, p: Player, flip7: bool = False) -> int:
        if p.status == "busted":
            return 0

        number_sum = sum(p.numbers)
        multiplier = 2 if "x2" in p.modifiers else 1
        flat = sum(
            int(mod[1:])
            for mod in p.modifiers
            if mod.startswith("+")
        )
        bonus = 15 if flip7 else 0
        return (number_sum * multiplier) + flat + bonus

    def _bank_player(self, p: Player, reason: str) -> None:
        if p.status == "busted":
            p.last_round_score = 0
            return

        p.last_round_score = self._score_player(
            p, flip7=(p.status == "flip7")
        )
        p.total_score += p.last_round_score
        p.status = "stayed"
        self.log_fn(
            f"{p.name} banks {p.last_round_score} points ({reason})."
        )

    def _round_over(self) -> bool:
        return not self.active_players()

    def end_round(self, flip7_player: Player | None = None) -> None:
        # If Flip 7 occurred, every other non-busted/non-stayed player banks
        # immediately. If there was no Flip 7, all remaining active players
        # bank their current scores normally.
        if flip7_player is not None:
            for p in self.players:
                if p is flip7_player:
                    p.last_round_score = self._score_player(p, flip7=True)
                    p.total_score += p.last_round_score
                    continue
                if p.status == "active":
                    self._bank_player(p, reason="round ended by Flip 7")
        else:
            for p in self.players:
                if p.status == "active":
                    self._bank_player(p, reason="round ended")

        for p in self.players:
            if p.second_chance:
                p.second_chance = False
                self.log_fn(f"{p.name}'s Second Chance is discarded at round end.")
        
        self.phase = "round_end"
        self.current_index = None
        self.pending_action = None
        self.pending_forced = None
        self.forced_stack.clear()

        # Check winner according to the highest-score rule. If multiple
        # players are tied at/above the configured target score, the game continues.
        contenders = [p for p in self.players if p.total_score >= self.target_score]
        if contenders:
            high = max(p.total_score for p in contenders)
            leaders = [p for p in contenders if p.total_score == high]
            if len(leaders) == 1:
                self.phase = "game_over"
                self.log_fn(
                    f"🏆 {leaders[0].name} wins with {leaders[0].total_score} points!"
                )
                return

        # Rotate dealer clockwise.
        self.dealer_index = (self.dealer_index + 1) % len(self.players)

    def start_next_round(self) -> None:
        if self.phase != "round_end":
            raise ValueError("The round is not over.")
        self._start_round()

    def _discard_cards(self, *cards: Card) -> None:
        # Deliberately a no-op: the requested house rule uses a fresh 94-card
        # refill instead of rebuilding the stack from prior discards.
        _ = cards

    def public_snapshot(self) -> dict[str, Any]:
        current_id = (
            self.players[self.current_index].player_id
            if self.current_index is not None
            else None
        )

        return {
            "phase": self.phase,
            "round": self.round_number,
            "dealer_id": self.players[self.dealer_index].player_id
            if self.players else None,
            "current_player_id": current_id,
            "draw_pile_count": len(self.draw_pile),
            "target_score": self.target_score,
            "players": self.public_players(),
            "standings": self.standings(),
        }

    def private_hand(self, player_id: str) -> dict[str, Any]:
        p = self.player_by_id(player_id)
        return {
            "player_id": player_id,
            "numbers": list(p.numbers),
            "modifiers": list(p.modifiers),
            "second_chance": p.second_chance,
            "status": p.status,
            "round_score": self._score_player(p, flip7=(p.status == "flip7")),
        }
