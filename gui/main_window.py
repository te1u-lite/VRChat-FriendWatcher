from __future__ import annotations

import tkinter as tk
from tkinter import ttk, messagebox
import queue
from typing import Callable, Optional

# Watcher から送られてくるイベント名 (文字列) を先に決めておきます。
# あとで core/events.py を導入するなら、そちらに移して import してください。
EVENT_ONLINE_NOW = "online_now"       # payload: list[{"id","name"}]
EVENT_LIST_UPDATE = "list_update"    # payload: list[{"id","name"}]
EVENT_HEARTBEAT = "heartbeat"        # payload: "YYYY-mm-dd HH:MM:SS"
EVENT_ERROR = "error"                # payload: str

EVENT_ONLINE = "online"
EVENT_OFFLINE = "offline"
EVENT_LOG = "log"


class MainWindow(tk.Tk):
    """
    Tkinter メインウィンドウ。
    - ユーザー入力 (ID/パス/OTP、間隔)
    - 開始/停止ボタン
    - 現在オンライン一覧
    - ステータスバー
    - Watcher からのイベント Queue を after() でドレイン
    - 実際の監視開始/停止処理はコールバックで外から注入 (疎結合)
    """

    def __init__(
            self,
            on_start: Optional[Callable[[
                str, str, Optional[str], int, str, str], None]] = None,
            on_stop: Optional[Callable[[], None]] = None,
    ) -> None:
        super().__init__()
        self.title("VRChat Friend Watcher")
        self.geometry("600x560")
        self.minsize(560, 520)

        # 外部注入のコールバック
        self._on_start = on_start
        self._on_stop = on_stop

        # Watcher から渡されるイベントキュー (あとで attach_event_queue で注入)
        self._event_queue: Optional[queue.Queue] = None

        # UI 構築
        self._build_ui()

        # Queue ドレイン開始 (Queue 未接続でも安全)
        self.after(500, self._drain_queue)

        # 終了時のクリーンアップ
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------- UI 構築 ----------
    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=10)
        root.pack(fill="both", expand=True)

        # レイアウトグリッドの伸縮
        for c in range(2):
            root.columnconfigure(c, weight=1)

        row = 0
        ttk.Label(root, text="VRChat Username").grid(row=row, column=0, sticky="w")
        self.ent_user = ttk.Entry(root, width=30)
        self.ent_user.grid(row=row, column=1, sticky="we")
        row += 1

        ttk.Label(root, text="Password").grid(row=row, column=0, sticky="w")
        self.ent_pass = ttk.Entry(root, width=30, show="*")
        self.ent_pass.grid(row=row, column=1, sticky="we")
        row += 1

        ttk.Label(root, text="OTP (2FA 任意)").grid(row=row, column=0, sticky="w")
        self.ent_otp = ttk.Entry(root, width=30)
        self.ent_otp.grid(row=row, column=1, sticky="we")
        row += 1

        # 監視方式
        frm_mode = ttk.Frame(root)
        frm_mode.grid(row=row, column=0, columnspan=2, pady=(6, 2), sticky="we")
        self.var_mode = tk.StringVar(value="rest")  # "rest" or "ws"
        ttk.Label(frm_mode, text="Mode").grid(row=0, column=0, sticky="w", padx=(0, 8))
        r1 = ttk.Radiobutton(frm_mode, text="REST (Polling)", value="rest",
                             variable=self.var_mode, command=self._update_interval_state)
        r2 = ttk.Radiobutton(frm_mode, text="WebSocket", value="ws",
                             variable=self.var_mode, command=self._update_interval_state)
        r1.grid(row=0, column=1, sticky="w")
        r2.grid(row=0, column=2, sticky="w")
        row += 1

        # インターバル (REST時にのみ有効)
        ttk.Label(root, text="Interval (sec)").grid(row=row, column=0, sticky="w")
        self.spin_interval = ttk.Spinbox(root, from_=5, to=600, increment=5, width=12)
        self.spin_interval.set("30")
        self.spin_interval.grid(row=row, column=1, sticky="w")
        row += 1

        # 対象フレンド
        ttk.Label(root, text="Target Friends").grid(row=row, column=0, sticky="w")
        self.cbo_target = ttk.Combobox(
            root,
            values=[
                "All friends",
                "Favorite Friends 1",
                "Favorite Friends 2",
                "Favorite Friends 3",
                "Favorite Friends 4",
            ],
            state="readonly",
        )
        self.cbo_target.current(0)
        self.cbo_target.grid(row=row, column=1, sticky="we")
        self.cbo_target.bind("<<ComboboxSelected>>", self._on_target_changed)
        row += 1

        # ボタン行
        btns = ttk.Frame(root)
        btns.grid(row=row, column=0, columnspan=2, pady=(8, 6), sticky="we")
        btns.columnconfigure(0, weight=1)
        btns.columnconfigure(1, weight=1)
        btns.columnconfigure(2, weight=0)

        self.btn_start = ttk.Button(btns, text="開始", command=self._handle_start)
        self.btn_start.grid(row=0, column=0, padx=(0, 6), sticky="we")
        self.btn_stop = ttk.Button(btns, text="停止", command=self._handle_stop, state="disabled")
        self.btn_stop.grid(row=0, column=1, padx=(6, 0), sticky="we")
        self.btn_clear = ttk.Button(btns, text="クリア", command=self._clear_log)
        self.btn_clear.grid(row=0, column=2, sticky="e")
        row += 1

        # ログコンソール
        ttk.Label(root, text="ログ").grid(row=row, column=0, columnspan=2, sticky="w")
        row += 1

        console_frame = ttk.Frame(root)
        console_frame.grid(row=row, column=0, columnspan=2, sticky="nsew")
        root.rowconfigure(row, weight=1)
        row += 1

        self.txt_log = tk.Text(console_frame, height=16, wrap="none", state="disabled")
        yscroll = ttk.Scrollbar(console_frame, orient="vertical", command=self.txt_log.yview)
        self.txt_log.configure(yscrollcommand=yscroll.set)
        self.txt_log.pack(side="left", fill="both", expand=True)
        yscroll.pack(side="right", fill="y")

        # ステータスバー
        self.var_status = tk.StringVar(value="未接続")
        status = ttk.Label(root, textvariable=self.var_status, anchor="w")
        status.grid(row=row, column=0, columnspan=2, sticky="we")

        # 初期状態調整
        self._update_interval_state()

    # ---------- 外部 API（Watcher/アプリ側から使う） ----------
    def attach_event_queue(self, q: queue.Queue) -> None:
        """Watcher スレッドからのイベントを受け取る Queue を注入。"""
        self._event_queue = q

    def set_running_ui(self, running: bool) -> None:
        """開始/停止ボタンの状態を更新。"""
        if running:
            self.btn_start.config(state="disabled")
            self.btn_stop.config(state="normal")
        else:
            self.btn_start.config(state="normal")
            self.btn_stop.config(state="disabled")

    def get_credentials(self) -> tuple[str, str, Optional[str]]:
        """ (username, password, otp) を返す。"""
        u = self.ent_user.get().strip()
        p = self.ent_pass.get().strip()
        o = self.ent_otp.get().strip() or None
        return u, p, o

    def get_interval_sec(self) -> int:
        try:
            return max(1, int(self.spin_interval.get()))
        except Exception:
            return 30

    def get_mode(self) -> str:
        return self.var_mode.get()  # "rest" or "ws"

    def get_target_group(self) -> str:
        idx = self.cbo_target.current()
        return ["all", "fav1", "fav2", "fav3", "fav4"][idx]

    # ---------- 内部処理 ----------
    def _update_interval_state(self) -> None:
        # REST選択時のみ Interval を使えるように
        state = "normal" if self.get_mode() == "rest" else "disabled"
        self.spin_interval.config(state=state)

    def _handle_start(self) -> None:
        u, p, _ = self.get_credentials()
        if not u or not p:
            messagebox.showerror("エラー", "ユーザー名とパスワードを入力してください。")
            return

        self.var_status.set("ログイン/監視を開始します")
        if self._on_start:
            # 実処理は外 (Watcher 起動など)
            username, password, otp = self.get_credentials()
            interval = self.get_interval_sec()
            mode = self.get_mode()
            tg = self.get_target_group()
            try:
                self._on_start(username, password, otp, interval, mode, tg)
                self.set_running_ui(True)
                self.var_status.set("監視中")
            except Exception as e:
                messagebox.showerror("エラー", f"開始に失敗しました : {e}")
                self.var_status.set("開始失敗")
        else:
            # コールバック未注入でも UI は生かす
            self.set_running_ui(True)
            self.var_status.set("監視中 (ダミー。後でコールバックを注入してください)")

    def _handle_stop(self) -> None:
        if self._on_stop:
            try:
                self._on_stop()
            except Exception as e:
                messagebox.showwarning("警告", f"停止処理で警告 : {e}")
        self.set_running_ui(False)
        self.var_status.set("停止中")

    def _on_close(self) -> None:
        # 終了時は停止コールバックを呼んでから閉じる
        try:
            if self._on_stop:
                self._on_stop()
        finally:
            self.destroy()

    def _clear_log(self) -> None:
        self.txt_log.config(state="normal")
        self.txt_log.delete("1.0", tk.END)
        self.txt_log.config(state="disabled")

    def _append_log(self, text: str) -> None:
        self.txt_log.config(state="normal")
        self.txt_log.insert(tk.END, text)
        self.txt_log.see(tk.END)
        self.txt_log.config(state="disabled")

    def _drain_queue(self) -> None:
        """Watcher からのイベントをUIに反映。Queue未設定でも安全にスキップ。"""
        q = self._event_queue
        if q is not None:
            try:
                while True:
                    kind, payload = q.get_nowait()

                    if kind == EVENT_ONLINE:
                        items = payload if isinstance(payload, list) else [payload]
                        for f in items:
                            self._append_log(
                                f"[ONLINE] {f.get("name", "(unknown)")} ({f.get("id", "")})\n")
                    elif kind == EVENT_OFFLINE:
                        items = payload if isinstance(payload, list)else [payload]
                        for f in items:
                            self._append_log(
                                f"[OFFLINE] {f.get("name", "(unknown)")} ({f.get("id", "")})\n")
                    elif kind == EVENT_LOG:
                        self._append_log(
                            str(payload)+("\n" if not str(payload).endswith("\n")else ""))
                    # 既存イベント
                    elif kind == EVENT_ONLINE_NOW:
                        names = ", ".join(f.get("name", "(unknown)")for f in payload)
                        if names:
                            self._append_log(f"[ONLINE] {names}\n")
                    elif kind == EVENT_LIST_UPDATE:
                        self._append_log("[INFO] list_update 受信 (ログUIでは非表示対象。必要なら実装を調整)\n")
                    elif kind == EVENT_HEARTBEAT:
                        self.var_status.set(f"最終更新: {payload}")
                    elif kind == EVENT_ERROR:
                        self.var_status.set(f"エラー: {payload}")
                        self._append_log(f"[ERROR] {payload}\n")

            except queue.Empty:
                pass

        # 500msごとにポーリング
        self.after(500, self._drain_queue)

    def _on_target_changed(self, *_):
        # 稼働中なら確認→再起動
        running = (self.btn_stop["state"] == "normal")
        if running and messagebox.askyesno("対象切り替え", "再ログインして対象を切り替えます。よろしいですか？"):
            if self._on_stop:
                self._on_stop()
            if self._on_start:
                u, p, otp = self.get_credentials()
                interval = self.get_interval_sec()
                mode = self.get_mode()
                tg = self.get_target_group()
                self._on_start(u, p, otp, interval, mode, tg)
                self.set_running_ui(True)
