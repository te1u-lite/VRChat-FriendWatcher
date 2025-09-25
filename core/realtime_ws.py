from __future__ import annotations

import json
import logging
import queue
import random
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set

from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

import websocket

from core.vrc_client import VRChatClient

log = logging.getLogger(__name__)


def mask_url(url: str) -> str:
    try:
        s = urlsplit(url)
        q = []
        for k, v in parse_qsl(s.query, keep_blank_values=True):
            if k.lower() in ("authtoken", "token", "access_token"):
                if v:
                    # 先頭4+末尾2だけ残す (長さ依存で調整可)
                    v = v[:4] + "..." + v[-2:]
            q.append((k, v))
        return urlunsplit((s.scheme, s.netloc, s.path, urlencode(q), s.fragment))
    except Exception:
        return "<redacted>"


@dataclass
class WebSocketConfig:
    url: str                                # 例: wss://pipeline.vrchat.cloud/?... （.envから）
    headers: Dict[str, str]                 # 例: wss://pipeline.vrchat.cloud/?... （.envから）
    ping_interval: int = 20                 # 秒
    ping_timeout: int = 10                  # 秒
    reconnect_initial: float = 3.0          # 再接続の初期待機
    reconnect_max: float = 300.0            # 再接続待機の上限
    jitter_ratio: float = 0.2               # ±20% ジッター
    list_flush_interval: int = 5            # “list_update” の最低間隔（秒）
    notify_rate_per_min: int = 20           # “online_now” 通知の1分あたり上限
    periodic_rest_resync_sec: int = 300     # 5分ごとにRESTで整合性同期
    origin: Optional[str] = None


class TokenBucket:
    """単純なトークンバケツ。1分当たりN通まで"""

    def __init__(self, capacity_per_min: int):
        self.capacity = capacity_per_min
        self.tokens = capacity_per_min
        self.reset_at = time.monotonic()+60

    def allow(self, n: int = 1) -> bool:
        now = time.monotonic()
        if now >= self.reset_at:
            self.tokens = self.capacity
            self.reset_at = now + 60
        if self.tokens >= n:
            self.tokens -= n
            return True
        return False


