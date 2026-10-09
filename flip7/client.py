from __future__ import annotations

import io
import json
import socket
import threading
import time
from queue import Queue, Empty
from typing import Any

from prompt_toolkit.application import Application
from prompt_toolkit.application.current import get_app
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.widgets import TextArea
from rich.console import Console
from rich.panel import Panel
from rich.markup import escape
from rich.table import Table
from rich.text import Text



DISCOVERY_MAGIC = "FLIP7_DISCOVER_V1"
UDP_PORT = 37072
UI_MAX_WIDTH = 120


def recv_messages(sock: socket.socket, out_queue: Queue, on_message=None) -> None:
    buffer = b""
    try:
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                message = {"type": "disconnected"}
                if on_message is not None:
                    try:
                        on_message(message)
                    except Exception:
                        pass
                out_queue.put(message)
                return
            buffer += chunk
            while b"\n" in buffer:
                raw, buffer = buffer.split(b"\n", 1)
                if raw.strip():
                    message = json.loads(raw.decode("utf-8"))
                    if on_message is not None:
                        try:
                            on_message(message)
                        except Exception:
                            pass
                    out_queue.put(message)
    except (OSError, json.JSONDecodeError) as exc:
        message = {"type": "disconnected", "message": str(exc)}
        if on_message is not None:
            try:
                on_message(message)
            except Exception:
                pass
        out_queue.put(message)


