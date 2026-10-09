FLIP 7 GAME — QUICK START
=========================

REQUIREMENTS
------------
- Python 3.10+ recommended
- All players must install the project dependencies
- For LAN discovery: all players must be on the same Wi-Fi / personal hotspot
- Required Python packages:
  - rich
  - prompt-toolkit

INSTALL
-------
Open a terminal in the project root (the folder containing the "flip7" package and requirements.txt).

Install dependencies:

    python -m pip install -r requirements.txt


CLI
---

General help:

    python -m flip7 --help

Host help:

    python -m flip7 host --help

Join help:

    python -m flip7 join --help


HOST A TABLE
------------

Basic:

    python -m flip7 host "Host_Name"

Specify a table name:

    python -m flip7 host "Host_Name" --table "Table_Name"

Specify a TCP port:

    python -m flip7 host "Host_Name" --port 37071

Specify a custom winning target:

    python -m flip7 host "Host_Name" --target 300

Available host options:

    host NAME
        NAME
            Your player name. Required.

        --table TABLE_NAME
            Table name shown during LAN discovery.
            Default: Flip 7 Table

        --port PORT
            TCP server port.
            Default: 37071

        --target SCORE
        --target-score SCORE
            Points required to win the game.
            Default: 200.
            --target is the recommended spelling.


JOIN A TABLE
------------

Discover tables automatically:

    python -m flip7 join "Player_Name"

Direct connection by IP/hostname:

    python -m flip7 join "Player_Name" --host 192.168.1.25 --code ABC123

Direct connection with a custom port:

    python -m flip7 join "Player_Name" --host 192.168.1.25 --port 37080 --code ABC123

Available join options:

    join NAME
        NAME
            Your player name. Required.

        --host HOST
            Skip LAN discovery and connect directly to a host IP/hostname.

        --port PORT
            TCP port for a direct connection.
            Default: 37071

        --code CODE
            Table code for a direct connection.
            Required when --host is used.


TABLE DISCOVERY
---------------

Normal join uses UDP broadcast discovery.

Discovery port:

    UDP 37072

Game connection port:

    TCP 37071

If discovery does not find a table, use direct connection:

    python -m flip7 join "Player_Name" --host HOST_IP --port 37071 --code TABLE_CODE


QUICKSTART — HOST
-----------------

1. Install dependencies:

       python -m pip install -r requirements.txt

2. Start the table:

       python -m flip7 host "Host_Name"

3. Note the displayed table code.

4. Have at least 3 players join.

5. On the host client, enter:

       start


QUICKSTART — OTHER PLAYERS
--------------------------

1. Install dependencies:

       python -m pip install -r requirements.txt

2. Start the join client:

       python -m flip7 join "Player_Name"

3. Select the discovered table.

4. Enter the required target number when an action card asks for a target.


IN-GAME COMMANDS
----------------

Lobby (host):

    start
        Starts the game once 3–16 players are connected and the displayed
        roster is still current. If the roster changed, Start is rejected and
        the original lobby screen refreshes; review the players and enter Start again.

    r
    refresh
        Refresh the lobby roster from the server.

Lobby / any in-game player:

    q
    quit
        Leave the table. This works even when it is not your turn.

Normal turn (only when your choice prompt is active):

    h
    hit
        Draw another card.

    s
    stand
    stay
        End your current turn and bank your round score. Stay behaves exactly
        like Stand. A single 's' does not start a game in the lobby.

Action-card target:

    1
    2
    3
    ...
        Enter the displayed target number.

The valid target range depends on the players displayed by the server.


GAME LIMITS
-----------

Players per table:

    Minimum: 3
    Maximum: 16

Initial deck:

    3–8 players   -> 94 cards
    9–16 players  -> 188 cards

These player-count/deck-size settings are project house rules.

Deck refill:

    Any time the draw stack becomes empty:
    one fresh 94-card deck is created and shuffled.

This refill behavior is a project house rule; it intentionally does not
reconstruct the discard pile.


TIMEOUT SETTINGS
----------------

The server timeout constants are in server.py:

    ROOM_IDLE_TIMEOUT = 30 * 60
        Empty-room lifetime after the last player disconnects.
        Default: 30 minutes.

    PLAYER_ACTION_TIMEOUT = 5 * 60
        Maximum time for the player currently being asked to act.
        Applies to hit/stand and action-card target selection.
        Default: 5 minutes.

    TIMEOUT_CHECK_INTERVAL = 1
        How often timeout conditions are checked.
        Default: 1 second.

Example:

    PLAYER_ACTION_TIMEOUT = 2 * 60
    ROOM_IDLE_TIMEOUT = 60 * 60


TESTING
-------

Run the Python unit tests from the project root:

    python -m unittest discover -s tests -v

Local multiplayer test on one PC:

Terminal 1:

    python -m flip7 host "Host_Name"

Terminal 2:

    python -m flip7 join "Player_Name" --host 127.0.0.1 --code TABLE_CODE

Terminal 3:

    python -m flip7 join "Charlie" --host 127.0.0.1 --code TABLE_CODE

Replace TABLE_CODE with the code printed by the host.

Actual LAN test:

    python -m flip7 join "Player_Name"

Use discovery, or use the host's LAN IP for a direct connection.


WINDOWS FIREWALL
----------------

Host ports:

    TCP 37071
    UDP 37072

Allow Python through the Windows Firewall on Private networks.

For a custom TCP port, use the same port on the join command:

    python -m flip7 join "Player_Name" --host HOST_IP --port PORT --code TABLE_CODE


PROJECT ENTRY POINT
-------------------

Run the application with:

    python -m flip7

Subcommands:

    python -m flip7 host ...
    python -m flip7 join ...

