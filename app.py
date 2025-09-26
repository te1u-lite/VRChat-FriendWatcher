import logging
import queue
import os
import threading
from pathlib import Path
from datetime import datetime
from tkinter import messagebox, simpledialog
from dotenv import load_dotenv, find_dotenv, set_key

from core.logging_setup import setup_logging
from core.vrc_client import VRChatClient, TwoFactorRequired, UserAgentRejected
from core.watcher import WatcherThread
from core.realtime_ws import WebSocketFriendSource, WebSocketConfig
from gui.main_window import MainWindow

APP_NAME = "VRChat-FriendWatcher"
APP_VER = "0.1.0"

load_dotenv()

# .env の場所を特定 (なければカレント直下に作る)
ENV_FILE = find_dotenv(usecwd=True) or str(Path.cwd()/".env")

# セッションCookie保存先 (前の提案と同じにしておくと吉)
CFG_DIR = Path(os.getenv("VRCWATCHER_HOME") or (Path.home()/".vrcwatcher"))
CFG_DIR.mkdir(parents=True, exist_ok=True)
COOKIE_PATH = str(CFG_DIR / "session.json")

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
    event_queue = queue.Queue()
    app.attach_event_queue(event_queue)

    load_dotenv(ENV_FILE)
    u = os.getenv("VRC_USERNAME") or ""
    p = os.getenv("VRC_PASSWORD") or ""

    if u:
        app.ent_user.insert(0, u)
    if p:
        app.ent_pass.insert(0, p)

    app.mainloop()


def on_start(username: str, password: str, otp: str | None, interval: int, mode: str, target_group: str) -> None:
    """
    MainWindow から「開始」押下で呼ばれる。
    1) パスワードだけで login_start()
    2) 2FA 必要なら TwoFactorRequired を捕まえてコード入力 → submit_code()
    3) 成功後に一度だけ friends を取得して画面に反映 (Watcherは未実装)
    """
    global _vrc, app, event_queue, _watcher, _ws_thread, _stop_event
    assert app is not None and event_queue is not None

    on_stop()

    interval = int(interval)
    _vrc = VRChatClient(user_agent=CUSTOM_UA)

    # 1) セッション (Cookie) 復元を最優先。成功すればOTP不要
    resumed = False
    try:
        resumed = _vrc.load_cookies(COOKIE_PATH)
        log.info("resume=%s cookie_file=%s exists=%s", resumed,
                 COOKIE_PATH, os.path.exists(COOKIE_PATH))
    except Exception as e:
        log.warning("Cookie読み込み失敗: %s", e)
        resumed = False

    if not resumed:
        # 2) GUI未入力 なら .env から補間
        if not username:
            username = os.getenv("VRC_USERNAME") or ""
        if not password:
            password = os.getenv("VRC_PASSWORD") or ""

        if not username or not password:
            raise RuntimeError(
                "ユーザー名/パスワードが未入力 (.env に VRC_USERNAME / VRC_PASSWORD を設定するか、GUIに入力してください)")

        # 3) 通常ログイン (必要ならOTP)
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

        # 4) ログイン成功 → Cookie保存 & .env に資格情報を保存
        try:
            _vrc.save_cookies(COOKIE_PATH)
        except Exception as e:
            log.warning("Cookie 保存失敗: %s", e)

        try:
            set_key(ENV_FILE, "VRC_USERNAME", username)
            set_key(ENV_FILE, "VRC_PASSWORD", password)
            log.info("資格情報を .env に保存しました: %s", ENV_FILE)
        except Exception as e:
            log.warning(".env への保存失敗: %s", e)
    else:
        log.info("セッション再開ログイン成功 (OTP不要)")

    # ここまで来たらログイン確定。オンライン一覧を1回だけ取得してUI更新
    filter_ids = None
    if target_group != "all":
        gi = {"fav1": 1, "fav2": 2, "fav3": 3, "fav4": 4}.get(target_group, 1)
        try:
            ids = _vrc.fetch_favorite_friend_ids(gi)
            filter_ids = ids or set()
            log.info("Target group=%s, users=%d", target_group, len(filter_ids))
        except Exception as e:
            log.warning("お気に入り取得に失敗: %s (全件監視にフォールバック)", e)
            filter_ids = None  # 失敗時は全件

    # --- ここで一括ログ出力 (GUI側は "online" をリストでも受け付け可) ---
    try:
        initial = _vrc.fetch_online_friends(only_ids=filter_ids)
        if initial:
            event_queue.put(("online", initial))
    except Exception as e:
        log.warning("初回オンライン一括取得に失敗: %s", e)

    # 起動時に一度だけ心拍・初期状態を反映 (ログ通知はウォッチャ/WSが行う)
    try:
        event_queue.put(("heartbeat", datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    except Exception:
        pass

    # 停止イベント
    _stop_event = threading.Event()

    if mode == "ws":
        # ---- WebSocket モード ----
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
        _ws_thread = WebSocketFriendSource(
            ws_cfg, event_queue, _stop_event, vrc=_vrc, filter_ids=filter_ids, emit_legacy=False)
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
            filter_ids=filter_ids,
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
