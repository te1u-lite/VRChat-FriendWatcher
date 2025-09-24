import logging
import queue
import os
import threading
from datetime import datetime
from tkinter import messagebox, simpledialog
from dotenv import load_dotenv

from core.logging_setup import setup_logging
from core.vrc_client import VRChatClient, TwoFactorRequired, UserAgentRejected
from core.watcher import WatcherThread
from core.realtime_ws import WebSocketFriendSource, WebSocketConfig
from gui.main_window import MainWindow, EVENT_LIST_UPDATE, EVENT_HEARTBEAT, EVENT_ERROR

APP_NAME = "VRChat-FriendWatcher"
APP_VER = "0.1.0"

load_dotenv()

CONTACT = os.getenv("CONTACT_EMAIL") or "unknown@example.com"
CUSTOM_UA = f"{APP_NAME}/{APP_VER} ({CONTACT})"

# GUIオブジェクト/キューをコールバックから参照するためのグローバル
app: MainWindow | None = None
event_queue: queue.Queue | None = None

# VRChatクライアント（開始後に生成）
_vrc: VRChatClient | None = None
log = logging.getLogger(__name__)

# グローバル
_ws_thread: WebSocketFriendSource | None = None
_watcher: WatcherThread | None = None
_stop_event: threading.Event | None = None

USE_WS = (os.getenv("REALTIME", "").lower() == "ws")
WS_URL = os.getenv("WS_URL")
WS_COOKIE = os.getenv("WS_COOKIE")


def main():
    global app, event_queue

    # ログ初期化
    setup_logging()

    # GUI 構築 (on_start / on_stop を渡す)
    app = MainWindow(on_start=on_start, on_stop=on_stop)
    # Watcher未実装でもイベントでUIを更新したいのでキューをつないでおく
    event_queue = queue.Queue()
    app.attach_event_queue(event_queue)

    app.mainloop()


def on_start(username: str, password: str, otp: str | None, interval: int):
    """
    MainWindow から「開始」押下で呼ばれる。
    1) パスワードだけで login_start()
    2) 2FA 必要なら TwoFactorRequired を捕まえてコード入力 → submit_code()
    3) 成功後に一度だけ friends を取得して画面に反映 (Watcherは未実装)
    """
    global _vrc, app, event_queue, _watcher, _ws_thread, _stop_event
    assert app is not None and event_queue is not None
    interval = int(interval)

    _vrc = VRChatClient(user_agent=CUSTOM_UA)

    # まずPWだけで試行 (ここで2FA判定/メール送信まで進む)
    try:
        if _vrc.login_start(username, password):
            log.info("ログイン成功 (2FA不要) ")
        else:
            raise RuntimeError("不明な理由でログインに失敗しました。")
    except UserAgentRejected as e:
        # UA不備 → コード入力に進ませない
        messagebox.showerror(
            "User-Agent エラー",
            f"User-Agent が不十分なため403で拒否されました。\n\n"
            f"現在のUA:\n{CUSTOM_UA}\n\n{e}\n"
            "UAは「アプリ名/バージョン (連絡先)」形式にしてください。"
        )
        raise
    except TwoFactorRequired:
        # 2FAが必要。GUIのOTP欄に入っていればそれを先に使用、なければダイアログで取得
        code = (otp or "").strip() or simpledialog.askstring(
            "2FAコード",
            "メールで届いた6桁コード、またはTOTP を入力してください:",
            parent=app,
        )
        if not code:
            raise RuntimeError("2FAコードが入力されませんでした。")

        # 1回目失敗時は再入力を促す
        if not _vrc.submit_code(code):
            code2 = simpledialog.askstring("再入力", "コードが無効/期限切れです。もう一度入力してください：", parent=app)
            if not code2 or not _vrc.submit_code(code2.strip()):
                raise RuntimeError("2FA検証に失敗しました。")

    # ここまで来たらログイン確定。オンライン一覧を1回だけ取得してUI更新
    try:
        friends = _vrc.fetch_online_friends()
        event_queue.put((EVENT_LIST_UPDATE, friends))
        event_queue.put((EVENT_HEARTBEAT, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        log.info("オンライン: %d人", len(friends))
        for f in friends:
            log.info(" - %s (%s)", f["name"], f["id"])
    except Exception as e:
        log.exception("フレンド取得に失敗: %s", e)
        event_queue.put((EVENT_ERROR, f"フレンド取得失敗：{e}"))
        # 例外をそのまま投げると MainWindow 側で「開始失敗」のダイアログが出ます
        raise

    # まず一度だけRESTで即時更新

    # 停止イベント
    _stop_event = threading.Event()

    # ---- WebSocket モード ----
    if USE_WS:
        ws_url = os.getenv("WS_URL")
        headers = {"User-Agent": CUSTOM_UA}

        if not ws_url:
            # Cookie から auth を拾って URL を作る
            ws_url = _vrc.build_pipeline_ws_url(base=os.getenv(
                "WS_BASE", "wss://pipeline.vrchat.cloud/"))
            if not ws_url:
                messagebox.showerror(
                    "WebSocket 初期化エラー",
                    "WS_URL が未設定で、auth トークンから URL を自動生成できませんでした。\n"
                    "・ログイン直後か確認\n"
                    "・VRChatClient.get_auth_token() がトークンを取得できているか\n"
                    "・必要なら .env に WS_URL または WS_BASE を設定"
                )
                return

            # 互換性のため Cookie も付ける (不要なら削除可)
            tok = _vrc.get_auth_token()
            if tok:
                headers["Cookie"] = f"auth={tok}"

        # 一部環境で Origin が必須なら .env で渡せるように
        origin = os.getenv("WS_ORIGIN") or None

        ws_cfg = WebSocketConfig(
            url=ws_url,
            headers=headers,
            ping_interval=20,
            ping_timeout=10,
            reconnect_initial=3.0,
            reconnect_max=300.0,
            jitter_ratio=0.2,
            list_flush_interval=5,
            notify_rate_per_min=20,
            periodic_rest_resync_sec=300,
            origin=origin
        )
        _ws_thread = WebSocketFriendSource(ws_cfg, event_queue, _stop_event, vrc=_vrc)
        _ws_thread.start()
        log.info("WebSocket mode started")
    else:
        # ---- REST ポーリング ----
        _watcher = WatcherThread(
            vrc=_vrc,
            interval_sec=interval,
            event_queue=event_queue,
            stop_event=_stop_event,
            first_run_no_notify=True,
        )
        _watcher.start()
        log.info("Polling mode started")


def on_stop():
    """MainWindow から「停止」押下で呼ばれる。今は特に停止処理は無し。"""
    global _watcher, _ws_thread, _stop_event
    if _stop_event is not None:
        _stop_event.set()
    if _watcher is not None and _watcher.is_alive():
        _watcher.join(timeout=3.0)
    if _ws_thread is not None and _ws_thread.is_alive():
        _ws_thread.join(timeout=3.0)
    _watcher = None
    _ws_thread = None
    _stop_event = None


if __name__ == "__main__":
    main()
