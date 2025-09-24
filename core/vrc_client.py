# core/vrc_client.py
from __future__ import annotations

import logging
from typing import Optional, Dict, List

from urllib3.exceptions import HTTPError

log = logging.getLogger(__name__)

try:
    from vrchatapi import ApiClient, Configuration
    from vrchatapi.api import authentication_api, friends_api
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
            # 想定外はそのまま上げる
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
    def fetch_online_friends(self) -> List[Dict[str, str]]:
        """オンラインのフレンドだけ [{'id','name'}] を返す（簡易判定）"""
        if not self._authed or not self._friends:
            return []

        friends = self._friends.get_friends()  # type: ignore
        online: List[Dict[str, str]] = []
        for f in friends:
            fid = getattr(f, "id", None)
            name = getattr(f, "display_name", None) or getattr(f, "username", None) or str(fid)
            status = (getattr(f, "status", None) or getattr(f, "state", None) or "").lower()
            location = (getattr(f, "location", None) or "").lower()

            # ざっくり判定：status=online か、location が offline 以外ならオンライン扱い
            if status == "online" or (location and location != "offline"):
                online.append({"id": str(fid), "name": str(name)})

        return online

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
