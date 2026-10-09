from __future__ import annotations

import json
import random
import secrets
import socket
import string
import threading
import time
import uuid
from typing import Any, Callable

from .game import Game, Player

TCP_PORT = 37071
UDP_PORT = 37072
DISCOVERY_MAGIC = "FLIP7_DISCOVER_V1"
DISCOVERY_RESPONSE = "FLIP7_TABLE_V1"
ROOM_IDLE_TIMEOUT = 30 * 60
PLAYER_ACTION_TIMEOUT = 5 * 60
MAX_PLAYER_NAME_LENGTH = 24
TIMEOUT_CHECK_INTERVAL = 1
PLAYER_COLORS = [
    "red", "green", "blue", "yellow", "magenta", "cyan",
    "white", "bright_red", "bright_green", "bright_yellow",
    "bright_blue", "bright_magenta", "bright_cyan", "bright_white", "orange1", "purple"
]

def send_json(sock: socket.socket, payload: dict[str, Any]) -> None:
    data = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
    sock.sendall(data)


def recv_messages(sock: socket.socket):
    buffer = b""
    while True:
        chunk = sock.recv(4096)
        if not chunk:
            return
        buffer += chunk
        while b"\n" in buffer:
            raw, buffer = buffer.split(b"\n", 1)
            if not raw.strip():
                continue
            yield json.loads(raw.decode("utf-8"))