def send_json(sock: socket.socket, payload: dict[str, Any]) -> None:
    sock.sendall(
        (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
    )


def discover_tables(timeout: float = 1.5) -> list[dict[str, Any]]:
    """Discover all Flip 7 tables responding on the local LAN."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.settimeout(0.25)

    seen: dict[tuple[str, int], dict[str, Any]] = {}
    deadline = time.monotonic() + timeout

    try:
        payload = DISCOVERY_MAGIC.encode("utf-8")
        # Global LAN broadcast. For home Wi-Fi and personal hotspot networks
        # this is the most portable discovery mechanism.
        sock.sendto(payload, ("255.255.255.255", UDP_PORT))

        while time.monotonic() < deadline:
            try:
                data, addr = sock.recvfrom(2048)
            except socket.timeout:
                continue

            try:
                item = json.loads(data.decode("utf-8"))
            except json.JSONDecodeError:
                continue

            if item.get("type") != "FLIP7_TABLE_V1":
                continue

            try:
                tcp_port = int(item["tcp_port"])
            except (KeyError, TypeError, ValueError):
                continue
            if not 1 <= tcp_port <= 65535:
                continue

            item["host_ip"] = addr[0]
            item["tcp_port"] = tcp_port
            key = (addr[0], tcp_port)
            seen[key] = item
    except OSError:
        pass
    finally:
        sock.close()

    return sorted(
        seen.values(),
        key=lambda t: (t["table_name"].lower(), t["host_ip"]),
    )


class Client:
    def __init__(
        self,
        host: str,
        port: int,
        code: str,
        player_name: str,
        console: Console | None = None,
        host_token: str | None = None,
    ):
        self.host = host
        self.port = port
        self.code = code
        self.player_name = player_name
        self.host_token = host_token

        self.console = console or Console()
        self._application: Application | None = None
        self.sock: socket.socket | None = None
        self.inbox: Queue = Queue()
        self.receiver: threading.Thread | None = None

        self.player_id: str | None = None
        self.is_host = False
        self.snapshot: dict[str, Any] = {}
        self.hand: dict[str, Any] = {}
        self.log: list[str] = []
        self.last_prompt: dict[str, Any] | None = None
        self.last_handled_prompt_id: int | None = None
        self.ui_dirty = True
        self.running = True
        self.lobby_player_count = 0
        self._state_lock = threading.RLock()
        self._hook_processed_ids: set[int] = set()
        self.rendered_lobby_revision: int | None = None
        self.rendered_lobby_player_ids: list[str] = []
        self.rendered_prompt_id: int | None = None
        self.rendered_prompt_kind: str | None = None
        self.status_message: tuple[str, str] | None = None

    def _receiver_message_hook(self, msg: dict[str, Any]) -> None:
        """Apply network updates immediately while the live input is active."""
        with self._state_lock:
            self._hook_processed_ids.add(id(msg))
            self._process(msg, from_receiver=True)

        # The full-screen prompt_toolkit app owns the terminal while waiting
        # for input. Invalidation redraws the complete original Rich screen.
        app = self._application
        if app is not None:
            try:
                app.invalidate()
                if not self.running:
                    # The input handler may already have requested exit (for
                    # example, after q closes the socket). Check the app's
                    # future on the UI thread so a second exit request cannot
                    # raise from prompt_toolkit's event loop.
                    app.loop.call_soon_threadsafe(
                        self._exit_application_if_running, app
                    )
            except (AttributeError, RuntimeError, AssertionError):
                pass

    @staticmethod
    def _exit_application_if_running(app: Application) -> None:
        try:
            future = getattr(app, "future", None)
            if not getattr(app, "is_running", False):
                return
            if future is not None and future.done():
                return
            app.exit(result="")
        except Exception:
            # Shutdown can race with a receiver-thread notification; exiting
            # twice should never break the client UI.
            pass

    def connect(self) -> None:
        sock = socket.create_connection((self.host, self.port), timeout=5)
        sock.settimeout(None)
        self.sock = sock

        send_json(sock, {
            "type": "hello",
            "code": self.code,
            "name": self.player_name,
            "host_token": self.host_token or "",
        })

        self.receiver = threading.Thread(
            target=recv_messages,
            args=(sock, self.inbox, self._receiver_message_hook),
            name="flip7-receiver",
            daemon=True,
        )
        self.receiver.start()

        while self.player_id is None:
            msg = self.inbox.get()

            if msg.get("type") == "error":
                raise ConnectionError(
                    msg.get("message", "Connection refused.")
                )

            if msg.get("type") == "disconnected":
                raise ConnectionError(
                    "Disconnected before joining the table."
                )

            self._process(msg)

        # Wait for the first authoritative snapshot.
        # run() must never render an empty "connecting" state.
        while not self.snapshot:
            msg = self.inbox.get()

            if msg.get("type") == "error":
                raise ConnectionError(
                    msg.get("message", "Connection failed.")
                )

            if msg.get("type") == "disconnected":
                raise ConnectionError(
                    "Disconnected before receiving the table state."
                )

            self._process(msg)

        self.ui_dirty = False

    def run(self) -> None:
        """Keep one full-screen application alive for the whole session.

        Recreating Application after every Enter caused prompt_toolkit to leave
        the alternate screen, briefly reveal the original terminal, and then
        enter the game screen again. Keep the same application active between
        moves so network updates and input submissions redraw in place.
        """
        self._drain_messages()

        def accept(buffer) -> bool:
            answer = buffer.text
            self._handle_input(answer)

            if not self.running:
                # Exit only when the player quits or the client shuts down.
                get_app().exit(result=None)

            # False tells prompt_toolkit to clear the input buffer while
            # keeping the current Application running. The full-screen view
            # is therefore never torn down between moves.
            return False

        input_area = TextArea(
            multiline=False,
            prompt="> ",
            wrap_lines=False,
            accept_handler=accept,
        )
        app = Application(
            layout=Layout(
                HSplit([
                    Window(
                        content=FormattedTextControl(
                            self._render_screen_ansi
                        ),
                        wrap_lines=False,
                    ),
                    input_area,
                ]),
                focused_element=input_area,
            ),
            full_screen=True,
            refresh_interval=0.4,
            mouse_support=False,
        )
        self._application = app
        try:
            if self.running:
                app.run()
        except (EOFError, KeyboardInterrupt):
            self.close()
        finally:
            self._application = None

    def close(self) -> None:
        self.running = False
        if self.sock:
            try:
                send_json(self.sock, {"type": "leave"})
            except OSError:
                pass
            try:
                self.sock.close()
            except OSError:
                pass

    def _drain_messages(self) -> None:
        while True:
            try:
                msg = self.inbox.get_nowait()
            except Empty:
                break
            self._process(msg)

    def _process(self, msg: dict[str, Any], from_receiver: bool = False) -> None:
        # recv_messages invokes _receiver_message_hook before queuing the same
        # dict. The hook already applied it; consume the queued copy once so
        # logs and prompts are never duplicated.
        with self._state_lock:
            if not from_receiver and id(msg) in self._hook_processed_ids:
                self._hook_processed_ids.discard(id(msg))
                return

            self._apply_message(msg)

    def _apply_message(self, msg: dict[str, Any]) -> None:
        kind = msg.get("type")

        if kind == "welcome":
            self.player_id = msg["player_id"]
            self.is_host = bool(msg["is_host"])
            return

        if kind == "snapshot":
            self.snapshot = msg["snapshot"]
            self.lobby_player_count = len(self.snapshot.get("players", []))

            # A lobby prompt can still be queued locally when the host
            # starts the game. Once the authoritative snapshot says we
            # left the lobby, discard that stale prompt.
            if self.snapshot.get("phase") != "lobby":
                if self.last_prompt and self.last_prompt.get("kind") == "lobby":
                    self.last_prompt = None

            self.ui_dirty = True
            return

        if kind == "private_hand":
            hand = msg["hand"]
            if hand.get("player_id") == self.player_id:
                self.hand = hand
                self.ui_dirty = True
            return

        if kind == "log":
            self.log.append(msg["line"])
            self.log = self.log[-30:]
            self.ui_dirty = True
            return

        if kind == "deck_refill":
            self.log.append(msg["message"])
            self.log = self.log[-30:]
            self.ui_dirty = True
            return

        if kind == "round_reset":
            self.log.clear()
            self.ui_dirty = True
            return

        if kind == "prompt":
            if msg.get("kind") == "lobby" and self.snapshot.get("phase") != "lobby":
                return

            prompt_id = msg.get("prompt_id")
            if (
                prompt_id is not None
                and self.last_handled_prompt_id is not None
                and prompt_id <= self.last_handled_prompt_id
            ):
                return

            self.last_prompt = msg
            self.ui_dirty = True

            if msg.get("kind") in {"choice", "target"}:
                try:
                    self.console.bell()
                except Exception:
                    pass
            return

        if kind in {"error", "lobby_stale"}:
            message = str(msg.get("message", "The server rejected that command."))
            self.status_message = (message, "bold red")
            self.ui_dirty = True
            return

        if kind == "lobby_refreshed":
            self.status_message = ("Lobby refreshed.", "bold green")
            self.ui_dirty = True
            return

        if kind == "disconnected":
            message = msg.get("message") or "Disconnected from the table."
            self.status_message = (str(message), "bold red")
            self.ui_dirty = True
            self.running = False
            return

    # ---------- UI ----------

    def _render(self) -> None:
        self._render_standings()

        phase = self.snapshot.get("phase")
        round_no = self.snapshot.get("round", 0)

        header = Text()
        header.append("Flip 7 LAN  ", style="bold magenta")
        header.append(f"Round {round_no}  ", style="bold")
        header.append(f"Phase: {phase}", style="dim")
        self.console.print(
            Panel(header, width=self._ui_width(), expand=False),
            justify="center",
        )

        if self.snapshot.get("phase") == "lobby":
            self._render_lobby()
        else:
            self._render_game()

    def _ui_width(self) -> int:
        """Return a readable centered UI width without forcing console resize."""
        return max(40, min(UI_MAX_WIDTH, self.console.width - 2))

    def _render_standings(self) -> None:
        """Render standings with fixed score/status columns and colored status."""
        rows = self.snapshot.get("standings", [])
        width = self._ui_width()

        status_styles = {
            "active": "bold yellow",
            "stayed": "bold green",
            "busted": "bold red",
            "flip7": "bold cyan",
            "waiting": "dim",
        }

        def player_status(row: dict[str, Any]) -> str:
            return next(
                (
                    p["status"]
                    for p in self.snapshot.get("players", [])
                    if p["player_id"] == row["player_id"]
                ),
                "",
            )

        def add_columns(table: Table, prefix: str = "") -> None:
            table.add_column(f"{prefix}#", width=4, justify="right", no_wrap=True)
            table.add_column(f"{prefix}Player", ratio=1, no_wrap=True)
            table.add_column(f"{prefix}Score", width=6, justify="right", no_wrap=True)
            table.add_column(f"{prefix}Status", width=10, no_wrap=True)

        # One to three players use a single four-column table. The score is
        # a dedicated fixed-width column, so long names cannot move it.
        if len(rows) <= 3:
            table = Table(
                title="Standings",
                width=width,
                expand=False,
                show_header=True,
                show_lines=False,
                padding=(0, 1),
            )
            add_columns(table)
            for row in rows:
                table.add_row(*self._standings_cells(
                    row, player_status(row), status_styles
                ))
        else:
            # With more players, use two independent four-column groups.
            # This keeps score/status aligned within each half regardless of
            # player-name length.
            table = Table(
                title="Standings",
                width=width,
                expand=False,
                show_header=False,
                show_lines=False,
                padding=(0, 1),
            )
            add_columns(table, "")
            add_columns(table, "")

            split = (len(rows) + 1) // 2
            left = rows[:split]
            right = rows[split:]

            for index in range(split):
                if index < len(left):
                    left_row = left[index]
                    left_cells = self._standings_cells(left_row, player_status(left_row), status_styles)
                else:
                    left_cells = ("", Text(""), "", Text(""))

                if index < len(right):
                    right_row = right[index]
                    right_cells = self._standings_cells(right_row, player_status(right_row), status_styles)
                else:
                    right_cells = ("", Text(""), "", Text(""))

                table.add_row(*(left_cells + right_cells))

        self.console.print(table, justify="center")

    def _standings_cells(
        self,
        row: dict[str, Any],
        status: str,
        status_styles: dict[str, str],
    ) -> tuple[str, Text, str, Text]:
        marker = " ← you" if row["player_id"] == self.player_id else ""
        name_text = Text()
        name_text.append(str(row["name"]), style=row.get("color", "white"))
        name_text.append(marker)
        status_text = Text(status, style=status_styles.get(status, "white"))
        return (str(row["rank"]), name_text, str(row["score"]), status_text)

    def _render_lobby(self) -> None:
        players = self.snapshot.get("players", [])
        target_score = self.snapshot.get("target_score", 200)
        lines = [
            f"Table code: [bold yellow]{self.code}[/bold yellow]",
            f"Winning target: {target_score} points",
            "",
            "Players:",
        ]
        for p in players:
            host = " (host)" if p.get("is_host") else ""
            lines.append(f"  • {escape(str(p['name']))}{host}")

        if self.is_host:
            lines += [
                "",
                "Host command: type [bold]start[/bold] once 3–16 players are connected.",
            ]
        else:
            lines += [
                "",
                "Waiting for the host to start the game...",
            ]

        self.console.print(
            Panel(
                "\n".join(lines),
                title="Lobby",
                width=self._ui_width(),
                expand=False,
            ),
            justify="center",
        )

    def _render_game(self) -> None:
        current_id = self.snapshot.get("current_player_id")
        current_name = next(
            (
                p["name"]
                for p in self.snapshot.get("players", [])
                if p["player_id"] == current_id
            ),
            "—",
        )
        target_score = self.snapshot.get("target_score", 200)
        
        self.console.print(
            Panel(
                f"Current turn: [bold cyan]{current_name}[/bold cyan]\n"
                f"Cards left in stack: {self.snapshot.get('draw_pile_count', 0)}\n"
                f"Winning target: {target_score} points",
                title="Table",
                width=self._ui_width(),
                expand=False,
            ),
            justify="center",
        )


        self.console.print(
            self._private_hand_panel(),
            justify="center",
        )

        card_count = (
            len(self.hand.get("numbers", []))
            + len(self.hand.get("modifiers", []))
            + (1 if self.hand.get("second_chance") else 0)
        )
        card_width = 13
        gap = 2
        cards_per_row = max(
            1,
            (max(40, self._ui_width() - 4) + gap)
            // (card_width + gap),
        )
        card_rows = max(1, (card_count + cards_per_row - 1) // cards_per_row)
        card_art_height = (card_rows * 5) + max(0, card_rows - 1)
        standings_rows = max(1, (len(self.snapshot.get("standings", [])) + 1) // 2)

        reserved = 3 + standings_rows + 3 + card_art_height + 3
        log_lines = max(4, min(8, self.console.height - reserved))

        if self.log:
            visible_log = self.log[-log_lines:]
            log_text = Text()
            for index, line in enumerate(visible_log):
                if index:
                    log_text.append("\n")
                log_text.append("• ")
                lower_line = line.casefold()
                is_departure = any(
                    marker in lower_line
                    for marker in (
                        "left the table.",
                        "automatically disconnected.",
                        "was removed from the table.",
                    )
                )
                if "BUST" in line.upper() or is_departure:
                    log_text.append(line, style="bold red")
                else:
                    log_text.append(line)
        else:
            log_text = Text("No events yet.")

        self.console.print(
            Panel(
                log_text,
                title=f"Common Log — last {min(len(self.log), log_lines)} events",
                width=self._ui_width(),
                expand=False,
            ),
            justify="center",
        )

    def _private_hand_panel(self) -> Panel:
        cards = []
        cards.extend(
            self._ascii_card(str(n))
            for n in self.hand.get("numbers", [])
        )
        cards.extend(
            self._ascii_card(m)
            for m in self.hand.get("modifiers", [])
        )
        if self.hand.get("second_chance"):
            cards.append(self._ascii_card("2nd Chance"))

        max_width = self._ui_width() - 4
        return Panel(
            self._join_card_art(cards, max_width=max_width)
            if cards
            else "No Cards",
            title=(
                f"Your Cards — Round Score: "
                f"{self.hand.get('round_score', 0)}"
            ),
            width=self._ui_width(),
            expand=False,
        )

    @staticmethod
    def _ascii_card(label: str) -> str:
        inner_width = 11
        clean = str(label)[:inner_width]
        top = "┌" + "─" * inner_width + "┐"
        bottom = "└" + "─" * inner_width + "┘"

        return "\n".join([
            top,
            f"│{clean:<{inner_width}}│",
            f"│{clean:^{inner_width}}│",
            f"│{clean:>{inner_width}}│",
            bottom,
        ])
    
    @staticmethod
    def _join_card_art(
        arts: list[str],
        max_width: int = 100,
    ) -> str:
        if not arts:
            return ""

        card_blocks = [art.splitlines() for art in arts]
        card_width = len(card_blocks[0][0])
        gap = 2

        cards_per_row = max(
            1,
            (max_width + gap) // (card_width + gap),
        )

        rows = []

        for start in range(0, len(card_blocks), cards_per_row):
            group = card_blocks[start:start + cards_per_row]

            for line_index in range(len(group[0])):
                rows.append(
                    (" " * gap).join(
                        card[line_index]
                        for card in group
                    )
                )

            if start + cards_per_row < len(card_blocks):
                rows.append("")

        return "\n".join(rows)

    # ---------- live input / interaction ----------

    def _render_screen_ansi(self) -> ANSI:
        """Render the complete original Rich layout for live prompt-toolkit redraws."""
        buffer = io.StringIO()
        with self._state_lock:
            # Keep the revision tied to the roster that is actually drawn,
            # not just the most recent background snapshot.
            if self.snapshot.get("phase") == "lobby":
                self.rendered_lobby_revision = int(
                    self.snapshot.get("lobby_revision", 0)
                )
                self.rendered_lobby_player_ids = [
                    str(player["player_id"])
                    for player in self.snapshot.get("players", [])
                    if "player_id" in player
                ]

            active_prompt = self.last_prompt
            if active_prompt and active_prompt.get("kind") in {"choice", "target"}:
                self.rendered_prompt_id = active_prompt.get("prompt_id")
                self.rendered_prompt_kind = active_prompt.get("kind")
            else:
                self.rendered_prompt_id = None
                self.rendered_prompt_kind = None

            original_console = self.console
            width = max(40, min(200, int(original_console.width)))
            renderer = Console(
                file=buffer,
                width=width,
                force_terminal=True,
                color_system="standard",
                highlight=False,
                soft_wrap=False,
            )
            self.console = renderer
            try:
                self._render()

                if self.status_message:
                    message, style = self.status_message
                    renderer.print(Text(message, style=style), justify="center")

                phase = self.snapshot.get("phase", "lobby")
                prompt = self.last_prompt
                if phase == "lobby":
                    if self.is_host:
                        renderer.print(Text.from_markup(
                            "\n[bold underline]Start[/bold underline] or [bold underline]Q[/bold underline]uit?"
                        ))
                    else:
                        renderer.print(Text.from_markup(
                            "\nWaiting for the host to start. [bold underline]Q[/bold underline]uit?"
                        ))
                elif prompt and prompt.get("kind") == "choice":
                    renderer.print(Text.from_markup(
                        "\n[bold underline]H[/bold underline]it or [bold underline]S[/bold underline]tay/[bold underline]S[/bold underline]tand?"
                    ))
                elif prompt and prompt.get("kind") == "target":
                    renderer.print(Text(str(prompt.get("message", "Choose a target."))))
                    for index, candidate in enumerate(prompt.get("candidates", []), 1):
                        renderer.print(f"  {index}. {escape(str(candidate['name']))}")
                    renderer.print(Text.from_markup(
                        "[bold underline]Q[/bold underline]uit or choose a target number?"
                    ))
                elif phase in {"game_over", "round_end"}:
                    renderer.print(Text.from_markup(
                        "\nRound complete. [bold underline]Q[/bold underline]uit?"
                    ))
                else:
                    renderer.print(Text.from_markup(
                        "\nWaiting for your turn. [bold underline]Q[/bold underline]uit?"
                    ))
            finally:
                self.console = original_console

        return ANSI(buffer.getvalue())

    def _handle_input(self, raw: str) -> None:
        answer = str(raw).strip().casefold()
        if not answer:
            return

        if answer in {"q", "quit"}:
            self.close()
            return

        with self._state_lock:
            phase = self.snapshot.get("phase", "lobby")
            prompt = self.last_prompt

            if phase == "lobby":
                if not self.is_host:
                    self.status_message = (
                        "Waiting for the host to start the game.", "dim"
                    )
                    return

                if answer == "start":
                    self.status_message = None
                    self._send({
                        "type": "start_game",
                        "lobby_revision": self.rendered_lobby_revision,
                        "displayed_player_ids": list(self.rendered_lobby_player_ids),
                    })
                elif answer in {"r", "refresh"}:
                    self.status_message = ("Refreshing lobby…", "dim")
                    self._send({"type": "refresh_lobby"})
                else:
                    self.status_message = (
                        "Invalid lobby command. Review the roster and try again.",
                        "bold red",
                    )
                return

            if prompt and prompt.get("kind") == "choice":
                if (
                    prompt.get("prompt_id") != self.rendered_prompt_id
                    or self.rendered_prompt_kind != "choice"
                ):
                    self.status_message = (
                        "Your turn prompt changed. Review the screen before responding.",
                        "bold red",
                    )
                    return

                if answer in {"h", "hit"}:
                    choice = "hit"
                elif answer in {"s", "stand", "stay"}:
                    choice = "stand"
                else:
                    self.status_message = (
                        "Input not recognized. Review the prompt and try again.",
                        "bold red",
                    )
                    return

                prompt_id = prompt.get("prompt_id")
                if prompt_id is not None:
                    self.last_handled_prompt_id = prompt_id
                self.last_prompt = None
                self.status_message = None
                self._send({
                    "type": "choice",
                    "choice": choice,
                    "prompt_id": prompt_id,
                })
                return

            if prompt and prompt.get("kind") == "target":
                if (
                    prompt.get("prompt_id") != self.rendered_prompt_id
                    or self.rendered_prompt_kind != "target"
                ):
                    self.status_message = (
                        "The target prompt changed. Review the screen before responding.",
                        "bold red",
                    )
                    return

                candidates = prompt.get("candidates", [])
                try:
                    target = candidates[int(answer) - 1]
                except (ValueError, IndexError):
                    self.status_message = (
                        "Choose one of the listed player numbers.", "bold red"
                    )
                    return

                prompt_id = prompt.get("prompt_id")
                if prompt_id is not None:
                    self.last_handled_prompt_id = prompt_id
                self.last_prompt = None
                self.status_message = None
                self._send({
                    "type": "action_target",
                    "target_id": target["player_id"],
                    "prompt_id": prompt_id,
                })
                return

            self.status_message = (
                "That action is not available right now.", "dim"
            )

    def _send(self, payload: dict[str, Any]) -> None:
        if self.sock is None:
            return
        try:
            send_json(self.sock, payload)
        except OSError:
            self.running = False


def choose_table(console: Console) -> dict[str, Any] | None:
    console.print("\n[bold]Searching for Flip 7 tables on your LAN...[/bold]")
    tables = discover_tables()

    if not tables:
        console.print(
            "[yellow]No tables were discovered.[/yellow]\n"
            "Check that everyone is on the same Wi-Fi/hotspot and that "
            "Python is allowed through the firewall."
        )
        return None

    console.print()
    table = Table(title="Discovered Tables")
    table.add_column("#", justify="right")
    table.add_column("Table")
    table.add_column("Host")
    table.add_column("Players")
    table.add_column("Target")
    table.add_column("Code")

    for i, item in enumerate(tables, 1):
        table.add_row(
            str(i),
            item["table_name"],
            item["host_ip"],
            f"{item['players']}/{item['max_players']}",
            str(item.get("target_score", 200)),
            escape(str(item["code"])),
        )

    console.print(table)

    raw = console.input("Choose table number (or q): ").strip()
    if raw.lower() == "q":
        return None

    try:
        idx = int(raw) - 1
        return tables[idx]
    except (ValueError, IndexError):
        console.print("[red]Invalid selection.[/red]")
        return None