class WebSocketFriendSource(threading.Thread):
    """
    VRChatのWSからフレンドの "オンライン/オフライン" のイベントを受け取り、
    GUIと同じイベント（online_now/list_update/heartbeat/error）を Queue へ流す。

    - URL/ヘッダは外部注入 (.envや設定ファイル)
    - WSイベントは実環境で形式が異なりうるため、受信ハンドラ内にマッピング層を用意
    - 定期的に REST で全体同期して表示のドリフトを補正
    """

    def __init__(
            self,
            ws_cfg: WebSocketConfig,
            event_queue: queue.Queue,
            stop_event: threading.Event,
            vrc: Optional[VRChatClient] = None,  # REST同期に使う (任意)
    ) -> None:
        super().__init__(daemon=True)
        self.ws_cfg = ws_cfg
        self.q = event_queue
        self.stop_event = stop_event
        self.vrc = vrc

        self._ws: Optional[websocket.WebSocketApp] = None
        self._prev_online_ids: Set[str] = set()
        self._known_names: Dict[str, str] = {}
        self._last_list_flush = 0.0
        self._notify_bucket = TokenBucket(ws_cfg.notify_rate_per_min)
        self._next_resync_at = time.monotonic() + ws_cfg.periodic_rest_resync_sec

        # 受信側バッファ (短時間に多発する更新を束ねて list_update を抑制)
        self._pending_changes: bool = False

    # -------------------- public --------------------
    def run(self) -> None:
        backoff = self.ws_cfg.reconnect_initial
        while not self.stop_event.is_set():
            try:
                log.info("WS connecting: %s", mask_url(self.ws_cfg.url))
                self._connect_and_loop()
                if self.stop_event.is_set():
                    break
                raise ConnectionError("WebSocket closed normalliy; will reconnect")
            except Exception as e:
                if self.stop_event.is_set():
                    break
                log.warning("WS disconnected: %s", e)
                wait = min(backoff, self.ws_cfg.reconnect_max)
                # ジッター
                jitter = random.uniform(1-self.ws_cfg.jitter_ratio, 1+self.ws_cfg.jitter_ratio)
                self._sleep_with_stop(wait*jitter)
                backoff = min(backoff * 2, self.ws_cfg.reconnect_max)
            log.info("WebSocketFriendSource stopped")

    # -------------------- internal --------------------
    def _connect_and_loop(self) -> None:
        if not self.ws_cfg.url or "authToken=" not in self.ws_cfg.url:
            raise ValueError(
                "WebSocket URL is empty. Provide WS_URL or ensure auth token could be extracted.")
        headers = dict(self.ws_cfg.headers or {})
        headers.setdefault("User-Agent", "vrchatapi-python/1.0 (+https://vrchat.community)")

        # setup
        self._ws = websocket.WebSocketApp(
            self.ws_cfg.url,
            header=[f"{k}: {v}" for k, v in headers.items()],
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )

        kwargs = dict(
            ping_interval=self.ws_cfg.ping_interval,
            ping_timeout=self.ws_cfg.ping_timeout,
        )
        if self.ws_cfg.origin:
            kwargs["origin"] = self.ws_cfg.origin

        self._ws.run_forever(**kwargs)

    # ---- WS callbacks ----
    def _on_open(self, ws):
        log.info("WS open")
        self.q.put(("heartbeat", datetime.now().strftime("%Y-%m-%d %H:%M:%S")))

    def _on_close(self, ws, code, reason):
        log.info("WS close: code=%s reason=%s", code, reason)

    def _on_error(self, ws, error):
        log.warning("WS error: %s", error)
        self.q.put(("error", f"WSエラー: {error}"))

    def _on_message(self, ws, message: str):
        try:
            data = json.loads(message)
        except Exception:
            # 想定外形式 (バイナリ/テキスト混在など)
            log.debug("Non-JSON message: %s", message[:200])
            return

        # 文字列だけ来る(pong等) → 無視
        if isinstance(data, str):
            log.debug("WS string frame: %s", data[:200])
            return

        # 配列で来ることがある → 要素ごとに処理
        if isinstance(data, list):
            for item in data:
                self._handle_one_event(item)
            return

        # 通常の dict
        if isinstance(data, dict):
            self._handle_one_event(data)
            return

        # それ以外はスキップ
        log.debug("WS unknown type: %r", type(data))

    def _handle_one_event(self, data: dict):
        if not isinstance(data, dict):
            return

        ev_type = str(data.get("type") or data.get("event") or "").lower()
        payload = data.get("content")

        # content が文字列 (JSON文字列) で来る場合がある → 可能ならデコード
        if isinstance(payload, str) and payload.lstrip().startswith(("{", "[")):
            try:
                payload = json.loads(payload)
            except Exception:
                pass

        # 外側が "notification" のとき、内側の type で再判定
        inner_type = None
        if ev_type == "notification" and isinstance(payload, dict):
            inner_type = str(payload.get("type") or "").lower()
            # 二重エンコードでもう一段文字列のことがある
            inner_content = payload.get("content")
            if isinstance(inner_content, str) and inner_content.lstrip().startswith(("{", "[")):
                try:
                    inner_content = json.loads(inner_content)
                except Exception:
                    pass
            if inner_content is not None:
                payload = inner_content or payload

        effective_type = inner_type or ev_type

        def _uid_and_name(p: dict) -> tuple[Optional[str], Optional[str]]:
            if not isinstance(p, dict):
                return None, None
            uid = p.get("userId") or p.get("userid") or p.get("id")
            user = p.get("user") or {}
            name = (
                (user.get("displayName") if isinstance(user, dict) else None)
                or p.get("displayName")
                or p.get("username")
                or p.get("name")
            )
            return uid, name

        if effective_type in {"friend-online", "friend_active", "friend-active", "user-online", "friend-location"}:
            # 配列で複数が一度に来る可能性も一応吸収
            items = payload if isinstance(payload, list) else [payload]
            for p in items:
                uid, name = _uid_and_name(p)
                if uid:
                    if name:
                        self._known_names[uid] = str(name)
                    self._handle_online(uid)

        elif effective_type in {"friend-offline", "friend_offline", "user-offline"}:
            items = payload if isinstance(payload, list) else [payload]
            for p in items:
                uid, _ = _uid_and_name(p)
                if uid:
                    self._handle_offline(uid)

        else:
            # payload が dict とは限らないので keys() を呼ばない
            log.debug("Unhandled event: %s", effective_type)

        # 心拍 & バッファ吐きはここで
        self.q.put(("heartbeat", datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        now = time.monotonic()
        if self._pending_changes and now - self._last_list_flush >= self.ws_cfg.list_flush_interval:
            self._flush_list_update()
        if self.vrc and now >= self._next_resync_at:
            self._resync_with_rest()
            self._next_resync_at = now + self.ws_cfg.periodic_rest_resync_sec

    # ---- state updates ----

    def _handle_online(self, uid: str):
        if uid not in self._prev_online_ids:
            self._prev_online_ids.add(uid)
            self._pending_changes = True
            # 通知をレート制限
            if self._notify_bucket.allow(1):
                name = self._known_names.get(uid, uid)
                self.q.put(("online_now", [{"id": uid, "name": self._known_names.get(uid, uid)}]))
                log.info("Friend online: %s (%s)", name, uid)

    def _handle_offline(self, uid: str):
        if uid in self._prev_online_ids:
            self._prev_online_ids.remove(uid)
            self._pending_changes = True
        # オフライン通知はノイズになりがちなので省略 (必要なら別イベントに)

    def _flush_list_update(self):
        friends = [{"id": uid, "name": self._known_names.get(
            uid, uid)}for uid in sorted(self._prev_online_ids)]
        self.q.put(("list_update", friends))
        self._last_list_flush = time.monotonic()
        self._pending_changes = False

    def _resync_with_rest(self):
        try:
            friends = self.vrc.fetch_online_friends()  # [{"id","name"}]
            # セットで比較してズレ補正
            rest_ids = {f["id"] for f in friends}
            if rest_ids != self._prev_online_ids:
                self._prev_online_ids = rest_ids
                for f in friends:
                    self._known_names[f["id"]] = f["name"]
                self._flush_list_update()
            log.debug("REST resync OK (online=%d)", len(friends))
        except Exception as e:
            log.warning("REST resync failed: %s", e)

    # ---- utils ----
    def _sleep_with_stop(self, seconds: float):
        end = time.time()+seconds
        while not self.stop_event.is_set() and time.time() < end:
            time.sleep(0.2)
