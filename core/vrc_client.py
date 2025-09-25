from __future__ import annotations

import logging
from typing import Optional, Dict, List, Set

from urllib3.exceptions import HTTPError
from http.cookiejar import Cookie

log = logging.getLogger(__name__)

try:
    from vrchatapi import ApiClient, Configuration
    from vrchatapi.api import authentication_api, friends_api
    try:
        from vrchatapi.api import favorites_api as favorites_api_mod
    except Exception:
        favorites_api_mod = None
    from vrchatapi.models.two_factor_email_code import TwoFactorEmailCode
    from vrchatapi.models.two_factor_auth_code import TwoFactorAuthCode
    from vrchatapi.exceptions import UnauthorizedException, ApiException
    HAS_SDK = True
except Exception:
    HAS_SDK = False


class HttpStatus(RuntimeError):
    def __init__(self, status: int, body: str):
        super().__init__(f"HTTP {status}: {body}")
        self.status = status
        self.body = body


def _unwrap_http_error(e: Exception) -> tuple[int | None, str]:
    s = str(e)
    # SDK 例外の中に "(403)" や response body が文字列で入ってくるケースが多い
    import re
    m = re.search(r"\((\d{3})\)", s)
    status = int(m.group(1))if m else None
    return status, s


class TwoFactorRequired(RuntimeError):
    """2FAが要求されたことを示すシグナル例外。"""


class UserAgentRejected(RuntimeError):
    """WAF が User-Agent 不備でリクエストを拒否した状態"""


class DnsResolutionFailed(RuntimeError):
    """DNS で api.vrchat.cloud を解決できなかった"""


def _get_any(obj, names, default=None):
    for n in names:
        if hasattr(obj, n):
            return getattr(obj, n)
    return default


