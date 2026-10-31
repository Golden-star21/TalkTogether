
"""
TalkTogether 0.6.0 Beta
Dostopna aplikacija za račune, profile, prijatelje in zvočne klice.
"""

import os
import sys
import json
import urllib.request
import urllib.error
import asyncio
import queue
import threading
import time
from pathlib import Path

from dotenv import load_dotenv
from supabase import create_client

from PySide6.QtGui import QShortcut, QKeySequence
from PySide6.QtCore import QThread, Signal, QTimer, QUrl, Qt
from PySide6.QtMultimedia import QSoundEffect
from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QFormLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QMessageBox,
    QStackedWidget,
    QStatusBar,
    QListWidget,
)


APP_NAME = "TalkTogether"
VERSION = "0.6.0-beta.1"
APP_DIR = Path(__file__).resolve().parent


def load_app_environment():
    """Load the local .env from the app folder or its immediate parent.

    The beta can be installed in a new client/ subfolder while the existing
    working .env remains in the project folder. We search both locations and
    the current working directory, but never bundle or print secret values.
    """
    candidates = [
        APP_DIR / ".env",
        APP_DIR.parent / ".env",
        Path.cwd() / ".env",
    ]
    seen = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            resolved = candidate
        if resolved in seen:
            continue
        seen.add(resolved)
        if resolved.is_file():
            load_dotenv(dotenv_path=resolved, override=False)
            return resolved

    # Keep compatibility with environment variables supplied by Windows or
    # an existing run.bat environment when no local .env file is found.
    load_dotenv(override=False)
    return None


ENV_FILE_PATH = load_app_environment()

# The public server address is configured in client_config.json after hosting.
def load_client_server_url():
    config_path = APP_DIR / "client_config.json"
    configured_url = ""
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
        configured_url = str(config.get("server_url", "")).strip()
    except (OSError, ValueError, TypeError):
        pass
    return (
        os.getenv("TALKTTOGETHER_SERVER_URL", "").strip()
        or configured_url
        or "http://127.0.0.1:8000"
    ).rstrip("/")


CALL_SERVER_URL = load_client_server_url()



class WebSocketWorker(QThread):
    """Background WebSocket client with automatic reconnection."""

    event_received = Signal(dict)
    connection_status = Signal(str)

    def __init__(self, server_url, token_provider):
        super().__init__()
        self.server_url = server_url
        self.token_provider = token_provider
        self._stop_event = threading.Event()
        self._send_queue = queue.Queue()
        self._socket = None

    def send(self, payload):
        try:
            self._send_queue.put_nowait(dict(payload))
            return True
        except Exception:
            return False

    def stop(self):
        self._stop_event.set()

    def run(self):
        try:
            from websockets.sync.client import connect
        except Exception as error:
            self.connection_status.emit(
                "Manjka paket websockets: " + str(error)
            )
            return

        delay = 2
        while not self._stop_event.is_set():
            try:
                token = self.token_provider()
                if not token:
                    raise RuntimeError("Supabase prijavni žeton ni na voljo.")

                with connect(
                    self.server_url,
                    additional_headers={"Authorization": f"Bearer {token}"},
                    # The first connection may wake a sleeping beta server.
                    open_timeout=75,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=3,
                    proxy=None,
                ) as websocket:
                    self._socket = websocket
                    delay = 2
                    last_heartbeat = time.monotonic()
                    self.connection_status.emit("Povezava s strežnikom je aktivna.")

                    while not self._stop_event.is_set():
                        while True:
                            try:
                                payload = self._send_queue.get_nowait()
                            except queue.Empty:
                                break
                            websocket.send(json.dumps(payload, ensure_ascii=False))

                        # Keep an active beta server awake with app-level traffic.
                        now = time.monotonic()
                        if now - last_heartbeat >= 25:
                            websocket.send(json.dumps({"type": "ping"}))
                            last_heartbeat = now

                        try:
                            raw = websocket.recv(timeout=0.25)
                        except TimeoutError:
                            continue
                        except Exception:
                            raise

                        try:
                            event = json.loads(raw)
                            if isinstance(event, dict):
                                self.event_received.emit(event)
                        except (ValueError, TypeError):
                            continue

                self._socket = None
            except Exception as error:
                self._socket = None
                if not self._stop_event.is_set():
                    reason = f"{type(error).__name__}: {error}"
                    self.connection_status.emit(
                        "Povezava je prekinjena (" + reason[:180] + "). Ponovno povezovanje …"
                    )

            if not self._stop_event.is_set():
                self._stop_event.wait(delay)
                delay = min(delay * 2, 15)

        self.connection_status.emit("Povezava s strežnikom je zaprta.")


class LiveKitAudioWorker(QThread):
    """Connects to a private LiveKit room and publishes the microphone."""

    connected = Signal()
    failed = Signal(str)
    disconnected = Signal()

    def __init__(self, server_url, token):
        super().__init__()
        self.server_url = server_url
        self.token = token
        self._stop_event = threading.Event()
        self._mute_lock = threading.Lock()
        self._muted = False

    def stop(self):
        self._stop_event.set()

    def set_muted(self, muted):
        with self._mute_lock:
            self._muted = bool(muted)

    def run(self):
        try:
            asyncio.run(self._run_audio())
        except Exception as error:
            self.failed.emit(str(error))
        finally:
            self.disconnected.emit()

    async def _run_audio(self):
        from livekit import rtc

        room = rtc.Room()
        platform_audio = None
        audio_source = None
        microphone_track = None

        try:
            platform_audio = rtc.PlatformAudio()
            await room.connect(self.server_url, self.token)

            audio_source = platform_audio.create_audio_source(
                rtc.PlatformAudioOptions(
                    echo_cancellation=True,
                    noise_suppression=True,
                    auto_gain_control=True,
                )
            )
            microphone_track = rtc.LocalAudioTrack.create_audio_track(
                "mikrofon", audio_source
            )
            await room.local_participant.publish_track(microphone_track)
            self.connected.emit()

            last_muted = False
            while not self._stop_event.is_set():
                with self._mute_lock:
                    wanted_muted = self._muted

                if microphone_track is not None and wanted_muted != last_muted:
                    if wanted_muted:
                        microphone_track.mute()
                    else:
                        microphone_track.unmute()
                    last_muted = wanted_muted

                await asyncio.sleep(0.1)

        finally:
            try:
                await room.disconnect()
            except Exception:
                pass
            if audio_source is not None:
                try:
                    audio_source.close()
                except Exception:
                    pass
            if platform_audio is not None:
                try:
                    platform_audio.close()
                except Exception:
                    pass


