"""Multiplayer window: raid boss lobby, friends, and practice battles.

Opened from the Ankimon menu. All server calls go through
MultiplayerController.run_action (background thread + main-thread callback),
so the dialog never freezes the UI. Friend-battle moves come from the normal
reviewer battle loop.
"""

from typing import Optional

from aqt.utils import showInfo, tooltip
from PyQt6.QtCore import QByteArray, QSize
from PyQt6.QtGui import QGuiApplication
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from . import get_controller
from .api_client import MultiplayerConflictError, load_credentials

_window = None


def open_multiplayer_window():
    """Menu entry point; keeps a single window instance alive."""
    global _window
    controller = get_controller()
    if controller is None:
        showInfo("Multiplayer is not initialized. Please restart Anki.")
        return
    if _window is None:
        _window = MultiplayerWindow(controller)
    _window.refresh_from_state()
    _window.check_server_health()
    # Re-check the fit on every open: the window is a long-lived singleton,
    # and the screen it opens onto can change between two openings (an
    # external monitor unplugged, a resolution or scaling change).
    _window.fit_to_screen()
    _window.show()
    _window.raise_()
    _window.activateWindow()


# Preferred size when nothing has been remembered yet. Treated as a wish,
# not a demand: PREFERRED_SIZE is clamped to whatever the screen actually
# offers, so a 700px-tall window never opens taller than a short screen.
PREFERRED_SIZE = QSize(600, 700)

# The floor the dialog can be dragged down to. Everything above it scrolls,
# so this is about what stays usable, not about what fits.
MINIMUM_SIZE = QSize(420, 320)

# Kept clear of the screen edges so the title bar and the resize corner are
# always grabbable, whatever the taskbar is doing.
SCREEN_MARGIN = 80

GEOMETRY_SETTING = "multiplayer.window_geometry"


