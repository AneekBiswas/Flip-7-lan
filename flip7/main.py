from __future__ import annotations

import argparse
import sys

from rich.console import Console

from .client import Client, choose_table
from .server import TableServer, TCP_PORT


def valid_port(value: str) -> int:
    port = int(value)
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="flip7",
        description="Flip 7 multiplayer over your local Wi-Fi/hotspot.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    host = sub.add_parser("host", help="Create a table and play as the host.")
    host.add_argument("name", help="Your player name.")
    host.add_argument(
        "--table",
        default="Flip 7 Table",
        help="Table name shown during LAN discovery.",
    )
    host.add_argument(
        "--port",
        type=valid_port,
        default=TCP_PORT,
        help=f"TCP port (default: {TCP_PORT}).",
    )
    host.add_argument(
        "--target",
        "--target-score",
        dest="target_score",
        type=int,
        default=200,
        help="Target score to win the game (default: 200).",
    )

    join = sub.add_parser("join", help="Discover a table and join it.")
    join.add_argument("name", help="Your player name.")
    join.add_argument(
        "--host",
        help="Skip discovery and connect directly to an IP/hostname.",
    )
    join.add_argument(
        "--port",
        type=valid_port,
        default=TCP_PORT,
        help=f"TCP port for direct connections (default: {TCP_PORT}).",
    )
    join.add_argument(
        "--code",
        help="Table code for a direct connection.",
    )
    return p


def main() -> int:
    args = parser().parse_args()
    console = Console()

    if args.command == "host":

        if args.target_score < 1:
            console.print("[red]Target score must be at least 1.[/red]")
            return 2
        
        server = TableServer(
            table_name=args.table,
            host_player_name=args.name,
            tcp_port=args.port,
            target_score=args.target_score,
        )
        try:
            server.start()
            console.print(
                f"[bold green]Share this table code:[/bold green] "
                f"[bold yellow]{server.code}[/bold yellow]"
            )
            console.print(
                "Remote players can run: "
                "[cyan]python -m flip7 join \"Their Name\"[/cyan]"
            )
            console.print(
                "[dim]LAN discovery uses UDP broadcast; the firewall may ask "
                "for permission the first time.[/dim]\n"
            )

            client = Client(
                "127.0.0.1",
                args.port,
                server.code,
                args.name,
                console=console,
                host_token=server.host_token,
            )
            client.connect()
            client.run()
            client.close()
        except KeyboardInterrupt:
            console.print("\n[yellow]Host stopped.[/yellow]")
        except OSError as exc:
            console.print(f"[red]Network error:[/red] {exc}")
            return 1
        finally:
            server.stop()

        return 0

    if args.command == "join":
        try:
            if args.host:
                if not args.code:
                    console.print(
                        "[red]Direct join requires --code.[/red]"
                    )
                    return 2
                table = {
                    "host_ip": args.host,
                    "tcp_port": args.port,
                    "code": args.code.upper(),
                    "table_name": "Flip 7 Table",
                }
            else:
                table = choose_table(console)
                if not table:
                    return 1

            client = Client(
                table["host_ip"],
                int(table["tcp_port"]),
                str(table["code"]).upper(),
                args.name,
                console=console,
            )
            try:
                client.connect()
                client.run()
            finally:
                client.close()

            return 0
        except KeyboardInterrupt:
            console.print("\n[yellow]Join cancelled.[/yellow]")
            return 0
        except OSError as exc:
            console.print(f"[red]Network error:[/red] {exc}")
            return 1

    return 2


if __name__ == "__main__":
    sys.exit(main())
