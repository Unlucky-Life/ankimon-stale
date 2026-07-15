"""Multiplayer window: raid boss lobby and friend battles.

Opened from the Ankimon menu. All server calls go through
MultiplayerController.run_action (background thread + main-thread callback),
so the dialog never freezes the UI. This is also where PvP moves are
committed — deliberately outside the reviewer.
"""

from typing import Optional

from aqt.utils import showInfo, tooltip
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QProgressBar,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
)

from .api_client import load_credentials
from . import get_controller

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
    _window.show()
    _window.raise_()
    _window.activateWindow()


class MultiplayerWindow(QDialog):
    def __init__(self, controller):
        super().__init__()
        self.controller = controller
        self.setWindowTitle("Ankimon Multiplayer")
        self.resize(460, 520)

        layout = QVBoxLayout(self)
        layout.addWidget(self._build_connection_group())

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_raid_tab(), "Raid Boss")
        self.tabs.addTab(self._build_pvp_tab(), "Friend Battles")
        layout.addWidget(self.tabs)

        refresh_button = QPushButton("Refresh")
        refresh_button.clicked.connect(self.refresh_from_server)
        layout.addWidget(refresh_button)

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
        credentials_row.addWidget(self.credentials_label)
        credentials_button = QPushButton("Set credentials…")
        credentials_button.clicked.connect(self._on_set_credentials)
        credentials_row.addWidget(credentials_button)
        layout.addLayout(credentials_row)

        self._update_credentials_label()
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

    def _on_set_credentials(self):
        # Same credentials file and dialog as the leaderboard: sign in once.
        from ..pyobj.ankimon_leaderboard import ApiKeyDialog

        dialog = ApiKeyDialog()
        dialog.exec()
        self.controller.reset_auth()
        self._update_credentials_label()

    # --- Raid tab -----------------------------------------------------------

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

        layout.addWidget(QLabel("Party contributions:"))
        self.raid_party_list = QListWidget()
        layout.addWidget(self.raid_party_list)

        join_row = QHBoxLayout()
        self.raid_code_input = QLineEdit()
        self.raid_code_input.setPlaceholderText("Raid code from a friend")
        join_row.addWidget(self.raid_code_input)
        join_button = QPushButton("Join raid")
        join_button.clicked.connect(self._on_join_raid)
        join_row.addWidget(join_button)
        layout.addLayout(join_row)

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
        self._run("Creating raid…", lambda: self.controller.api.create_raid())

    def _on_join_raid(self):
        code = self.raid_code_input.text().strip()
        if not code:
            tooltip("Enter a raid code first.")
            return
        self._run("Joining raid…", lambda: self.controller.api.join_raid(code))

    def _on_leave_raid(self):
        raid = self.controller.state.get("raid") or {}
        code = raid.get("code")
        if not code:
            tooltip("You are not in a raid.")
            return
        self._run("Leaving raid…", lambda: self.controller.api.leave_raid(code))

    # --- Friend battles tab -----------------------------------------------

    def _build_pvp_tab(self):
        tab = QGroupBox()
        layout = QVBoxLayout(tab)

        challenge_row = QHBoxLayout()
        self.challenge_input = QLineEdit()
        self.challenge_input.setPlaceholderText("Friend's username")
        challenge_row.addWidget(self.challenge_input)
        challenge_button = QPushButton("Challenge")
        challenge_button.clicked.connect(self._on_challenge)
        challenge_row.addWidget(challenge_button)
        layout.addLayout(challenge_row)

        self.tokens_label = QLabel("Turn tokens: 0 / 3")
        layout.addWidget(self.tokens_label)

        layout.addWidget(QLabel("Your battles:"))
        self.match_list = QListWidget()
        self.match_list.currentRowChanged.connect(lambda _row: self._update_turn_controls())
        layout.addWidget(self.match_list)

        turn_row = QHBoxLayout()
        turn_row.addWidget(QLabel("Move:"))
        self.move_combo = QComboBox()
        attacks = getattr(self.controller.main_pokemon, "attacks", None) or []
        self.move_combo.addItems([str(attack) for attack in attacks])
        turn_row.addWidget(self.move_combo, stretch=1)
        self.commit_button = QPushButton("Commit turn")
        self.commit_button.clicked.connect(self._on_commit_turn)
        turn_row.addWidget(self.commit_button)
        layout.addLayout(turn_row)

        respond_row = QHBoxLayout()
        self.accept_button = QPushButton("Accept challenge")
        self.accept_button.clicked.connect(lambda: self._on_respond(True))
        respond_row.addWidget(self.accept_button)
        self.decline_button = QPushButton("Decline")
        self.decline_button.clicked.connect(lambda: self._on_respond(False))
        respond_row.addWidget(self.decline_button)
        layout.addLayout(respond_row)

        return tab

    def _selected_match(self) -> Optional[dict]:
        item = self.match_list.currentItem()
        if item is None:
            return None
        return item.data(0x0100)  # Qt.ItemDataRole.UserRole

    def _on_challenge(self):
        opponent = self.challenge_input.text().strip()
        if not opponent:
            tooltip("Enter a username to challenge.")
            return
        self._run(
            f"Challenging {opponent}…",
            lambda: self.controller.api.challenge_friend(opponent),
        )

    def _on_respond(self, accept: bool):
        match = self._selected_match()
        if not match or not match.get("incoming_challenge"):
            tooltip("Select an incoming challenge first.")
            return
        self._run(
            "Sending response…",
            lambda: self.controller.api.respond_to_challenge(match["id"], accept),
        )

    def _on_commit_turn(self):
        match = self._selected_match()
        if not match or match.get("status") != "active":
            tooltip("Select an active battle first.")
            return
        if match.get("your_move_committed"):
            tooltip("You already committed a move this round.")
            return
        tokens = (self.controller.state.get("pvp") or {}).get("tokens", 0)
        if tokens < 1:
            tooltip("No turn tokens — answer more cards to charge one!")
            return
        move = self.move_combo.currentText()
        if not move:
            tooltip("Your Pokémon has no moves to use.")
            return
        self._run(
            f"Committing {move}…",
            lambda: self.controller.api.submit_turn(match["id"], move),
        )

    # --- Shared plumbing -----------------------------------------------------

    def _run(self, busy_message: str, task):
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
                showInfo(f"Multiplayer request failed:\n{error}")
                return
            self.refresh_from_state()

        self.controller.run_action(task, on_done)

    def refresh_from_server(self):
        self.controller.refresh_state(lambda _ok: self.refresh_from_state())

    def refresh_from_state(self):
        """Redraw both tabs from the controller's cached state."""
        state = self.controller.state
        raid = state.get("raid") or {}
        if raid.get("boss_max_hp"):
            pct = max(0, min(100, int(100 * raid.get("boss_hp", 0) / raid["boss_max_hp"])))
            self.raid_title.setText(f"{raid.get('boss_name', 'Raid boss')}")
            self.raid_bar.setValue(pct)
            info = f"Raid code: {raid.get('code', '?')}"
            if raid.get("ends_at"):
                info += f"  •  Ends: {raid['ends_at']}"
            if raid.get("your_damage_today") is not None:
                info += f"  •  Your damage today: {raid['your_damage_today']}"
            self.raid_info.setText(info)
        else:
            self.raid_title.setText("No active raid")
            self.raid_bar.setValue(0)
            self.raid_info.setText("Create a raid or join one with a friend's code.")

        self.raid_party_list.clear()
        for member in raid.get("party", []):
            self.raid_party_list.addItem(
                f"{member.get('username', '?')} — {member.get('damage', 0)} dmg"
            )

        pvp = state.get("pvp") or {}
        self.tokens_label.setText(f"Turn tokens: {pvp.get('tokens', 0)} / 3")

        self.match_list.clear()
        for match in pvp.get("matches", []):
            opponent = match.get("opponent", "?")
            status = match.get("status", "?")
            if match.get("incoming_challenge"):
                text = f"⚔ {opponent} challenged you!"
            elif status == "active":
                round_no = match.get("round", 1)
                you = "✓" if match.get("your_move_committed") else "…"
                them = "✓" if match.get("opponent_move_committed") else "…"
                text = f"{opponent} — round {round_no} (you {you} / them {them})"
            else:
                text = f"{opponent} — {status}"
            item = QListWidgetItem(text)
            item.setData(0x0100, match)
            self.match_list.addItem(item)

        self._update_turn_controls()

    def _update_turn_controls(self):
        match = self._selected_match()
        is_incoming = bool(match and match.get("incoming_challenge"))
        can_commit = bool(
            match
            and match.get("status") == "active"
            and not match.get("your_move_committed")
        )
        self.accept_button.setEnabled(is_incoming)
        self.decline_button.setEnabled(is_incoming)
        self.commit_button.setEnabled(can_commit)