class MultiplayerWindow(QDialog):
    def __init__(self, controller):
        super().__init__()
        self.controller = controller
        self.setWindowTitle("Ankimon Multiplayer")

        # A grip in the corner: on some Linux window managers a QDialog is
        # otherwise awkward to grab by its edges.
        self.setSizeGripEnabled(True)
        self.setMinimumSize(MINIMUM_SIZE)

        # The three tabs and their group boxes have a tall combined minimum.
        # Putting the whole body in a scroll area means that minimum stops
        # being the dialog's minimum, which is what actually lets the window
        # be dragged smaller instead of springing back.
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.addWidget(self._build_connection_group())
        layout.addWidget(self._build_demo_group())

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_raid_tab(), "Raid Boss")
        self.tabs.addTab(self._build_friends_tab(), "Friends")
        self.tabs.addTab(self._build_pvp_tab(), "Battles")
        layout.addWidget(self.tabs)

        refresh_button = QPushButton("Refresh")
        refresh_button.clicked.connect(self.refresh_from_server)
        layout.addWidget(refresh_button)

        scroll = QScrollArea()
        scroll.setWidget(body)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)

        self._restore_geometry()

    # --- Sizing -----------------------------------------------------------

    def _available_size(self) -> QSize:
        """The usable area of the screen this window will appear on."""
        screen = self.screen() or QGuiApplication.primaryScreen()
        if screen is None:
            return PREFERRED_SIZE
        area = screen.availableGeometry()
        return QSize(
            max(MINIMUM_SIZE.width(), area.width() - SCREEN_MARGIN),
            max(MINIMUM_SIZE.height(), area.height() - SCREEN_MARGIN),
        )

    def _restore_geometry(self) -> None:
        """Reopen at the remembered size, or at a size the screen can hold.

        The remembered geometry is still clamped: it may have been saved on
        a larger monitor, or on a laptop that has since been undocked, and
        restoring it verbatim would put the window back off-screen — the
        one state a user cannot drag their way out of.
        """
        available = self._available_size()
        stored = self.controller.settings.get(GEOMETRY_SETTING, "")
        if stored:
            try:
                self.restoreGeometry(QByteArray.fromBase64(stored.encode("ascii")))
            except (ValueError, TypeError):
                pass

        size = self.size() if stored else PREFERRED_SIZE
        self.resize(
            min(size.width(), available.width()),
            min(size.height(), available.height()),
        )
        self._move_onto_screen()

    def _move_onto_screen(self) -> None:
        """Nudge the window fully inside its screen's usable area."""
        screen = self.screen() or QGuiApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        frame = self.frameGeometry()
        x = min(max(frame.x(), area.left()), max(area.left(), area.right() - frame.width()))
        y = min(max(frame.y(), area.top()), max(area.top(), area.bottom() - frame.height()))
        self.move(x, y)

    def fit_to_screen(self) -> None:
        """Shrink the window to the current screen and pull it into view."""
        available = self._available_size()
        self.resize(
            min(self.width(), available.width()),
            min(self.height(), available.height()),
        )
        self._move_onto_screen()

    def _save_geometry(self) -> None:
        try:
            encoded = bytes(self.saveGeometry().toBase64()).decode("ascii")
            self.controller.settings.set(GEOMETRY_SETTING, encoded)
        except Exception:
            # Remembering the window size is a convenience; never let it
            # stop the window from closing.
            pass

    def closeEvent(self, event):
        self._save_geometry()
        super().closeEvent(event)

    def reject(self):
        # Escape closes a QDialog through reject(), not closeEvent().
        self._save_geometry()
        super().reject()

    # --- Connection -------------------------------------------------------

    def _build_connection_group(self) -> QGroupBox:
        group = QGroupBox("Connection")
        layout = QVBoxLayout(group)

        self.enabled_checkbox = QCheckBox("Enable multiplayer")
        self.enabled_checkbox.setChecked(
            bool(self.controller.settings.get("multiplayer.enabled", False))
        )
        self.enabled_checkbox.toggled.connect(self._on_enabled_toggled)
        layout.addWidget(self.enabled_checkbox)

        status_row = QHBoxLayout()
        status_row.addWidget(QLabel("Status:"))
        self.health_label = QLabel("Checking...")
        status_row.addWidget(self.health_label, stretch=1)
        health_button = QPushButton("Check")
        health_button.clicked.connect(self.check_server_health)
        status_row.addWidget(health_button)
        layout.addLayout(status_row)

        url_row = QHBoxLayout()
        url_row.addWidget(QLabel("Server:"))
        self.url_input = QLineEdit(
            str(self.controller.settings.get("multiplayer.api_url", ""))
        )
        self.url_input.setPlaceholderText("https://multiplayer-api.ankimon.com")
        self.url_input.editingFinished.connect(self._on_url_changed)
        url_row.addWidget(self.url_input)
        layout.addLayout(url_row)

        credentials_row = QHBoxLayout()
        self.credentials_label = QLabel()
        credentials_row.addWidget(self.credentials_label, stretch=1)
        credentials_button = QPushButton("Set credentials")
        credentials_button.clicked.connect(self._on_set_credentials)
        credentials_row.addWidget(credentials_button)
        guest_button = QPushButton("Use guest")
        guest_button.clicked.connect(self._on_create_guest)
        credentials_row.addWidget(guest_button)
        layout.addLayout(credentials_row)

        self._update_credentials_label()
        return group

    def _build_demo_group(self) -> QGroupBox:
        group = QGroupBox("Test tools")
        layout = QVBoxLayout(group)

        self.demo_label = QLabel(
            "Use these to verify server connection, raids, and bot battles."
        )
        layout.addWidget(self.demo_label)

        buttons_row = QHBoxLayout()
        demo_button = QPushButton("Create test raid + bot")
        demo_button.clicked.connect(self._on_setup_demo)
        buttons_row.addWidget(demo_button)
        challenge_button = QPushButton("Challenge test bot (practice)")
        challenge_button.clicked.connect(self._on_challenge_test_bot)
        buttons_row.addWidget(challenge_button)
        layout.addLayout(buttons_row)

        return group

    def _update_credentials_label(self):
        credentials = load_credentials()
        if credentials:
            self.credentials_label.setText(f"Signed in as {credentials['username']}")
        else:
            self.credentials_label.setText("No credentials set")

    def _on_enabled_toggled(self, checked: bool):
        self.controller.settings.set("multiplayer.enabled", bool(checked))
        self.controller.reset_auth()

    def _on_url_changed(self):
        self.controller.settings.set("multiplayer.api_url", self.url_input.text().strip())
        self.controller.reset_auth()
        self.check_server_health()

    def _on_set_credentials(self):
        from ..pyobj.ankimon_leaderboard import ApiKeyDialog

        dialog = ApiKeyDialog()
        dialog.exec()
        self.controller.reset_auth()
        self._update_credentials_label()

    def check_server_health(self):
        self.health_label.setText("Checking...")

        def on_done(_result, error):
            if error is not None:
                self.health_label.setText("Offline")
                tooltip("Multiplayer server is offline or unreachable.")
                return
            self.health_label.setText("Online")

        self.controller.run_action(lambda: self.controller.api.check_health(), on_done)

    def _on_create_guest(self):
        tooltip("Creating guest...")

        def on_done(result, error):
            if error is not None:
                showInfo(f"Could not create guest:\n{error}")
                return
            state = (result or {}).get("state")
            if isinstance(state, dict):
                self.controller._apply_state(state)
            self.controller.reset_auth()
            self.controller.settings.set("multiplayer.enabled", True)
            self.enabled_checkbox.setChecked(True)
            self._update_credentials_label()
            self.refresh_from_state()
            tooltip("Guest ready.")

        self.controller.run_action(lambda: self.controller.api.create_guest(), on_done)

    def _on_setup_demo(self):
        self._run_demo_action(
            "Creating test raid and bot friend...",
            lambda: self.controller.api.setup_demo(),
        )

    def _on_challenge_test_bot(self):
        self._run(
            "Challenging test bot...",
            lambda: self.controller.api.challenge_friend(
                "bot:ankimon-test", self.controller.active_pokemon_payload()
            ),
        )

    # --- Raid tab ---------------------------------------------------------

    def _build_raid_tab(self):
        tab = QGroupBox()
        layout = QVBoxLayout(tab)

        self.raid_title = QLabel("No active raid")
        self.raid_title.setStyleSheet("font-weight: bold; font-size: 14px;")
        layout.addWidget(self.raid_title)

        self.raid_bar = QProgressBar()
        self.raid_bar.setRange(0, 100)
        self.raid_bar.setFormat("Boss HP: %p%")
        layout.addWidget(self.raid_bar)

        self.raid_info = QLabel("")
        layout.addWidget(self.raid_info)

        self.start_raid_button = QPushButton("Start raid (locks the room)")
        self.start_raid_button.setToolTip(
            "Once started, no one else can join this raid."
        )
        self.start_raid_button.clicked.connect(self._on_start_raid)
        self.start_raid_button.setVisible(False)
        layout.addWidget(self.start_raid_button)

        layout.addWidget(QLabel("Party contributions:"))
        self.raid_party_list = QListWidget()
        self.raid_party_list.setMaximumHeight(90)
        layout.addWidget(self.raid_party_list)

        layout.addWidget(QLabel("Open raid rooms:"))
        self.raid_room_list = QListWidget()
        layout.addWidget(self.raid_room_list)

        room_buttons_row = QHBoxLayout()
        join_selected_button = QPushButton("Join selected room")
        join_selected_button.clicked.connect(self._on_join_selected_room)
        room_buttons_row.addWidget(join_selected_button)
        layout.addLayout(room_buttons_row)

        join_row = QHBoxLayout()
        self.raid_code_input = QLineEdit()
        self.raid_code_input.setPlaceholderText("Raid code from a friend")
        join_row.addWidget(self.raid_code_input)
        join_button = QPushButton("Join by code")
        join_button.clicked.connect(self._on_join_raid)
        join_row.addWidget(join_button)
        layout.addLayout(join_row)

        create_row = QHBoxLayout()
        create_row.addWidget(QLabel("Visibility:"))
        self.raid_visibility_combo = QComboBox()
        self.raid_visibility_combo.addItems(["public", "friends", "code"])
        create_row.addWidget(self.raid_visibility_combo)
        create_row.addWidget(QLabel("Bots:"))
        self.raid_bots_spin = QSpinBox()
        self.raid_bots_spin.setRange(0, 5)
        create_row.addWidget(self.raid_bots_spin)
        layout.addLayout(create_row)

        buttons_row = QHBoxLayout()
        create_button = QPushButton("Create new raid")
        create_button.clicked.connect(self._on_create_raid)
        buttons_row.addWidget(create_button)
        leave_button = QPushButton("Leave raid")
        leave_button.clicked.connect(self._on_leave_raid)
        buttons_row.addWidget(leave_button)
        layout.addLayout(buttons_row)

        return tab

    def _on_create_raid(self):
        visibility = self.raid_visibility_combo.currentText()
        bots = self.raid_bots_spin.value()
        self._run(
            "Creating raid...",
            lambda: self.controller.api.create_raid(visibility=visibility, bots=bots),
        )

    def _on_join_raid(self):
        code = self.raid_code_input.text().strip()
        if not code:
            tooltip("Enter a raid code first.")
            return
        self._join_raid_code(code)

    def _selected_raid_room(self) -> Optional[dict]:
        item = self.raid_room_list.currentItem()
        if item is None:
            return None
        return item.data(0x0100)

    def _on_join_selected_room(self):
        room = self._selected_raid_room()
        if not room:
            tooltip("Select a raid room first.")
            return
        if room.get("locked"):
            showInfo("This raid has already started and can't be joined.")
            return
        code = room.get("code")
        if not code:
            tooltip("That room has no code.")
            return
        self._join_raid_code(code)

    def _join_raid_code(self, code: str):
        self._run(
            "Joining raid...",
            lambda: self.controller.api.join_raid(code),
            conflict_message="This raid has already started and can't be joined.",
        )

    def _on_leave_raid(self):
        raid = self.controller.state.get("raid") or {}
        code = raid.get("code")
        if not code:
            tooltip("You are not in a raid.")
            return
        self._run("Leaving raid...", lambda: self.controller.api.leave_raid(code))

    def _on_start_raid(self):
        raid = self.controller.state.get("raid") or {}
        code = raid.get("code")
        if not code:
            tooltip("You are not in a raid.")
            return
        self._run(
            "Starting raid... no one else will be able to join afterwards.",
            lambda: self.controller.api.start_raid(code),
        )

    # --- Friends tab --------------------------------------------------------

    def _build_friends_tab(self):
        tab = QGroupBox()
        layout = QVBoxLayout(tab)

        add_row = QHBoxLayout()
        self.add_friend_input = QLineEdit()
        self.add_friend_input.setPlaceholderText("Username to add")
        add_row.addWidget(self.add_friend_input)
        add_friend_button = QPushButton("Add friend")
        add_friend_button.clicked.connect(self._on_add_friend)
        add_row.addWidget(add_friend_button)
        layout.addLayout(add_row)

        layout.addWidget(QLabel("Friends:"))
        self.friend_list = QListWidget()
        layout.addWidget(self.friend_list, stretch=2)

        friend_buttons_row = QHBoxLayout()
        remove_friend_button = QPushButton("Remove selected friend")
        remove_friend_button.clicked.connect(self._on_remove_friend)
        friend_buttons_row.addWidget(remove_friend_button)
        layout.addLayout(friend_buttons_row)

        layout.addWidget(QLabel("Incoming requests:"))
        self.incoming_requests_list = QListWidget()
        self.incoming_requests_list.setMaximumHeight(80)
        layout.addWidget(self.incoming_requests_list)

        incoming_buttons_row = QHBoxLayout()
        accept_request_button = QPushButton("Accept")
        accept_request_button.clicked.connect(lambda: self._on_respond_friend_request(True))
        incoming_buttons_row.addWidget(accept_request_button)
        decline_request_button = QPushButton("Decline")
        decline_request_button.clicked.connect(lambda: self._on_respond_friend_request(False))
        incoming_buttons_row.addWidget(decline_request_button)
        layout.addLayout(incoming_buttons_row)

        layout.addWidget(QLabel("Outgoing requests (pending):"))
        self.outgoing_requests_list = QListWidget()
        self.outgoing_requests_list.setMaximumHeight(60)
        layout.addWidget(self.outgoing_requests_list)

        return tab

    def _friend_status_text(self, friend: dict) -> str:
        if friend.get("bot"):
            return "bot"
        if friend.get("reviewing_now"):
            return "reviewing"
        if friend.get("online"):
            return "online"
        last_seen = friend.get("last_seen")
        return f"offline - last seen {last_seen}" if last_seen else "offline"

    def _friend_row_text(self, friend: dict) -> str:
        name = friend.get("username", "?")
        parts = [name, f"({self._friend_status_text(friend)})"]
        if friend.get("in_raid"):
            parts.append("[in raid]")
        if friend.get("in_match"):
            parts.append("[in battle]")
        return " ".join(parts)

    def _selected_friend(self) -> Optional[dict]:
        item = self.friend_list.currentItem()
        if item is None:
            return None
        return item.data(0x0100)

    def _on_add_friend(self):
        username = self.add_friend_input.text().strip()
        if not username:
            tooltip("Enter a username first.")
            return
        self._run(
            f"Sending friend request to {username}...",
            lambda: self.controller.api.add_friend(username),
        )
        self.add_friend_input.clear()

    def _on_remove_friend(self):
        friend = self._selected_friend()
        if not friend:
            tooltip("Select a friend first.")
            return
        username = friend.get("raw_username") or friend.get("username")
        self._run(
            f"Removing {friend.get('username', username)}...",
            lambda: self.controller.api.remove_friend(username),
        )

    def _selected_incoming_request(self) -> Optional[dict]:
        item = self.incoming_requests_list.currentItem()
        if item is None:
            return None
        return item.data(0x0100)

    def _on_respond_friend_request(self, accept: bool):
        request = self._selected_incoming_request()
        if not request:
            tooltip("Select an incoming request first.")
            return
        username = request.get("username")
        self._run(
            "Sending response...",
            lambda: self.controller.api.respond_to_friend_request(username, accept),
        )

    # --- Battles tab (bot practice + gated human PvP) ----------------------

    def _build_pvp_tab(self):
        tab = QGroupBox()
        layout = QVBoxLayout(tab)

        self.pvp_gate_label = QLabel(
            "Player battles are coming soon. Bot practice battles are available now."
        )
        self.pvp_gate_label.setStyleSheet("color: #B9770E; font-style: italic;")
        self.pvp_gate_label.setWordWrap(True)
        layout.addWidget(self.pvp_gate_label)

        self.challenge_group = QGroupBox("Challenge a player")
        challenge_layout = QHBoxLayout(self.challenge_group)
        self.challenge_input = QLineEdit()
        self.challenge_input.setPlaceholderText("Friend's username")
        challenge_layout.addWidget(self.challenge_input)
        self.challenge_button = QPushButton("Challenge")
        self.challenge_button.clicked.connect(self._on_challenge)
        challenge_layout.addWidget(self.challenge_button)
        layout.addWidget(self.challenge_group)

        friend_row = QHBoxLayout()
        friend_row.addWidget(QLabel("Friends:"))
        self.pvp_friend_list = QListWidget()
        self.pvp_friend_list.setMaximumHeight(90)
        self.pvp_friend_list.currentRowChanged.connect(
            lambda _row: self._update_pvp_gate_controls()
        )
        friend_row.addWidget(self.pvp_friend_list, stretch=1)
        friend_buttons = QVBoxLayout()
        add_test_friend_button = QPushButton("Add test bot")
        add_test_friend_button.clicked.connect(self._on_add_test_bot_friend)
        friend_buttons.addWidget(add_test_friend_button)
        self.challenge_selected_button = QPushButton("Challenge selected (practice)")
        self.challenge_selected_button.clicked.connect(self._on_challenge_selected_friend)
        friend_buttons.addWidget(self.challenge_selected_button)
        friend_row.addLayout(friend_buttons)
        layout.addLayout(friend_row)

        self.tokens_label = QLabel("Turn tokens: 0 / 3")
        layout.addWidget(self.tokens_label)

        layout.addWidget(QLabel("Your battles:"))
        self.match_list = QListWidget()
        self.match_list.currentRowChanged.connect(lambda _row: self._update_turn_controls())
        layout.addWidget(self.match_list)

        reviewer_hint = QLabel(
            "Active battles continue in the reviewer. Answer cards to attack; "
            "a charged turn is submitted automatically using that attack."
        )
        reviewer_hint.setWordWrap(True)
        reviewer_hint.setStyleSheet("color: #5D6D7E; font-style: italic;")
        layout.addWidget(reviewer_hint)

        respond_row = QHBoxLayout()
        self.accept_button = QPushButton("Accept challenge")
        self.accept_button.clicked.connect(lambda: self._on_respond(True))
        respond_row.addWidget(self.accept_button)
        self.decline_button = QPushButton("Decline")
        self.decline_button.clicked.connect(lambda: self._on_respond(False))
        respond_row.addWidget(self.decline_button)
        layout.addLayout(respond_row)

        return tab

    def _human_pvp_enabled(self) -> bool:
        pvp = self.controller.state.get("pvp") or {}
        return bool(pvp.get("human_enabled"))

    def _selected_match(self) -> Optional[dict]:
        item = self.match_list.currentItem()
        if item is None:
            return None
        return item.data(0x0100)

    def _selected_pvp_friend(self) -> Optional[dict]:
        item = self.pvp_friend_list.currentItem()
        if item is None:
            return None
        return item.data(0x0100)

    def _on_add_test_bot_friend(self):
        self._run("Adding test bot friend...", lambda: self.controller.api.add_friend("bot"))

    def _on_challenge_selected_friend(self):
        friend = self._selected_pvp_friend()
        if not friend:
            tooltip("Select a friend first.")
            return
        if not friend.get("bot") and not self._human_pvp_enabled():
            showInfo("Player battles are coming soon - only bot friends can be challenged.")
            return
        opponent = friend.get("challenge_value") or friend.get("raw_username") or friend.get("username")
        if not opponent:
            tooltip("Selected friend cannot be challenged.")
            return
        self._run(
            f"Challenging {friend.get('username', opponent)}...",
            lambda: self.controller.api.challenge_friend(
                opponent, self.controller.active_pokemon_payload()
            ),
        )

    def _on_challenge(self):
        if not self._human_pvp_enabled():
            showInfo("Player battles are coming soon.")
            return
        opponent = self.challenge_input.text().strip()
        if not opponent:
            tooltip("Enter a username to challenge.")
            return
        self._run(
            f"Challenging {opponent}...",
            lambda: self.controller.api.challenge_friend(
                opponent, self.controller.active_pokemon_payload()
            ),
        )

    def _on_respond(self, accept: bool):
        match = self._selected_match()
        if not match or not match.get("incoming_challenge"):
            tooltip("Select an incoming challenge first.")
            return
        self._run(
            "Sending response...",
            lambda: self.controller.api.respond_to_challenge(
                match["id"], accept, self.controller.active_pokemon_payload()
            ),
        )

    # --- Shared plumbing --------------------------------------------------

    def _run(self, busy_message: str, task, conflict_message: Optional[str] = None):
        """Run an API action in the background and refresh on completion."""
        if not self.controller.enabled:
            showInfo(
                "Multiplayer is disabled or credentials are missing.\n"
                "Enable it above and set your username and API key."
            )
            return
        tooltip(busy_message)

        def on_done(_result, error):
            if error is not None:
                if isinstance(error, MultiplayerConflictError):
                    showInfo(conflict_message or "This action conflicts with the current server state.")
                else:
                    showInfo(f"Multiplayer request failed:\n{error}")
                return
            self.refresh_from_server()

        self.controller.run_action(task, on_done)

    def _run_demo_action(self, busy_message: str, task):
        if not self.controller.enabled:
            showInfo("Create a guest or set credentials first, then enable multiplayer.")
            return
        tooltip(busy_message)

        def on_done(result, error):
            if error is not None:
                showInfo(f"Multiplayer request failed:\n{error}")
                return
            state = (result or {}).get("state")
            if isinstance(state, dict):
                self.controller._apply_state(state)
            demo = (result or {}).get("demo") or {}
            if demo.get("raid_code"):
                tooltip(f"Test raid ready: {demo['raid_code']}")
            self.refresh_from_state()

        self.controller.run_action(task, on_done)

    def refresh_from_server(self):
        self.controller.refresh_state(lambda _ok: self.refresh_from_state())

    def refresh_from_state(self):
        """Redraw every tab from the controller's cached state."""
        state = self.controller.state
        raid = state.get("raid") or {}
        credentials = load_credentials() or {}
        if raid.get("boss_max_hp"):
            pct = max(0, min(100, int(100 * raid.get("boss_hp", 0) / raid["boss_max_hp"])))
            self.raid_title.setText(f"{raid.get('boss_name', 'Raid boss')}")
            self.raid_bar.setValue(pct)
            info = f"Raid code: {raid.get('code', '?')}"
            if raid.get("visibility"):
                info += f" - {raid['visibility']}"
            if raid.get("locked"):
                info += " - LOCKED"
            if raid.get("ends_at"):
                info += f" - Ends: {raid['ends_at']}"
            if raid.get("your_damage_today") is not None:
                info += f" - Your damage today: {raid['your_damage_today']}"
            self.raid_info.setText(info)

            is_owner = raid.get("owner") and raid.get("owner") == credentials.get("username")
            self.start_raid_button.setVisible(bool(is_owner and not raid.get("locked")))
        else:
            self.raid_title.setText("No active raid")
            self.raid_bar.setValue(0)
            self.raid_info.setText("Create a raid or join one with a friend's code.")
            self.start_raid_button.setVisible(False)

        self.raid_party_list.clear()
        for member in raid.get("party", []):
            suffix = " (bot)" if member.get("bot") else ""
            self.raid_party_list.addItem(
                f"{member.get('username', '?')}{suffix} - {member.get('damage', 0)} dmg"
            )

        self.raid_room_list.clear()
        for room in state.get("raid_rooms", []):
            boss = room.get("boss_name", "?")
            hp_pct = 0
            if room.get("boss_max_hp"):
                hp_pct = int(100 * room.get("boss_hp", 0) / room["boss_max_hp"])
            status = "LOCKED" if room.get("locked") else "open"
            joined = " [joined]" if room.get("joined") else ""
            text = (
                f"{boss} {hp_pct}% - {room.get('party_size', 0)} players - "
                f"{room.get('visibility', '?')} - owner {room.get('owner', '?')} - "
                f"{status}{joined}"
            )
            item = QListWidgetItem(text)
            item.setData(0x0100, room)
            self.raid_room_list.addItem(item)

        pvp = state.get("pvp") or {}
        self.tokens_label.setText(f"Turn tokens: {pvp.get('tokens', 0)} / 3")

        self.friend_list.clear()
        for friend in state.get("friends", []):
            item = QListWidgetItem(self._friend_row_text(friend))
            item.setData(0x0100, friend)
            self.friend_list.addItem(item)

        self.pvp_friend_list.clear()
        for friend in state.get("friends", []):
            name = friend.get("username", "?")
            suffix = " (bot)" if friend.get("bot") else ""
            item = QListWidgetItem(f"{name}{suffix}")
            item.setData(0x0100, friend)
            self.pvp_friend_list.addItem(item)

        friend_requests = state.get("friend_requests") or {}
        self.incoming_requests_list.clear()
        for request in friend_requests.get("incoming", []):
            item = QListWidgetItem(request.get("username", "?"))
            item.setData(0x0100, request)
            self.incoming_requests_list.addItem(item)

        self.outgoing_requests_list.clear()
        for request in friend_requests.get("outgoing", []):
            self.outgoing_requests_list.addItem(request.get("username", "?"))

        self.match_list.clear()
        for match in pvp.get("matches", []):
            opponent = match.get("opponent", "?")
            status = match.get("status", "?")
            if match.get("incoming_challenge"):
                text = f"{opponent} challenged you!"
            elif status == "active":
                round_no = match.get("round", 1)
                you = "yes" if match.get("your_move_committed") else "no"
                them = "yes" if match.get("opponent_move_committed") else "no"
                text = f"{opponent} - round {round_no} (you {you} / them {them})"
            else:
                text = f"{opponent} - {status}"
            item = QListWidgetItem(text)
            item.setData(0x0100, match)
            self.match_list.addItem(item)

        self._update_turn_controls()
        self._update_pvp_gate_controls()

    def _update_turn_controls(self):
        match = self._selected_match()
        is_incoming = bool(match and match.get("incoming_challenge"))
        self.accept_button.setEnabled(is_incoming)
        self.decline_button.setEnabled(is_incoming)

    def _update_pvp_gate_controls(self):
        human_enabled = self._human_pvp_enabled()
        self.pvp_gate_label.setVisible(not human_enabled)
        self.challenge_group.setVisible(human_enabled)

        friend = self._selected_pvp_friend()
        selected_is_bot = bool(friend and friend.get("bot"))
        self.challenge_selected_button.setEnabled(
            human_enabled or selected_is_bot or friend is None
        )
