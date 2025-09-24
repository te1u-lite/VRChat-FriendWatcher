import logging
import queue
import os
from datetime import datetime
from tkinter import messagebox, simpledialog

from dotenv import load_dotenv

from core.logging_setup import setup_logging
from core.vrc_client import VRChatClient, TwoFactorRequired, UserAgentRejected
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
    global _vrc, app, event_queue
    assert app is not None and event_queue is not None

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


def on_stop():
    """MainWindow から「停止」押下で呼ばれる。今は特に停止処理は無し。"""
    log.info("停止要求を受け取りました（Watcher未実装のため何もしません）。")


if __name__ == "__main__":
    main()
