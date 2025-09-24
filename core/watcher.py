from __future__ import annotations

import logging
import queue
import threading
import time
from datetime import datetime
from typing import Dict, List, Set

from core.vrc_client import VRChatClient

log = logging.getLogger(__name__)


class WatcherThread(threading.Thread):
    """
    VRChatのフレンド状態を定期取得し、差分 (新規オンライン) を検知してQueueへイベントを流す。

    キューに投げるイベント (kind, payload) :
    - ("online_now",  [ {id, name}, ... ])  # 新規にオンラインになった人たち
    - ("list_update", [ {id, name}, ... ])  # 現在オンラインの全リスト
    - ("heartbeat",   "YYYY-mm-dd HH:MM:SS")# 最終更新
    - ("error",       "メッセージ")          # 例外など
    """

    def __init__(
            self,
            vrc: VRChatClient,
            interval_sec: int,
            event_queue: queue.Queue,
            stop_event: threading.Event,
            first_run_no_notify: bool = True,   # 初回は通知しない (スパム防止)
    ) -> None:
        super().__init__(daemon=True)
        self.vrc = vrc
        self.interval = max(5, int(interval_sec))
        self.q = event_queue
        self.stop_event = stop_event
        self.first_run = True
        self.first_run_no_notify = first_run_no_notify
        self.prev_online_ids: Set[str] = set()

        # 失敗時の指数バックオフ
        self._backoff = 1
        self._backoff_max = 60

    def run(self) -> None:
        log.info("WatcherThread started (interval=%ss)", self.interval)
        try:
            while not self.stop_event.is_set():
                try:
                    friends: List[Dict[str, str]] = self.vrc.fetch_online_friends()
                    now_ids = {f["id"]for f in friends}

                    # 新規オンライン検知
                    if self.first_run and self.first_run_no_notify:
                        newly_online = []
                    else:
                        newly_online_ids = now_ids - self.prev_online_ids
                        newly_online = [f for f in friends if f["id"] in newly_online_ids]

                    # イベント出力
                    if newly_online:
                        self.q.put(("online_now", newly_online))
                        for f in newly_online:
                            log.info("%s がオンラインになりました", f["name"])

                    self.q.put(("list_update", friends))
                    self.q.put(("heartbeat", datetime.now().strftime("%Y-%m-%d %H:%M:%S")))

                    # 状態更新・バックオフリセット
                    self.prev_online_ids = now_ids
                    self.first_run = False
                    self._backoff = 1

                    # 通常スリープ
                    self._sleep_with_stop(self.interval)

                except Exception as e:
                    log.exception("Watcher loop error: %s", e)
                    self.q.put(("error", f"監視エラー: {e}"))
                    # バックオフして再試行
                    wait = min(self._backoff, self._backoff_max)
                    self._backoff = min(self._backoff*2, self._backoff_max)
                    self._sleep_with_stop(wait)

        finally:
            log.info("WatcherThread stopped")

    def _sleep_with_stop(self, seconds: int) -> None:
        """停止指示に素早く反応できるよう、短い刻みで眠る。"""
        deadline = time.time()+seconds
        while not self.stop_event.is_set() and time.time() < deadline:
            time.sleep(0.5)