class VRChatClient:
    """
    まずメールコードを検証し、ダメならTOTPを試す最小実装。
    - login(username, password, code=None, totp=None) ・・・ code はメールコード、totp はTOTP
    - fetch_online_friends() ・・・ [{'id','name'}]
    """

    def __init__(self, host: Optional[str] = None, user_agent: str = "vrcwatcher/0.1") -> None:
        if not HAS_SDK:
            raise RuntimeError("vrchatapi が見つかりません。`pip install vrchatapi` を実行してください。")

        conf = Configuration()
        if host:
            conf.host = host  # 例: "https://api.vrchat.cloud/api/1"（通常は未指定でOK）
        conf.user_agent = user_agent

        self._conf = conf
        self._api: Optional[ApiClient] = None
        self._auth: Optional[authentication_api.AuthenticationApi] = None  # type: ignore
        self._friends: Optional[friends_api.FriendsApi] = None  # type: ignore
        self._favorites = None
        self._authed: bool = False

    def is_authed(self) -> bool:
        return self._authed

    # ---------- 段階1: パスワードだけで試行（ここで2FAメールが送られる） ----------
    def login_start(self, username: str, password: str) -> bool:
        self._conf.username = username
        self._conf.password = password
        self._api = ApiClient(self._conf)

        try:
            self._api.set_default_header("User-Agent", self._conf.user_agent)
        except AttributeError:
            # set_default_header が無い版は default_headers を直接触る
            if hasattr(self._api, "default_headers"):
                self._api.default_headers["User-Agent"] = self._conf.user_agent

        self._auth = authentication_api.AuthenticationApi(self._api)
        log.debug("Using UA: %s", self._conf.user_agent)

        try:
            me = self._auth.get_current_user()  # type: ignore[union-attr]
            log.info("ログイン成功 (2FA不要) : %s", getattr(me, "display_name", "me"))
            self._finalize_auth()
            return True
        except Exception as e:
            status, body = _unwrap_http_error(e)
            log.warning("最初のログイン試行に失敗: %s", e)

            if status == 403 and "please identify yourself" in body.lower():
                raise UserAgentRejected("WAF により UA 不備として拒否されました。")from e
            if status in (401, 200) or "2fa" in body.lower():
                raise TwoFactorRequired("2FA が必要です。")from e
            raise

    # ---------- 段階2: コード提出（まずメール→だめならTOTP） ----------

    def submit_code(self, code: str) -> bool:
        if not code:
            return False
        # 1) メールコードとして検証
        if self._try_verify_email_code(code) and self._try_get_current_user():
            self._finalize_auth()
            log.info("メールコード認証")
            return True
        # 2) TOTPとして検証
        if self._try_verify_totp(code) and self._try_get_current_user():
            self._finalize_auth()
            log.info("TOTP認証成功")
            return True
        log.error("コード検証に失敗しました (不一致/期限切れの可能性)")
        return False

    # ----------------- Friends -----------------
    def fetch_online_friends(self, only_ids: Optional[Set[str]] = None) -> List[Dict[str, str]]:
        """オンラインのフレンドだけ [{'id','name'}] を返す（簡易判定）"""
        if not self._authed or not self._friends:
            return []

        all_friends = []
        offset = 0
        page_size = 100
        while True:
            try:
                page = self._friends.get_friends(n=page_size, offset=offset, offline=False)
            except Exception as e:
                log.debug("get_friends page fetch failed: %s", e)
                break
            if not page:
                break
            all_friends.extend(page)
            if len(page) < page_size:
                break
            offset += page_size

        online: List[Dict[str, str]] = []
        for f in all_friends:
            fid = getattr(f, "id", None)
            name = getattr(f, "display_name", None) or getattr(f, "username", None) or str(fid)
            status = (getattr(f, "status", None) or getattr(f, "state", None) or "")
            status = status.lower() if isinstance(status, str) else ""
            location = (getattr(f, "location", None) or "")
            location = location.lower() if isinstance(location, str) else ""
            is_online = (status == "online") or (location and location != "offline")
            if is_online:
                if only_ids is None or (fid and str(fid) in only_ids):
                    online.append({"id": str(fid), "name": str(name)})
        return online

    # ----------------- Favorites（お気に入りフレンド） -----------------
    def fetch_favorite_friend_ids(self, group_index: int) -> set[str]:
        """
        Favorite Friends 1~4 のいずれかに所属するフレンドの ID 集合を返す。
        group_index: 1..4
        ※ SDKの版によりプロパティ名が揺れるため動的に吸収。
        """
        ids: Set[str] = set()
        if not self._authed or not favorites_api_mod:
            if not favorites_api_mod:
                log.warning("FavoritesApi が見つかりません (SDKバージョン要確認) 。空集合を返します。")
                return ids

        if self._favorites is None:
            try:
                self._favorites = favorites_api_mod.FavoritesApi(self._api)
            except Exception as e:
                log.warning("FavoritesApi 初期化失敗: %s", e)
                return ids

        def _tag_matches_group(tag: str, gi: int) -> bool:
            t = (tag or "").lower()
            return any([
                f"group_{gi-1}" in t,
                f"favorite_friends_{gi}" in t,
                f"favorite-friends-{gi}" in t,
                (("friend" in t or "friends" in t) and "favorite" in t and str(gi) in t),
            ])

        offset = 0
        page_size = 100
        while True:
            try:
                page = self._favorites.get_favorites(n=page_size, offset=offset, type="friend")
            except Exception as e:
                log.debug("get_favorites 失敗: %s", e)
                break
            if not page:
                break

            for fav in page:
                tags = _get_any(fav, ["tags"], []) or []
                if any(_tag_matches_group(t, group_index)for t in tags):
                    # フレンド本体のID (favorite対象ID) を取り出す
                    fid = _get_any(fav, ["favorite_id", "favoriteId",
                                   "object_id", "objectId", "target_id", "targetId"])
                    # 万一上記が取れない版では "id" が対象IDのことも (そうでないことも) あるため最終手段に
                    if not fid:
                        fid = getattr(fav, "id", None)
                    if fid:
                        ids.add(str(fid))

            if len(page) < page_size:
                break
            offset += page_size
        log.info("Favorite Friends %d: %d users", group_index, len(ids))
        return ids

    # ----------------- internal helpers -----------------

    def _finalize_auth(self) -> None:
        self._friends = friends_api.FriendsApi(self._api)
        self._authed = True

    def _try_get_current_user(self) -> bool:
        try:
            me = self._auth.get_current_user()  # type: ignore[union-attr]
            log.debug("get_current_user OK: %s", getattr(me, "display_name", "me"))
            return True
        except Exception as e:
            log.debug("get_current_user NG: %s", e)
            return False

    def _try_verify_email_code(self, code: str) -> bool:
        if not self._auth:
            return False
        try:
            self._auth.verify2_fa_email_code(
                two_factor_email_code=TwoFactorEmailCode(code=code.strip())
            )
            return True
        except Exception as e:
            log.debug("verify2_fa_email_code 失敗: %s", e)
            return False

    def _try_verify_totp(self, code: str) -> bool:
        if not self._auth:
            return False
        try:
            self._auth.verify2_fa(
                two_factor_auth_code=TwoFactorAuthCode(code=code.strip())
            )
            return True
        except Exception as e:
            log.debug("verify2_fa 失敗: %s", e)
            return False

    @staticmethod
    def _try_methods(obj, candidates) -> bool:
        if not obj:
            return False
        for name, kwargs in candidates:
            fn = getattr(obj, name, None)
            if callable(fn):
                try:
                    fn(**kwargs)
                    return True
                except Exception as e:
                    log.debug("%s 失敗: %s", name, e)
        return False

    def get_auth_token(self) -> Optional[str]:
        """
        SDKが保持する Cookie から auth トークンを取り出す。
        例: Cookie(name="auth", value="xxxxxxxx", domain=".vrchat.com" or "api.vrchat.cloud")
        """
        api = getattr(self, "_api", None)
        if not api:
            return None

        # openapi-python-client の典型配置を両対応で探す
        jar = getattr(api, "cookie_jar", None)
        if jar is None:
            rest = getattr(api, "rest_client", None)
            jar = getattr(rest, "cookie_jar", None)

        if not jar:
            return None

        for c in jar:
            name = (c.name or "").lower()
            if name in ("auth", "authtoken"):
                return c.value
        return None

    def build_pipeline_ws_url(self, base: str = "wss://pipeline.vrchat.cloud/") -> Optional[str]:
        """
        旧来の形式: wss://pipeline.vrchat.cloud/?authToken=<token>
        ※  実環境によっては base が wss://vrchat.com/...のこともあるので、
            DevTools > Network > WS で確認して必要なら base を差し替えてください。
        """
        tok = self.get_auth_token()
        if not tok:
            return None
        return f"{base}?authToken={tok}"