class TableServer:
    def __init__(
        self,
        table_name: str,
        host_player_name: str,
        tcp_port: int = TCP_PORT,
        udp_port: int = UDP_PORT,
        target_score: int = Game.WIN_SCORE,
    ):
        self.table_name = table_name
        self.tcp_port = tcp_port
        self.udp_port = udp_port
        self.code = self._new_code()
        self.host_token = secrets.token_urlsafe(18)
        if target_score < 1:
            raise ValueError("Target score must be at least 1.")
        if not 1 <= tcp_port <= 65535:
            raise ValueError("TCP port must be between 1 and 65535.")
        if not 1 <= udp_port <= 65535:
            raise ValueError("UDP port must be between 1 and 65535.")
        self.target_score = target_score

        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._connections: dict[str, socket.socket] = {}
        self._player_by_conn: dict[socket.socket, Player] = {}
        self._player_id_by_conn: dict[socket.socket, str] = {}
        self._empty_since: float | None = None
        self._action_deadlines: dict[str, float] = {}
        self._prompt_counter = 0
        self._available_colors = PLAYER_COLORS.copy()
        random.shuffle(self._available_colors)

        self.players: list[Player] = []
        self.host_player_name = host_player_name
        self.game = Game(
            self.players,
            log_fn=self.broadcast_log,
            deck_notice_fn=self.broadcast_deck_refill,
            target_score=self.target_score,
        )
        self.current_prompt_id: int | None = None
        self._lobby_prompt_active = False
        self._lobby_revision = 0
        self._tcp_sock: socket.socket | None = None
        self._udp_sock: socket.socket | None = None

    @staticmethod
    def _new_code() -> str:
        alphabet = "".join(
            c for c in string.ascii_uppercase + string.digits
            if c not in "O0I1"
        )
        return "".join(secrets.choice(alphabet) for _ in range(6))

    def start(self) -> None:
        self._start_tcp()
        self._start_udp()

        host = Player(
            player_id=uuid.uuid4().hex[:8],
            name=self.host_player_name,
            is_host=True,
        )
        self._host_player = host
        threading.Thread(
            target=self._room_idle_loop,
            name="flip7-room-idle",
            daemon=True,
        ).start()
        threading.Thread(
            target=self._player_action_timeout_loop,
            name="flip7-action-timeout",
            daemon=True,
        ).start()
        # The actual TCP player connection is created by the local client,
        # just like every remote player.
        print(
            f"\nTable '{self.table_name}' is hosting.\n"
            f"Table code: {self.code}\n"
            f"TCP port: {self.tcp_port}\n"
        )

    def stop(self) -> None:
        self._stop.set()
        for sock in list(self._connections.values()):
            try:
                sock.close()
            except OSError:
                pass
        if self._tcp_sock:
            try:
                self._tcp_sock.close()
            except OSError:
                pass
        if self._udp_sock:
            try:
                self._udp_sock.close()
            except OSError:
                pass

    def _room_idle_loop(self) -> None:
        while not self._stop.wait(TIMEOUT_CHECK_INTERVAL):
            with self._lock:
                if self._connections:
                    self._empty_since = None
                    continue

                if self._empty_since is None:
                    self._empty_since = time.monotonic()
                    continue

                if time.monotonic() - self._empty_since >= ROOM_IDLE_TIMEOUT:
                    self.stop()
                    return

    def _start_action_deadline(self, player_id: str) -> None:
        """Start/reset the response timer for a player who needs to act."""
        self._action_deadlines[player_id] = (
            time.monotonic() + PLAYER_ACTION_TIMEOUT
        )

    def _clear_action_deadline(self, player_id: str) -> None:
        self._action_deadlines.pop(player_id, None)

    @staticmethod
    def _player_log_name(player: Player) -> str:
        """Mark the host explicitly in departure/disconnect log entries."""
        return f"{player.name} [host]" if player.is_host else player.name

    def _player_action_timeout_loop(self) -> None:
        """
        Disconnect a player only when the game is actively waiting for that
        player's response and their response deadline expires.
        """
        while not self._stop.wait(TIMEOUT_CHECK_INTERVAL):
            with self._lock:
                now = time.monotonic()
                expired = [
                    player_id
                    for player_id, deadline in self._action_deadlines.items()
                    if now >= deadline
                ]

                for player_id in expired:
                    self._action_deadlines.pop(player_id, None)

                    player = next(
                        (
                            p for p in self.players
                            if p.player_id == player_id
                        ),
                        None,
                    )
                    conn = self._connections.get(player_id)

                    if player is None or conn is None:
                        continue

                    player_log_name = self._player_log_name(player)
                    self.broadcast_log(
                        f"⏰ {player_log_name} did not respond within "
                        f"{PLAYER_ACTION_TIMEOUT // 60} minutes and was "
                        f"automatically disconnected."
                    )

                    # If this player currently owns an action-target decision,
                    # resolve/cancel that pending action before removing the player.
                    if (
                        self.game.phase == "action_target"
                        and self.game.pending_action
                        and self.game.pending_action["owner_id"] == player_id
                    ):
                        self.game._complete_pending_action(None)

                    self.current_prompt_id = None

                    # Permanently remove the player from Game.players.
                    try:
                        removed, was_current = self.game.remove_player(player_id)
                        self._release_player_color(removed.color)
                    except KeyError:
                        was_current = False

                    if (
                        was_current
                        and self.game.phase == "turns"
                        and self.game.current_index is not None
                    ):
                        self.game._advance_turn()

                    if not self.players:
                        self._empty_since = time.monotonic()

                    self.broadcast_log(
                        f"{player_log_name} was removed from the table."
                    )
                    self.broadcast_snapshot()
                    self.send_private_all()
                    self._send_prompt_if_needed()

                    try:
                        conn.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass

                    try:
                        conn.close()
                    except OSError:
                        pass

    def _start_tcp(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", self.tcp_port))
        sock.listen(32)
        sock.settimeout(1.0)
        self._tcp_sock = sock
        threading.Thread(
            target=self._accept_loop,
            name="flip7-accept",
            daemon=True,
        ).start()

    def _start_udp(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", self.udp_port))
        sock.settimeout(1.0)
        self._udp_sock = sock
        threading.Thread(
            target=self._discovery_loop,
            name="flip7-discovery",
            daemon=True,
        ).start()

    def _accept_loop(self) -> None:
        assert self._tcp_sock is not None
        while not self._stop.is_set():
            try:
                conn, addr = self._tcp_sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break

            threading.Thread(
                target=self._client_loop,
                args=(conn, addr),
                name=f"flip7-client-{addr[0]}",
                daemon=True,
            ).start()

    def _discovery_loop(self) -> None:
        assert self._udp_sock is not None
        while not self._stop.is_set():
            try:
                data, addr = self._udp_sock.recvfrom(1024)
            except socket.timeout:
                continue
            except OSError:
                break

            if data.decode("utf-8", "ignore").strip() != DISCOVERY_MAGIC:
                continue

            with self._lock:
                payload = {
                    "type": DISCOVERY_RESPONSE,
                    "table_name": self.table_name,
                    "code": self.code,
                    "players": len(self.players),
                    "max_players": Game.MAX_PLAYERS,
                    "target_score": self.game.target_score,
                    "tcp_port": self.tcp_port,
                }

            try:
                self._udp_sock.sendto(
                    json.dumps(payload).encode("utf-8"),
                    (addr[0], addr[1]),
                )
            except OSError:
                pass

    def _claim_player_color(self) -> str:
        if not self._available_colors:
            return random.choice(PLAYER_COLORS)
        return self._available_colors.pop()

    def _release_player_color(self, color: str) -> None:
        if color and color not in self._available_colors:
            self._available_colors.append(color)
            random.shuffle(self._available_colors)

    def _client_loop(self, conn: socket.socket, addr) -> None:
        player: Player | None = None
        try:
            conn.settimeout(None)
            messages = recv_messages(conn)
            first = next(messages, None)
            if not first or first.get("type") != "hello":
                send_json(conn, {"type": "error", "message": "Expected hello."})
                return

            if first.get("code", "").upper() != self.code:
                send_json(conn, {"type": "error", "message": "Incorrect table code."})
                return

            name = str(first.get("name", "")).strip()
            if not name:
                send_json(conn, {"type": "error", "message": "Player name is required."})
                return
            if len(name) > MAX_PLAYER_NAME_LENGTH:
                send_json(conn, {
                    "type": "error",
                    "message": f"Player name must be {MAX_PLAYER_NAME_LENGTH} characters or fewer.",
                })
                return
            if any(ord(char) < 32 for char in name):
                send_json(conn, {
                    "type": "error",
                    "message": "Player name cannot contain control characters.",
                })
                return

            with self._lock:
                if self.game.phase not in {"lobby", "round_end"}:
                    send_json(conn, {
                        "type": "error",
                        "message": "This game is already in progress.",
                    })
                    return

                if len(self.players) >= Game.MAX_PLAYERS:
                    send_json(conn, {
                        "type": "error",
                        "message": "Table is full.",
                    })
                    return

                if any(p.name.casefold() == name.casefold() for p in self.players):
                    send_json(conn, {
                        "type": "error",
                        "message": "That player name is already taken.",
                    })
                    return

                is_real_host = (
                    str(first.get("host_token", "")) == self.host_token
                )
                if name.casefold() == self.host_player_name.casefold() and not is_real_host:
                    send_json(conn, {
                        "type": "error",
                        "message": "That name is reserved for the host.",
                    })
                    return
                if is_real_host and name.casefold() != self.host_player_name.casefold():
                    send_json(conn, {
                        "type": "error",
                        "message": "Host token does not match the host player name.",
                    })
                    return

                player = Player(
                    player_id=uuid.uuid4().hex[:8],
                    name=name,
                    is_host=is_real_host,
                    color=self._claim_player_color()
                )

                self.players.append(player)
                if self.game.phase == "lobby":
                    self._lobby_revision += 1
                self._connections[player.player_id] = conn
                self._player_by_conn[conn] = player
                self._player_id_by_conn[conn] = player.player_id
                self._empty_since = None

            send_json(conn, {
                "type": "welcome",
                "player_id": player.player_id,
                "is_host": player.is_host,
                "table_name": self.table_name,
                "code": self.code,
            })
            self.broadcast_snapshot()
            self._send_prompt_if_needed()

            for message in messages:
                self._handle_message(player, message)

        except (ConnectionError, OSError, StopIteration, json.JSONDecodeError):
            pass
        finally:
            if player is not None:
                self._disconnect_player(player, conn)

    def _handle_message(self, player: Player, message: dict[str, Any]) -> None:
        kind = message.get("type")

        try:
            with self._lock:
                if kind == "start_game":
                    if not player.is_host:
                        raise ValueError("Only the host can start the game.")

                    if self.game.phase != "lobby":
                        raise ValueError("The game has already started.")

                    current_ids = [p.player_id for p in self.players]
                    raw_displayed_ids = message.get("displayed_player_ids")
                    displayed_ids_are_valid = (
                        isinstance(raw_displayed_ids, list)
                        and all(isinstance(item, str) for item in raw_displayed_ids)
                    )
                    displayed_ids = raw_displayed_ids if displayed_ids_are_valid else []
                    displayed_revision = message.get("lobby_revision")
                    if (
                        displayed_revision != self._lobby_revision
                        or not displayed_ids_are_valid
                        or displayed_ids != current_ids
                    ):
                        newly_joined = set(current_ids) - set(displayed_ids) # pyright: ignore [reportArgumentType]
                        if newly_joined:
                            warning = (
                                "New players have joined since this roster was displayed. "
                                "The game has not started; review the refreshed roster."
                            )
                        else:
                            warning = (
                                "The lobby roster changed since it was displayed. "
                                "The game has not started; review the refreshed roster."
                            )
                        self._send_to_player(player.player_id, {
                            "type": "lobby_stale",
                            "message": warning,
                        })
                        self.broadcast_snapshot()
                        return

                    if not Game.MIN_PLAYERS <= len(self.players) <= Game.MAX_PLAYERS:
                        raise ValueError(
                            f"Need {Game.MIN_PLAYERS}-{Game.MAX_PLAYERS} players."
                        )

                    self._lobby_prompt_active = False
                    self.game.start_game()
                    self.broadcast_snapshot()
                    self.send_private_all()
                    self._send_prompt_if_needed()

                elif kind == "refresh_lobby":
                    if not player.is_host:
                        raise ValueError("Only the host can refresh the lobby.")
                    if self.game.phase != "lobby":
                        raise ValueError("The game is no longer in the lobby.")
                    self.broadcast_snapshot()
                    self._send_to_player(player.player_id, {
                        "type": "lobby_refreshed",
                    })

                elif kind == "choice":
                    prompt_id = message.get("prompt_id")

                    if prompt_id != self.current_prompt_id:
                        raise ValueError("This prompt is no longer active.")

                    self.game.handle_choice(
                        player.player_id,
                        str(message.get("choice", "")),
                    )

                    self.current_prompt_id = None
                    self._clear_action_deadline(player.player_id)

                    self.broadcast_snapshot()
                    self.send_private_all()
                    self._send_prompt_if_needed()

                    if self.game.phase == "round_end":
                        self._schedule_next_round()

                elif kind == "action_target":
                    prompt_id = message.get("prompt_id")

                    if prompt_id != self.current_prompt_id:
                        raise ValueError("This prompt is no longer active.")

                    self.game.handle_action_target(
                        player.player_id,
                        str(message.get("target_id", "")),
                    )

                    self.current_prompt_id = None
                    self._clear_action_deadline(player.player_id)

                    self.broadcast_snapshot()
                    self.send_private_all()
                    self._send_prompt_if_needed()

                    if self.game.phase == "round_end":
                        self._schedule_next_round()

                elif kind == "leave":
                    raise ConnectionError("Player left.")

                else:
                    raise ValueError("Unknown command.")

        except (ValueError, ConnectionError) as exc:
            # A failed lobby start should leave the live lobby prompt active.

            try:
                send_json(
                    self._connections[player.player_id],
                    {
                        "type": "error",
                        "message": str(exc),
                    },
                )
                if kind == "start_game" and self.game.phase == "lobby":
                    self._send_prompt_if_needed()
            except OSError:
                pass

    def _schedule_next_round(self) -> None:
        if self.game.phase != "round_end":
            return

        # Give clients time to see the standings before the log is cleared.
        threading.Timer(3.0, self._start_next_round_if_still_ready).start()

    def _start_next_round_if_still_ready(self) -> None:
        with self._lock:
            if self._stop.is_set() or self.game.phase != "round_end":
                return
            if not Game.MIN_PLAYERS <= len(self.players) <= Game.MAX_PLAYERS:
                return

            # Explicitly clear log through a reset event before the new round.
            self.broadcast({
                "type": "round_reset",
                "standings": self.game.standings(),
            })
            self.game.start_next_round()
            self.broadcast_snapshot()
            self.send_private_all()
            self._send_prompt_if_needed()

    def _disconnect_player(
        self,
        player: Player,
        conn: socket.socket,
    ) -> None:
        with self._lock:
            # The timeout path may already have removed this player.
            # Do not process the same disconnect twice.
            if (
                player.player_id not in self._connections
                and player not in self.players
            ):
                return

            self._connections.pop(player.player_id, None)
            self._player_by_conn.pop(conn, None)
            self._player_id_by_conn.pop(conn, None)
            self._clear_action_deadline(player.player_id)

            was_current = (
                self.game.current_index is not None
                and self.players[self.game.current_index] is player
            )

            # Cancel any pending action owned by this player before
            # removing them, because the game state still needs the Player.
            if (
                self.game.phase == "action_target"
                and self.game.pending_action
                and self.game.pending_action["owner_id"] == player.player_id
            ):
                self.game._complete_pending_action(None)

            # Permanently remove from the game's player list.
            was_lobby = self.game.phase == "lobby" and player in self.players
            try:
                removed, removed_was_current = self.game.remove_player(
                    player.player_id
                )
                self._release_player_color(removed.color)
                if was_lobby:
                    self._lobby_revision += 1
                was_current = was_current or removed_was_current
            except KeyError:
                pass

            self.current_prompt_id = None

            if (
                was_current
                and self.game.phase == "turns"
                and self.game.current_index is not None
            ):
                self.game._advance_turn()

            if not self.players:
                self._empty_since = time.monotonic()

            self.broadcast_log(
                f"{self._player_log_name(player)} left the table."
            )
            self.broadcast_snapshot()
            self.send_private_all()
            self._send_prompt_if_needed()

    # ---------- outbound ----------

    def broadcast(self, payload: dict[str, Any]) -> None:
        dead: list[str] = []
        for player_id, conn in list(self._connections.items()):
            try:
                send_json(conn, payload)
            except OSError:
                dead.append(player_id)

        for player_id in dead:
            self._connections.pop(player_id, None)

    def broadcast_log(self, line: str) -> None:
        self.broadcast({"type": "log", "line": line})

    def broadcast_snapshot(self) -> None:
        with self._lock:
            snapshot = self.game.public_snapshot()
            snapshot["lobby_revision"] = self._lobby_revision
            snapshot["max_players"] = Game.MAX_PLAYERS
        self.broadcast({
            "type": "snapshot",
            "snapshot": snapshot,
        })

    def broadcast_deck_refill(self, cards: int) -> None:
        self.broadcast({
            "type": "deck_refill",
            "message": (
                f"The deck stack is empty. Refilling with one deck (94 cards)."
            ),
            "cards": cards,
        })

    def send_private_all(self) -> None:
        for player_id, conn in list(self._connections.items()):
            try:
                send_json(conn, {
                    "type": "private_hand",
                    "hand": self.game.private_hand(player_id),
                })
            except OSError:
                pass

    def _next_prompt_id(self) -> int:
        self._prompt_counter += 1
        return self._prompt_counter

    def _send_prompt_if_needed(self) -> None:
        # Join/disconnect threads may call this concurrently. Guard the one
        # logical lobby prompt so simultaneous joins cannot enqueue duplicates.
        with self._lock:
            self._send_prompt_if_needed_locked()

    def _send_prompt_if_needed_locked(self) -> None:
        if self.game.phase == "lobby":
            host = next((p for p in self.players if p.is_host), None)
            count = len(self.players)

            if host is None or count < Game.MIN_PLAYERS:
                self._lobby_prompt_active = False
                return

            # One logical lobby prompt is enough. The client's input prompt
            # stays active and its full Rich screen redraws from snapshots;
            # joins must never queue another Start prompt.
            if not self._lobby_prompt_active:
                self._lobby_prompt_active = True
                self._send_to_player(host.player_id, {
                    "type": "prompt",
                    "kind": "lobby",
                    "message": "Review the lobby, then type start.",
                })
            return

        self._lobby_prompt_active = False

        if self.game.phase == "turns" and self.game.current_index is not None:
            player = self.players[self.game.current_index]
            self._start_action_deadline(player.player_id)
            self.current_prompt_id = self._next_prompt_id()

            self._send_to_player(player.player_id, {
                "type": "prompt",
                "prompt_id": self.current_prompt_id,
                "kind": "choice",
                "message": (
                    "Choose hit or stand."
                    f" (You have {PLAYER_ACTION_TIMEOUT // 60} minutes to respond.)"
                ),
            })
            return

        if self.game.phase == "action_target" and self.game.pending_action:
            action = self.game.pending_action
            owner = self.game.player_by_id(action["owner_id"])

            if action["effect"] == "second_chance":
                candidates = [
                    p for p in self.players
                    if p.status == "active" and not p.second_chance
                ]
            else:
                candidates = [
                    p for p in self.players if p.status == "active"
                ]

            self._start_action_deadline(owner.player_id)
            self.current_prompt_id = self._next_prompt_id()
            self._send_to_player(owner.player_id, {
                "type": "prompt",
                "prompt_id": self.current_prompt_id,
                "kind": "target",
                "effect": action["effect"],
                "message": (
                    "Choose a target for "
                    f"{action['effect'].replace('_', ' ')}. "
                    f" (You have {PLAYER_ACTION_TIMEOUT // 60} minutes to respond.)"
                    ),
                "candidates": [
                    {"player_id": p.player_id, "name": p.name}
                    for p in candidates
                ],
            })

    def _send_to_player(self, player_id: str, payload: dict[str, Any]) -> None:
        conn = self._connections.get(player_id)
        if not conn:
            return
        try:
            send_json(conn, payload)
        except OSError:
            pass
