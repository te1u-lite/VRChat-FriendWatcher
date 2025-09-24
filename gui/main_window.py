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
                str, str, Optional[str], int], None]] = None,
            on_stop: Optional[Callable[[], None]] = None,
    ) -> None:
        super().__init__()
        self.title("VRChat Friend Watcher")
        self.geometry("560x520")
        self.minsize(520, 480)

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
        # リスト部分の行だけ伸ばす
        stretchy_row = 6

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

        ttk.Label(root, text="Interval (sec)").grid(row=row, column=0, sticky="w")
        self.spin_interval = ttk.Spinbox(root, from_=10, to=600, increment=5, width=12)
        self.spin_interval.set("30")
        self.spin_interval.grid(row=row, column=1, sticky="w")
        row += 1

        # ボタン行
        btns = ttk.Frame(root)
        btns.grid(row=row, column=0, columnspan=2, pady=(8, 4), sticky="we")
        btns.columnconfigure(0, weight=1)
        btns.columnconfigure(1, weight=1)

        self.btn_start = ttk.Button(btns, text="開始", command=self._handle_start)
        self.btn_start.grid(row=0, column=0, padx=(0, 6), sticky="we")
        self.btn_stop = ttk.Button(btns, text="停止", command=self._handle_stop, state="disabled")
        self.btn_stop.grid(row=0, column=1, padx=(6, 0), sticky="we")
        row += 1

        # 現在オンライン
        header = ttk.Frame(root)
        header.grid(row=row, column=0, columnspan=2, sticky="we")
        header.columnconfigure(0, weight=1)
        self.var_online_title = tk.StringVar(value="現在オンライン (0)")
        ttk.Label(header, textvariable=self.var_online_title).grid(row=0, column=0, sticky="w")
        row += 1

        self.list_online = tk.Listbox(root, height=14, activestyle="none")
        self.list_online.grid(row=row, column=0, columnspan=2, sticky="nsew")
        root.rowconfigure(row, weight=1)
        row += 1

        # ステータスバー
        self.var_status = tk.StringVar(value="未接続")
        status = ttk.Label(root, textvariable=self.var_status, anchor="w")
        status.grid(row=row, column=0, columnspan=2, sticky="we")

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

    # ---------- 内部処理 ----------
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
            try:
                self._on_start(username, password, otp, interval)
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

    def _drain_queue(self) -> None:
        """Watcher からのイベントをUIに反映。Queue未設定でも安全にスキップ。"""
        q = self._event_queue
        if q is not None:
            try:
                while True:
                    kind, payload = q.get_nowait()
                    if kind == EVENT_ONLINE_NOW:
                        # 今は通知レイや未実装なので、ステータスに出すだけ
                        names = ", ".join(f["name"] for f in payload)
                        self.var_status.set(f"オンラインになった: {names}")
                    elif kind == EVENT_LIST_UPDATE:
                        self.list_online.delete(0, tk.END)
                        for f in payload:
                            self.list_online.insert(tk.END, f"{f["name"]}")
                        self.var_online_title.set(f"現在オンライン ({len(payload)})")
                    elif kind == EVENT_HEARTBEAT:
                        self.var_status.set(f"最終更新: {payload}")
                    elif kind == EVENT_ERROR:
                        self.var_status.set(f"エラー: {payload}")
                    # 未知イベントは無視
            except queue.Empty:
                pass

        # 500msごとにポーリング
        self.after(500, self._drain_queue)
