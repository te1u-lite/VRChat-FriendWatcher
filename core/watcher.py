from __future__ import annotations

import logging
import queue
import threading
import time
from datetime import datetime
from typing import Dict, Optional, Set

from core.vrc_client import VRChatClient

log = logging.getLogger(__name__)

EVENT_ONLINE = "online"
EVENT_OFFLINE = "offline"
EVENT_HEARTBEAT = "heartbeat"
EVENT_ERROR = "error"


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
            filter_ids: Optional[Set[str]] = None,
    ) -> None:
        super().__init__(daemon=True)
        self.vrc = vrc
        self.interval = max(5, int(interval_sec))
        self.q = event_queue
        self.stop_event = stop_event
        self.first_run_no_notify = first_run_no_notify
        self.filter_ids = set(filter_ids) if filter_ids else None
        self._online_prev: Set[str] = set()
        self._name_cache: Dict[str, str] = {}

    def run(self) -> None:
        suppress = self.first_run_no_notify
        while not self.stop_event.is_set():
            try:
                friends = self.vrc.fetch_online_friends(only_ids=self.filter_ids)
                now_ids = {f["id"]for f in friends}
                for f in friends:
                    self._name_cache[f["id"]] = f["name"]

                if not suppress:
                    new_online = now_ids - self._online_prev
                    new_off = self._online_prev - now_ids
                    for uid in sorted(new_online):
                        self.q.put(
                            (EVENT_ONLINE, {"id": uid, "name": self._name_cache.get(uid, uid)}))
                    for uid in sorted(new_off):
                        self.q.put(
                            (EVENT_OFFLINE, {"id": uid, "name": self._name_cache.get(uid, uid)}))

                self._online_prev = now_ids
                self.q.put((EVENT_HEARTBEAT, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                suppress = False
            except Exception as e:
                log.warning("Polling failed: %s", e)
                self.q.put((EVENT_ERROR, f"RESTポーリング失敗: {e}"))

            # スリープ (停止要求をチェックしながら)
            end = time.time() + self.interval
            while not self.stop_event.is_set() and time.time() < end:
                time.sleep(0.2)