class TalkTogetherWindow(QMainWindow):

    def __init__(self):
        super().__init__()

        self.setWindowTitle(
            f"{APP_NAME} - različica {VERSION}"
        )
        self.resize(760, 620)
        self.setMinimumSize(560, 460)

        self.supabase_url = os.getenv(
            "SUPABASE_URL", ""
        ).strip()

        self.supabase_key = os.getenv(
            "SUPABASE_PUBLISHABLE_KEY", ""
        ).strip()

        self.supabase = None
        self.current_user = None
        self.profile = None

        self.search_results = []
        self.requests_data = []
        self.friends_data = []
        self.websocket_worker = None
        self.rtc_worker = None
        self.current_call = None
        self.call_started_at = None
        self.microphone_muted = False
        self.call_timer = QTimer(self)
        self.call_timer.setInterval(1000)
        self.call_timer.timeout.connect(self.update_call_duration)

        self.pages = QStackedWidget()
        self.setCentralWidget(self.pages)

        self.login_page = self.create_login_page()
        self.register_page = self.create_register_page()
        self.home_page = self.create_home_page()
        self.profile_page = self.create_profile_page()
        self.search_page = self.create_search_page()
        self.requests_page = self.create_requests_page()
        self.friends_page = self.create_friends_page()
        self.call_page = self.create_call_page()

        self.pages.addWidget(self.login_page)
        self.pages.addWidget(self.register_page)
        self.pages.addWidget(self.home_page)
        self.pages.addWidget(self.profile_page)
        self.pages.addWidget(self.search_page)
        self.pages.addWidget(self.requests_page)
        self.pages.addWidget(self.friends_page)
        self.pages.addWidget(self.call_page)

        self.setStatusBar(QStatusBar())

        self.setup_call_sounds()
        self.connect_supabase()

        self.pages.setCurrentWidget(self.login_page)

        self.apply_style()
        self.install_shortcuts()

        self.email_input.setFocus()

    # --------------------------------------------------
    # POVEZAVA S SUPABASE
    # --------------------------------------------------

    def connect_supabase(self):

        if not self.supabase_url or not self.supabase_key:
            env_location = str(ENV_FILE_PATH) if ENV_FILE_PATH else "Datoteke .env nisem našel."
            QMessageBox.warning(
                self,
                "Nastavitve Supabase manjkajo",
                "TalkTogether ni našel obeh potrebnih nastavitev.\n\n"
                "Potrebni sta spremenljivki SUPABASE_URL in "
                "SUPABASE_PUBLISHABLE_KEY.\n\n"
                f"Datoteka .env: {env_location}\n\n"
                "Kopiraj svojo delujočo datoteko .env iz različice 0.5.0 "
                "v isto mapo, kjer sta main.py in run.bat. "
                "Ne pošiljaj datoteke .env drugim in je ne nalagaj na GitHub."
            )
            return

        try:
            self.supabase = create_client(
                self.supabase_url,
                self.supabase_key
            )

            self.statusBar().showMessage(
                "Povezava s Supabase je pripravljena."
            )

        except Exception as error:
            QMessageBox.critical(
                self,
                "Napaka povezave",
                str(error)
            )

    def make_heading(self, text):

        heading = QLabel(text)
        heading.setStyleSheet(
            "font-size: 18pt; font-weight: bold;"
        )
        return heading

    # --------------------------------------------------
    # STRAN ZA PRIJAVO
    # --------------------------------------------------

    def create_login_page(self):

        page = QWidget()
        layout = QVBoxLayout(page)

        layout.addWidget(
            self.make_heading("Prijava v TalkTogether")
        )

        layout.addWidget(
            QLabel("Vpiši svoje podatke za prijavo.")
        )

        form = QFormLayout()

        self.email_input = QLineEdit()
        self.email_input.setAccessibleName(
            "E-poštni naslov"
        )
        self.email_input.setPlaceholderText(
            "ime@example.com"
        )

        form.addRow(
            "E-poštni naslov:",
            self.email_input
        )

        self.password_input = QLineEdit()
        self.password_input.setAccessibleName("Geslo")
        self.password_input.setEchoMode(
            QLineEdit.EchoMode.Password
        )

        form.addRow(
            "Geslo:",
            self.password_input
        )

        layout.addLayout(form)

        login_button = QPushButton("Prijava")
        login_button.clicked.connect(self.login)

        self.password_input.returnPressed.connect(
            self.login
        )

        layout.addWidget(login_button)

        register_button = QPushButton(
            "Ustvari nov račun"
        )
        register_button.clicked.connect(
            self.show_register
        )

        layout.addWidget(register_button)
        layout.addStretch()

        return page

    # --------------------------------------------------
    # REGISTRACIJA
    # --------------------------------------------------

    def create_register_page(self):

        page = QWidget()
        layout = QVBoxLayout(page)

        layout.addWidget(
            self.make_heading("Registracija v TalkTogether")
        )

        form = QFormLayout()

        self.register_email = QLineEdit()
        self.register_email.setAccessibleName(
            "E-poštni naslov za registracijo"
        )

        form.addRow(
            "E-pošta:",
            self.register_email
        )

        self.register_password = QLineEdit()
        self.register_password.setAccessibleName(
            "Novo geslo"
        )
        self.register_password.setEchoMode(
            QLineEdit.EchoMode.Password
        )

        form.addRow(
            "Geslo:",
            self.register_password
        )

        self.register_password_again = QLineEdit()
        self.register_password_again.setAccessibleName(
            "Ponovi geslo"
        )
        self.register_password_again.setEchoMode(
            QLineEdit.EchoMode.Password
        )

        form.addRow(
            "Ponovi geslo:",
            self.register_password_again
        )

        layout.addLayout(form)

        register_button = QPushButton("Registriraj se")
        register_button.clicked.connect(self.register)

        self.register_password_again.returnPressed.connect(
            self.register
        )

        layout.addWidget(register_button)

        back_button = QPushButton("Nazaj na prijavo")
        back_button.clicked.connect(self.show_login)

        layout.addWidget(back_button)
        layout.addStretch()

        return page

    # --------------------------------------------------
    # GLAVNI ZASLON
    # --------------------------------------------------

    def create_home_page(self):

        page = QWidget()
        layout = QVBoxLayout(page)

        layout.addWidget(
            self.make_heading("Dobrodošel v TalkTogether!")
        )

        self.account_label = QLabel("")
        self.account_label.setAccessibleName(
            "Prijavljeni uporabnik"
        )
        layout.addWidget(self.account_label)

        self.home_profile_label = QLabel("")
        self.home_profile_label.setAccessibleName(
            "Podatki o profilu"
        )
        layout.addWidget(self.home_profile_label)

        profile_button = QPushButton(
            "Moj profil in uporabniško ime"
        )
        profile_button.clicked.connect(self.show_profile)
        layout.addWidget(profile_button)

        search_button = QPushButton(
            "Poišči uporabnike in dodaj prijatelje"
        )
        search_button.clicked.connect(self.show_search)
        layout.addWidget(search_button)

        requests_button = QPushButton(
            "Prošnje za prijateljstvo"
        )
        requests_button.clicked.connect(self.show_requests)
        layout.addWidget(requests_button)

        friends_button = QPushButton("Moji prijatelji")
        friends_button.clicked.connect(self.show_friends)
        layout.addWidget(friends_button)

        logout_button = QPushButton("Odjava")
        logout_button.clicked.connect(self.logout)
        layout.addWidget(logout_button)

        layout.addStretch()

        return page

    # --------------------------------------------------
    # PROFIL
    # --------------------------------------------------

    def create_profile_page(self):

        page = QWidget()
        layout = QVBoxLayout(page)

        layout.addWidget(
            self.make_heading("Moj uporabniški profil")
        )

        layout.addWidget(
            QLabel(
                "Uporabniško ime mora imeti od 3 do 24 znakov. "
                "Dovoljene so črke, številke in podčrtaj."
            )
        )

        form = QFormLayout()

        self.username_input = QLineEdit()
        self.username_input.setAccessibleName(
            "Uporabniško ime"
        )
        self.username_input.setPlaceholderText(
            "Na primer florijan123"
        )

        form.addRow(
            "Uporabniško ime:",
            self.username_input
        )

        self.display_name_input = QLineEdit()
        self.display_name_input.setAccessibleName(
            "Prikazno ime"
        )
        self.display_name_input.setPlaceholderText(
            "Ime, ki ga vidijo drugi"
        )

        form.addRow(
            "Prikazno ime:",
            self.display_name_input
        )

        layout.addLayout(form)

        save_button = QPushButton("Shrani profil")
        save_button.clicked.connect(self.save_profile)
        layout.addWidget(save_button)

        back_button = QPushButton(
            "Nazaj na glavni zaslon"
        )
        back_button.clicked.connect(self.show_home)
        layout.addWidget(back_button)

        layout.addStretch()

        return page

    # --------------------------------------------------
    # ISKANJE UPORABNIKOV
    # --------------------------------------------------

    def create_search_page(self):

        page = QWidget()
        layout = QVBoxLayout(page)

        layout.addWidget(
            self.make_heading("Iskanje uporabnikov")
        )

        layout.addWidget(
            QLabel(
                "Vpiši uporabniško ime ali celoten "
                "e-poštni naslov uporabnika."
            )
        )

        self.search_input = QLineEdit()
        self.search_input.setAccessibleName(
            "Iskalni niz, uporabniško ime ali e-pošta"
        )
        self.search_input.setPlaceholderText(
            "Uporabniško ime ali e-pošta"
        )
        self.search_input.returnPressed.connect(
            self.search_users
        )
        layout.addWidget(self.search_input)

        search_button = QPushButton("Išči")
        search_button.clicked.connect(self.search_users)
        layout.addWidget(search_button)

        self.search_list = QListWidget()
        self.search_list.setAccessibleName(
            "Rezultati iskanja uporabnikov"
        )
        layout.addWidget(self.search_list)

        send_button = QPushButton(
            "Pošlji prošnjo izbranemu uporabniku"
        )
        send_button.clicked.connect(
            self.send_friend_request
        )
        layout.addWidget(send_button)

        back_button = QPushButton(
            "Nazaj na glavni zaslon"
        )
        back_button.clicked.connect(self.show_home)
        layout.addWidget(back_button)

        return page

    # --------------------------------------------------
    # PREJETE PROŠNJE
    # --------------------------------------------------

    def create_requests_page(self):

        page = QWidget()
        layout = QVBoxLayout(page)

        layout.addWidget(
            self.make_heading("Prošnje za prijateljstvo")
        )

        self.requests_list = QListWidget()
        self.requests_list.setAccessibleName(
            "Prejete prošnje za prijateljstvo"
        )
        layout.addWidget(self.requests_list)

        accept_button = QPushButton("Sprejmi prošnjo")
        accept_button.clicked.connect(self.accept_request)
        layout.addWidget(accept_button)

        reject_button = QPushButton("Zavrni prošnjo")
        reject_button.clicked.connect(self.reject_request)
        layout.addWidget(reject_button)

        refresh_button = QPushButton("Osveži prošnje")
        refresh_button.clicked.connect(self.load_requests)
        layout.addWidget(refresh_button)

        back_button = QPushButton(
            "Nazaj na glavni zaslon"
        )
        back_button.clicked.connect(self.show_home)
        layout.addWidget(back_button)

        return page

    # --------------------------------------------------
    # SEZNAM PRIJATELJEV
    # --------------------------------------------------

    def create_friends_page(self):

        page = QWidget()
        layout = QVBoxLayout(page)

        layout.addWidget(
            self.make_heading("Moji prijatelji")
        )

        self.friends_list = QListWidget()
        self.friends_list.setAccessibleName(
            "Seznam prijateljev"
        )
        layout.addWidget(self.friends_list)

        call_button = QPushButton("Pokliči izbranega prijatelja")
        call_button.clicked.connect(self.start_outgoing_call)
        layout.addWidget(call_button)

        refresh_button = QPushButton(
            "Osveži seznam prijateljev"
        )
        refresh_button.clicked.connect(self.load_friends)
        layout.addWidget(refresh_button)

        back_button = QPushButton(
            "Nazaj na glavni zaslon"
        )
        back_button.clicked.connect(self.show_home)
        layout.addWidget(back_button)

        return page


    # --------------------------------------------------
    # ZASLON IN UPRAVLJANJE ZVOČNIH KLICEV
    # --------------------------------------------------

    def create_call_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(self.make_heading("Zvočni klic"))

        self.call_friend_label = QLabel("Ni aktivnega klica.")
        self.call_friend_label.setAccessibleName("Oseba v klicu")
        layout.addWidget(self.call_friend_label)

        self.call_status_label = QLabel("Stanje: pripravljeno")
        self.call_status_label.setAccessibleName("Stanje klica")
        self.call_status_label.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        layout.addWidget(self.call_status_label)

        self.call_duration_label = QLabel("Trajanje: 00:00")
        self.call_duration_label.setAccessibleName("Trajanje klica")
        layout.addWidget(self.call_duration_label)

        self.accept_call_button = QPushButton("Sprejmi klic")
        self.accept_call_button.clicked.connect(self.accept_incoming_call)
        self.accept_call_button.setVisible(False)
        layout.addWidget(self.accept_call_button)

        self.reject_call_button = QPushButton("Zavrni klic")
        self.reject_call_button.clicked.connect(self.reject_incoming_call)
        self.reject_call_button.setVisible(False)
        layout.addWidget(self.reject_call_button)

        self.mute_call_button = QPushButton("Izklopi mikrofon")
        self.mute_call_button.clicked.connect(self.toggle_microphone)
        self.mute_call_button.setEnabled(False)
        layout.addWidget(self.mute_call_button)

        self.end_call_button = QPushButton("Končaj klic")
        self.end_call_button.clicked.connect(self.hang_up_call)
        self.end_call_button.setVisible(False)
        layout.addWidget(self.end_call_button)

        back_button = QPushButton("Nazaj na glavni zaslon")
        back_button.clicked.connect(self.show_home)
        layout.addWidget(back_button)
        layout.addStretch()
        return page

    def setup_call_sounds(self):
        sound_folder = Path(__file__).resolve().parent / "sounds" / "calls"
        self.outgoing_call_sound = QSoundEffect(self)
        self.incoming_call_sound = QSoundEffect(self)

        outgoing_path = sound_folder / "COMTelph_Dial phone (ID 0002)_BigSoundBank.com.wav"
        incoming_path = sound_folder / "COMTelph_Fixed telephone ring 4 (ID 0253)_BigSoundBank.com.wav"

        for sound, sound_path in (
            (self.outgoing_call_sound, outgoing_path),
            (self.incoming_call_sound, incoming_path),
        ):
            sound.setVolume(0.65)
            sound.setLoopCount(-2)  # Qt: -2 pomeni neskončno ponavljanje.
            if sound_path.is_file():
                sound.setSource(QUrl.fromLocalFile(str(sound_path.resolve())))

    def stop_call_sounds(self):
        self.outgoing_call_sound.stop()
        self.incoming_call_sound.stop()

    def play_outgoing_call_sound(self):
        self.incoming_call_sound.stop()
        if self.outgoing_call_sound.source().isValid():
            self.outgoing_call_sound.play()

    def play_incoming_call_sound(self):
        self.outgoing_call_sound.stop()
        if self.incoming_call_sound.source().isValid():
            self.incoming_call_sound.play()

    def show_call_page(self):
        self.pages.setCurrentWidget(self.call_page)
        self.call_status_label.setFocus()

    def start_websocket(self):
        if self.websocket_worker is not None and self.websocket_worker.isRunning():
            return

        if not self.get_access_token():
            self.statusBar().showMessage(
                "Prijavni žeton ni na voljo; klicni strežnik ni povezan."
            )
            return

        websocket_url = CALL_SERVER_URL.rstrip("/")
        if websocket_url.startswith("https://"):
            websocket_url = "wss://" + websocket_url[len("https://"):]
        elif websocket_url.startswith("http://"):
            websocket_url = "ws://" + websocket_url[len("http://"):]
        websocket_url += "/ws"

        self.websocket_worker = WebSocketWorker(
            websocket_url,
            self.get_access_token,
        )
        self.websocket_worker.event_received.connect(self.handle_websocket_event)
        self.websocket_worker.connection_status.connect(self.on_websocket_status)
        self.websocket_worker.start()

    def stop_websocket(self):
        worker = self.websocket_worker
        self.websocket_worker = None
        if worker is not None:
            worker.stop()
            worker.wait(3000)

    def on_websocket_status(self, status):
        self.statusBar().showMessage(status)

    def send_websocket_message(self, payload):
        worker = self.websocket_worker
        if worker is None or not worker.isRunning():
            QMessageBox.warning(
                self,
                "Strežnik ni povezan",
                "TalkTogether trenutno ni povezan s strežnikom za klice. "
                "Preveri, ali strežnik deluje, in poskusi znova."
            )
            return False
        return worker.send(payload)

    def start_outgoing_call(self):
        index = self.friends_list.currentRow()
        if index < 0 or index >= len(self.friends_data):
            QMessageBox.information(
                self,
                "Izberi prijatelja",
                "Najprej izberi prijatelja s seznama."
            )
            return
        if self.current_call is not None:
            QMessageBox.information(self, "Klic", "Klic je že v teku.")
            return

        friend = self.friends_data[index]
        self.current_call = {
            "direction": "outgoing",
            "friend_id": friend["user_id"],
            "friend_name": friend["display_name"],
            "call_id": None,
        }
        self.call_friend_label.setText("Kličeš: " + friend["display_name"])
        self.call_status_label.setText("Stanje: klicanje …")
        self.call_duration_label.setText("Trajanje: 00:00")
        self.accept_call_button.setVisible(False)
        self.reject_call_button.setVisible(False)
        self.end_call_button.setVisible(True)
        self.mute_call_button.setEnabled(False)
        self.play_outgoing_call_sound()
        self.show_call_page()

        if not self.send_websocket_message({
            "type": "call",
            "to_user_id": friend["user_id"],
        }):
            self.finish_call_ui("Klica ni bilo mogoče poslati.")

    def accept_incoming_call(self):
        if not self.current_call or self.current_call.get("direction") != "incoming":
            return
        call_id = self.current_call.get("call_id")
        if call_id:
            self.send_websocket_message({"type": "accept", "call_id": call_id})
            self.call_status_label.setText("Stanje: sprejemanje klica …")
            self.accept_call_button.setEnabled(False)
            self.reject_call_button.setEnabled(False)

    def reject_incoming_call(self):
        if not self.current_call:
            return
        call_id = self.current_call.get("call_id")
        if call_id:
            self.send_websocket_message({"type": "reject", "call_id": call_id})
        self.finish_call_ui("Klic je bil zavrnjen.")

    def hang_up_call(self):
        if self.current_call:
            call_id = self.current_call.get("call_id")
            if call_id:
                self.send_websocket_message({"type": "hangup", "call_id": call_id})
        self.finish_call_ui("Klic je končan.")

    def toggle_microphone(self):
        if self.rtc_worker is None or not self.rtc_worker.isRunning():
            return
        self.microphone_muted = not self.microphone_muted
        self.rtc_worker.set_muted(self.microphone_muted)
        if self.microphone_muted:
            self.mute_call_button.setText("Vklopi mikrofon")
            self.call_status_label.setText("Stanje: povezano; mikrofon je izklopljen.")
        else:
            self.mute_call_button.setText("Izklopi mikrofon")
            self.call_status_label.setText("Stanje: povezano.")

    def start_livekit_audio(self, event):
        self.stop_call_sounds()
        self.call_status_label.setText("Stanje: povezovanje z zvočno sobo …")
        self.accept_call_button.setVisible(False)
        self.reject_call_button.setVisible(False)
        self.end_call_button.setVisible(True)
        self.mute_call_button.setEnabled(False)
        self.microphone_muted = False
        self.mute_call_button.setText("Izklopi mikrofon")

        if self.rtc_worker is not None:
            self.rtc_worker.stop()
            self.rtc_worker.wait(1500)

        self.rtc_worker = LiveKitAudioWorker(
            event.get("server_url", ""),
            event.get("token", ""),
        )
        self.rtc_worker.connected.connect(self.on_livekit_connected)
        self.rtc_worker.failed.connect(self.on_livekit_failed)
        self.rtc_worker.start()

    def on_livekit_connected(self):
        self.call_status_label.setText("Stanje: povezano.")
        self.mute_call_button.setEnabled(True)
        self.call_started_at = time.monotonic()
        self.call_duration_label.setText("Trajanje: 00:00")
        self.call_timer.start()
        self.statusBar().showMessage("Zvočni klic je povezan.")

    def on_livekit_failed(self, message):
        self.statusBar().showMessage("Napaka zvočnega klica: " + message)
        if self.current_call and self.current_call.get("call_id"):
            self.send_websocket_message({
                "type": "hangup",
                "call_id": self.current_call["call_id"],
            })
        self.finish_call_ui("Povezave za zvočni klic ni bilo mogoče vzpostaviti.")

    def update_call_duration(self):
        if self.call_started_at is None:
            return
        seconds = max(0, int(time.monotonic() - self.call_started_at))
        minutes, seconds = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)
        shown = f"{hours:02d}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:02d}:{seconds:02d}"
        self.call_duration_label.setText("Trajanje: " + shown)

    def finish_call_ui(self, message):
        self.call_timer.stop()
        self.call_started_at = None
        self.stop_call_sounds()

        worker = self.rtc_worker
        self.rtc_worker = None
        if worker is not None:
            worker.stop()
            worker.wait(3000)

        self.current_call = None
        self.accept_call_button.setVisible(False)
        self.reject_call_button.setVisible(False)
        self.accept_call_button.setEnabled(True)
        self.reject_call_button.setEnabled(True)
        self.end_call_button.setVisible(False)
        self.mute_call_button.setEnabled(False)
        self.mute_call_button.setText("Izklopi mikrofon")
        self.call_duration_label.setText("Trajanje: 00:00")
        self.call_status_label.setText("Stanje: " + message)
        self.statusBar().showMessage(message)

    def handle_websocket_event(self, event):
        event_type = event.get("type", "")

        if event_type == "connected":
            self.statusBar().showMessage("Povezava s strežnikom za klice je aktivna.")
            return

        if event_type == "incoming_call":
            if self.current_call is not None:
                self.send_websocket_message({
                    "type": "reject",
                    "call_id": event.get("call_id", ""),
                })
                return
            friend_name = event.get("caller_name") or "Prijatelj"
            self.current_call = {
                "direction": "incoming",
                "friend_name": friend_name,
                "call_id": event.get("call_id"),
            }
            self.call_friend_label.setText("Dohodni klic: " + friend_name)
            self.call_status_label.setText("Stanje: telefon zvoni.")
            self.call_duration_label.setText("Trajanje: 00:00")
            self.accept_call_button.setVisible(True)
            self.reject_call_button.setVisible(True)
            self.accept_call_button.setEnabled(True)
            self.reject_call_button.setEnabled(True)
            self.end_call_button.setVisible(False)
            self.mute_call_button.setEnabled(False)
            self.play_incoming_call_sound()
            self.show_call_page()
            return

        if event_type == "outgoing_ringing":
            if self.current_call is not None:
                self.current_call["call_id"] = event.get("call_id")
            self.call_status_label.setText("Stanje: čakam, da prijatelj sprejme klic …")
            return

        if event_type == "call_accepted":
            if self.current_call is None:
                return
            self.current_call["call_id"] = event.get("call_id")
            self.current_call["friend_name"] = event.get("other_name") or self.current_call.get("friend_name", "Prijatelj")
            self.call_friend_label.setText("Klic z: " + self.current_call["friend_name"])
            self.start_livekit_audio(event)
            return

        if event_type == "call_rejected":
            self.finish_call_ui("Prijatelj je zavrnil klic.")
            return

        if event_type in ("call_timeout", "call_unavailable", "call_error", "call_ended"):
            self.finish_call_ui(event.get("message") or "Klic je končan.")
            return

        if event_type == "error":
            QMessageBox.warning(
                self,
                "Klicna napaka",
                event.get("message") or "Zahteve ni bilo mogoče izvesti."
            )

    def closeEvent(self, event):
        self.stop_call_sounds()
        self.call_timer.stop()
        if self.current_call and self.current_call.get("call_id"):
            worker = self.websocket_worker
            if worker is not None:
                worker.send({"type": "hangup", "call_id": self.current_call["call_id"]})
        if self.rtc_worker is not None:
            self.rtc_worker.stop()
            self.rtc_worker.wait(3000)
            self.rtc_worker = None
        self.stop_websocket()
        event.accept()


    def install_shortcuts(self):

        shortcut = QShortcut(
            QKeySequence("Alt+Left"),
            self
        )
        shortcut.activated.connect(self.show_home)

    def apply_style(self):

        self.setStyleSheet("""
            QWidget {
                font-size: 12pt;
            }

            QLineEdit {
                padding: 8px;
                min-height: 26px;
            }

            QPushButton {
                padding: 9px;
                min-height: 28px;
            }

            QListWidget {
                padding: 5px;
                min-height: 140px;
            }

            QPushButton:focus,
            QLineEdit:focus,
            QListWidget:focus {
                border: 2px solid #005fcc;
            }
        """)

    # --------------------------------------------------
    # PREKLOP STRANI
    # --------------------------------------------------

    def show_register(self):

        self.register_email.setText(
            self.email_input.text()
        )

        self.pages.setCurrentWidget(
            self.register_page
        )
        self.register_email.setFocus()

    def show_login(self):

        self.pages.setCurrentWidget(
            self.login_page
        )
        self.email_input.setFocus()

    def show_home(self):

        if not self.current_user:
            return

        username = (
            (self.profile or {}).get("username")
            or "še ni nastavljeno"
        )

        self.home_profile_label.setText(
            f"Uporabniško ime: {username}"
        )

        self.pages.setCurrentWidget(
            self.home_page
        )

    def show_profile(self):

        self.username_input.setText(
            (self.profile or {}).get("username") or ""
        )

        self.display_name_input.setText(
            (self.profile or {}).get("display_name") or ""
        )

        self.pages.setCurrentWidget(
            self.profile_page
        )
        self.username_input.setFocus()

    def show_search(self):

        self.search_input.clear()
        self.search_list.clear()
        self.search_results = []

        self.pages.setCurrentWidget(
            self.search_page
        )
        self.search_input.setFocus()

    def show_requests(self):

        self.pages.setCurrentWidget(
            self.requests_page
        )
        self.load_requests()

    def show_friends(self):

        self.pages.setCurrentWidget(
            self.friends_page
        )
        self.load_friends()

    # --------------------------------------------------
    # REGISTRACIJA
    # --------------------------------------------------

    def register(self):

        if self.supabase is None:
            QMessageBox.warning(
                self,
                "Napaka",
                "Povezava s Supabase ni nastavljena."
            )
            return

        email = self.register_email.text().strip()
        password = self.register_password.text()
        password_again = self.register_password_again.text()

        if not email or not password or not password_again:
            QMessageBox.warning(
                self,
                "Manjkajo podatki",
                "Izpolni vsa polja."
            )
            return

        if password != password_again:
            QMessageBox.warning(
                self,
                "Napaka",
                "Gesli se ne ujemata."
            )
            self.register_password_again.setFocus()
            return

        if len(password) < 8:
            QMessageBox.warning(
                self,
                "Napaka",
                "Geslo mora imeti najmanj 8 znakov."
            )
            return

        try:

            response = self.supabase.auth.sign_up({
                "email": email,
                "password": password,
            })

            if response.session is not None:

                self.current_user = response.user
                self.after_login()

                QMessageBox.information(
                    self,
                    "Uspešno",
                    "Račun je ustvarjen."
                )

            else:

                QMessageBox.information(
                    self,
                    "Preveri e-pošto",
                    "Registracija je bila sprejeta. "
                    "Preveri e-pošto in potrdi račun, "
                    "nato se prijavi."
                )

                self.email_input.setText(email)
                self.show_login()

        except Exception as error:

            QMessageBox.critical(
                self,
                "Napaka registracije",
                str(error)
            )

    # --------------------------------------------------
    # PRIJAVA
    # --------------------------------------------------

    def login(self):

        if self.supabase is None:
            QMessageBox.warning(
                self,
                "Napaka",
                "Povezava s Supabase ni nastavljena."
            )
            return

        email = self.email_input.text().strip()
        password = self.password_input.text()

        if not email or not password:
            QMessageBox.warning(
                self,
                "Manjkajo podatki",
                "Vpiši e-pošto in geslo."
            )
            return

        try:

            response = self.supabase.auth.sign_in_with_password({
                "email": email,
                "password": password,
            })

            self.current_user = response.user
            self.after_login()

        except Exception as error:

            QMessageBox.critical(
                self,
                "Prijava ni uspela",
                str(error)
            )

    def after_login(self):

        self.account_label.setText(
            f"Prijavljen uporabnik: {self.current_user.email}"
        )

        try:
            self.load_my_profile()

        except Exception as error:
            QMessageBox.critical(
                self,
                "Napaka profila",
                "Profila ni bilo mogoče naložiti:\n"
                + str(error)
            )
            return

        if not (self.profile or {}).get("username"):

            QMessageBox.information(
                self,
                "Nastavi uporabniško ime",
                "Pred uporabo iskanja prijateljev "
                "najprej nastavi uporabniško ime."
            )

            self.show_profile()

        else:
            self.show_home()

        self.start_websocket()

        self.statusBar().showMessage(
            "Uspešno si prijavljen."
        )

    # --------------------------------------------------
    # NALOŽI MOJ PROFIL
    # --------------------------------------------------

    def load_my_profile(self):

        response = (
            self.supabase.table("profiles")
            .select("user_id, username, display_name")
            .eq("user_id", self.current_user.id)
            .limit(1)
            .execute()
        )

        rows = response.data or []
        self.profile = rows[0] if rows else None

    # --------------------------------------------------
    # SHRANI PROFIL
    # --------------------------------------------------

    def save_profile(self):

        if not self.current_user:
            return

        username = self.username_input.text().strip()
        display_name = self.display_name_input.text().strip()

        if len(username) < 3 or len(username) > 24:
            QMessageBox.warning(
                self,
                "Neveljavno uporabniško ime",
                "Uporabniško ime mora imeti od 3 do 24 znakov."
            )
            self.username_input.setFocus()
            return

        if not all(
            character.isalnum() or character == "_"
            for character in username
        ):
            QMessageBox.warning(
                self,
                "Neveljavno uporabniško ime",
                "Uporabi samo črke, številke in podčrtaj."
            )
            self.username_input.setFocus()
            return

        try:

            payload = {
                "username": username,
                "display_name": display_name or username,
            }

            self.supabase.table("profiles").update(
                payload
            ).eq(
                "user_id",
                self.current_user.id
            ).execute()

            self.load_my_profile()

            QMessageBox.information(
                self,
                "Profil shranjen",
                "Tvoj profil je uspešno shranjen."
            )

            self.show_home()

        except Exception as error:

            QMessageBox.critical(
                self,
                "Napaka profila",
                "Profila ni bilo mogoče shraniti.\n\n"
                + str(error)
                + "\n\nUporabniško ime je morda že zasedeno."
            )

    # --------------------------------------------------
    # ISKANJE UPORABNIKOV
    # --------------------------------------------------

    def search_users(self):

        term = self.search_input.text().strip()

        if not term:
            QMessageBox.information(
                self,
                "Iskanje",
                "Najprej vpiši uporabniško ime ali e-pošto."
            )
            self.search_input.setFocus()
            return

        try:

            response = self.supabase.rpc(
                "search_profiles",
                {"search_term": term}
            ).execute()

            self.search_results = [
                row
                for row in (response.data or [])
                if row.get("id") != self.current_user.id
            ]

            self.search_list.clear()

            for row in self.search_results:

                username = (
                    row.get("username")
                    or "brez uporabniškega imena"
                )

                display_name = (
                    row.get("display_name")
                    or username
                )

                self.search_list.addItem(
                    f"{display_name} (@{username})"
                )

            if not self.search_results:

                QMessageBox.information(
                    self,
                    "Ni zadetkov",
                    "Nisem našel uporabnika. "
                    "Preveri vnos in poskusi znova."
                )

            else:

                self.search_list.setCurrentRow(0)
                self.search_list.setFocus()

        except Exception as error:

            QMessageBox.critical(
                self,
                "Napaka iskanja",
                "Iskanje ni uspelo:\n" + str(error)
            )

    # --------------------------------------------------
    # POŠLJI PROŠNJO
    # --------------------------------------------------

    def send_friend_request(self):

        index = self.search_list.currentRow()

        if index < 0 or index >= len(self.search_results):
            QMessageBox.information(
                self,
                "Izberi uporabnika",
                "Najprej izberi uporabnika s seznama rezultatov."
            )
            return

        receiver_id = self.search_results[index].get("id")

        try:

            self.supabase.table("friend_requests").insert({
                "sender_id": self.current_user.id,
                "receiver_id": receiver_id,
                "status": "pending",
            }).execute()

            QMessageBox.information(
                self,
                "Prošnja poslana",
                "Prošnja za prijateljstvo je bila poslana."
            )

        except Exception as error:

            QMessageBox.warning(
                self,
                "Prošnje ni bilo mogoče poslati",
                str(error)
                + "\n\nMorda prošnja že obstaja "
                "ali pa sta že prijatelja."
            )

    # --------------------------------------------------
    # NALOŽI PREJETE PROŠNJE
    # --------------------------------------------------

    def load_requests(self):

        self.requests_list.clear()
        self.requests_data = []

        try:

            response = (
                self.supabase.table("friend_requests")
                .select("id, sender_id, created_at")
                .eq("receiver_id", self.current_user.id)
                .eq("status", "pending")
                .execute()
            )

            requests = response.data or []

            sender_ids = list({
                row["sender_id"]
                for row in requests
            })

            profiles = {}

            if sender_ids:

                profile_response = (
                    self.supabase.table("profiles")
                    .select("user_id, username, display_name")
                    .in_("user_id", sender_ids)
                    .execute()
                )

                profiles = {
                    row["user_id"]: row
                    for row in (profile_response.data or [])
                }

            for request in requests:

                profile = profiles.get(
                    request["sender_id"], {}
                )

                username = (
                    profile.get("username")
                    or "neznan uporabnik"
                )

                display_name = (
                    profile.get("display_name")
                    or username
                )

                self.requests_data.append(request)

                self.requests_list.addItem(
                    f"{display_name} (@{username})"
                )

            if not self.requests_data:

                self.requests_list.addItem(
                    "Trenutno nimaš prejetih prošenj."
                )
                self.requests_list.setCurrentRow(-1)

            else:
                self.requests_list.setCurrentRow(0)

        except Exception as error:

            QMessageBox.critical(
                self,
                "Napaka",
                "Prošenj ni bilo mogoče naložiti:\n"
                + str(error)
            )

    # --------------------------------------------------
    # SPREJMI ALI ZAVRNI PROŠNJO
    # --------------------------------------------------

    def update_friend_request(self, new_status):

        index = self.requests_list.currentRow()

        if index < 0 or index >= len(self.requests_data):
            QMessageBox.information(
                self,
                "Izberi prošnjo",
                "Najprej izberi prošnjo."
            )
            return

        request_id = self.requests_data[index]["id"]

        try:

            self.supabase.table("friend_requests").update({
                "status": new_status
            }).eq(
                "id", request_id
            ).eq(
                "receiver_id", self.current_user.id
            ).eq(
                "status", "pending"
            ).execute()

            if new_status == "accepted":
                message = "Prošnja je sprejeta."
            else:
                message = "Prošnja je zavrnjena."

            QMessageBox.information(
                self,
                "Končano",
                message
            )

            self.load_requests()

        except Exception as error:

            QMessageBox.critical(
                self,
                "Napaka",
                "Prošnje ni bilo mogoče obdelati:\n"
                + str(error)
            )

    def accept_request(self):
        self.update_friend_request("accepted")

    def reject_request(self):
        self.update_friend_request("rejected")

    # --------------------------------------------------
    # NALOŽI SEZNAM PRIJATELJEV
    # --------------------------------------------------

    def load_friends(self):

        self.friends_list.clear()
        self.friends_data = []

        try:
            user_id = self.current_user.id
            response = (
                self.supabase.table("friendships")
                .select("user_a, user_b")
                .or_(f"user_a.eq.{user_id},user_b.eq.{user_id}")
                .execute()
            )

            friend_ids = []
            for row in response.data or []:
                other_id = row["user_b"] if row["user_a"] == user_id else row["user_a"]
                friend_ids.append(other_id)

            if not friend_ids:
                self.friends_list.addItem("Še nimaš prijateljev.")
                return

            profile_response = (
                self.supabase.table("profiles")
                .select("user_id, username, display_name")
                .in_("user_id", friend_ids)
                .execute()
            )

            profiles = {
                row["user_id"]: row
                for row in (profile_response.data or [])
            }

            for friend_id in friend_ids:
                profile = profiles.get(friend_id, {})
                username = profile.get("username") or "brez uporabniškega imena"
                display_name = profile.get("display_name") or username
                self.friends_data.append({
                    "user_id": friend_id,
                    "username": username,
                    "display_name": display_name,
                })
                self.friends_list.addItem(f"{display_name} (@{username})")

            if self.friends_data:
                self.friends_list.setCurrentRow(0)

        except Exception as error:
            QMessageBox.critical(
                self,
                "Napaka",
                "Seznama prijateljev ni bilo mogoče naložiti:\n" + str(error)
            )

    # --------------------------------------------------
    # SUPABASE ACCESS TOKEN
    # --------------------------------------------------

    def get_access_token(self):

        if self.supabase is None:
            return None

        try:
            session = self.supabase.auth.get_session()
            if session is None:
                return None

            access_token = getattr(session, "access_token", None)
            return access_token or None
        except Exception:
            return None

    def logout(self):

        if self.current_call and self.current_call.get("call_id"):
            self.send_websocket_message({
                "type": "hangup",
                "call_id": self.current_call["call_id"],
            })

        self.finish_call_ui("Odjavljen si.")
        self.stop_websocket()

        try:
            if self.supabase is not None:
                self.supabase.auth.sign_out()
        except Exception:
            pass

        self.current_user = None
        self.profile = None
        self.password_input.clear()
        self.show_login()
        self.statusBar().showMessage("Uspešno si se odjavil.")

# ------------------------------------------------------
# ZAGON PROGRAMA
# ------------------------------------------------------

def main():

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)

    window = TalkTogetherWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
