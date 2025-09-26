from __future__ import annotations

import os
import time
import json
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


def _cookie_to_dict(c: Cookie) -> dict:
    return {
        "version": c.version, "name": c.name, "value": c.value,
        "port": c.port, "port_specified": c.port_specified,
        "domain": c.domain, "domain_specified": c.domain_specified,
        "domain_initial_dot": c.domain_initial_dot,
        "path": c.path, "path_specified": c.path_specified,
        "secure": c.secure, "expires": c.expires,
        "discard": c.discard, "comment": c.comment, "comment_url": c.comment_url,
        "rest": getattr(c, "_rest", {}), "rfc2109": c.rfc2109
    }


def _dict_to_cookie(d: dict) -> Cookie:
    return Cookie(
        version=d.get("version", 0), name=d["name"], value=d["value"],
        port=d.get("port"), port_specified=d.get("port_specified", False),
        domain=d.get("domain", ""), domain_specified=d.get("domain_specified", False),
        domain_initial_dot=d.get("domain_initial_dot", False),
        path=d.get("path", "/"), path_specified=d.get("path_specified", True),
        secure=d.get("secure", False), expires=d.get("expires"),
        discard=d.get("discard", True), comment=d.get("comment"),
        comment_url=d.get("comment_url"), rest=d.get("rest", {}), rfc2109=d.get("rfc2109", False)
    )


def _get_cookie_jar(api) -> Optional[object]:
    if not api:
        return None
    jar = getattr(api, "cookie_jar", None)
    if jar is None:
        rest = getattr(api, "rest_client", None)
        jar = getattr(rest, "cookie_jar", None)
    return jar


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
        if not code or not self._auth:
            return False
        code = code.strip()

        # 1) まずメールコード (6桁想定) を優先して試す
        if self._verify_email_code_fallback(code) and self._post_2fa_fixup():
            log.info("メールコード認証成功")
            return True

        # 2) ダメなら TOTP として試す
        if self._verify_totp_fallback(code) and self._post_2fa_fixup():
            log.info("TOTP 認証成功")
            return True

        log.error("2FAコード検証に失敗 (不一致/期限切れ/SDKメソッド不整合の可能性)")
        return False

    def _verify_email_code_fallback(self, code: str) -> bool:
        try:
            payload = TwoFactorEmailCode(code=code)
        except Exception:
            payload = {"code": code}  # モデル未一致版に保険

        candidates = [
            ("verify2_fa_email_code", {"two_factor_email_code": payload}),
            ("verify_two_factor_email_code", {"two_factor_email_code": payload}),
            ("verify2_fa_email_code", {"two_factor_email_code": {"code": code}}),
            ("verify_two_factor_email_code", {"two_factor_email_code": {"code": code}}),
        ]
        return self._try_methods(self._auth, candidates)

    def _verify_totp_fallback(self, code: str) -> bool:
        try:
            payload = TwoFactorAuthCode(code=code)
        except Exception:
            payload = {"code": code}

        candidates = [
            ("verify2_fa", {"two_factor_auth_code": payload}),
            ("verify_two_factor", {"two_factor_auth_code": payload}),
            ("verify2_fa", {"two_factor_auth_code": {"code": code}}),
            ("verify_two_factor", {"two_factor_auth_code": {"code": code}}),
        ]
        return self._try_methods(self._auth, candidates)

    # --- 新規: 2FA 成功後に「本当にセッション確立しているか」を確定させる ---
    def _post_2fa_fixup(self) -> bool:
        """
        2FA検証後にセッションが確立しているかを二重に確認:
        1) get_current_user() が成功するか
        2) CookieJar に 'auth' / 'authtoken' が存在するか
        OKなら finalize_auth() 済みで True
        """
        # まず current_user で 200 を確認
        if not self._try_get_current_user():
            return False

        # Cookie に auth が入っているかを確認
        tok = self.get_auth_token()
        if not tok:
            log.debug("2FA後に auth cookie が見つかりません")
            return False

        self._finalize_auth()
        return True

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

    def load_cookies(self, path: str) -> bool:
        if not os.path.exists(path):
            log.info("Cookie file not found: %s", path)
            return False
        try:
            obj = json.loads(open(path, "r", encoding="utf-8").read())
            cookies = obj.get("cookies", [])
        except Exception as e:
            log.warning("Cookie file read failed: %s", e)
            return False

        # ApiClient 準備 + UA を必ず入れる（WAF回避に必須）
        self._api = ApiClient(self._conf)
        try:
            self._api.set_default_header("User-Agent", self._conf.user_agent)
        except AttributeError:
            if hasattr(self._api, "default_headers"):
                self._api.default_headers["User-Agent"] = self._conf.user_agent

        jar = _get_cookie_jar(self._api)
        if not jar:
            log.warning("No cookie jar on ApiClient")
            return False
        try:
            # いったん既存をクリア（安全のため例外を握りつぶす）
            jar.clear()
        except Exception:
            pass

        # ユーティリティ：同一 cookie が jar に存在するか判定
        def jar_has_cookie(name, domain, value):
            for cc in jar:
                try:
                    if (getattr(cc, "name", "").lower() == (name or "").lower() and
                        getattr(cc, "domain", "") == domain and
                            getattr(cc, "value", None) == value):
                        return True
                except Exception:
                    continue
            return False

        has_auth = False
        # 読み込んだ cookie を入れていく（domain が無い等のケースに耐える）
        for d in cookies:
            try:
                # ドメインが無い/空の場合はAPIドメインをデフォルトとする
                dom = d.get("domain") or "api.vrchat.cloud"
                d_copy = dict(d)
                d_copy["domain"] = dom
                c = _dict_to_cookie(d_copy)
                if not jar_has_cookie(c.name, c.domain, c.value):
                    jar.set_cookie(c)
                if (c.name or "").lower() in ("auth", "authtoken") and c.value:
                    has_auth = True
            except Exception as e:
                log.debug("set_cookie failed (skipped): %s", e)

        if not has_auth:
            log.info("Cookie file has no 'auth'/'authtoken'; cannot resume.")
            return False

        # ドメイン不一致対策：auth を必要ドメインに複製（ただし重複は作らない）
        try:
            from http.cookiejar import Cookie
            wanted = {"api.vrchat.cloud", ".vrchat.com", "vrchat.com"}

            def ensure_cookie(name: str):
                # jar内の同名クッキーの値を収集（複数可）
                values = [
                    c.value for c in jar
                    if getattr(c, "name", "").lower() == name.lower() and c.value
                ]
                for value in values:
                    for dom in wanted:
                        exists = any(
                            (getattr(cc, "name", "").lower() == name.lower()
                             and getattr(cc, "domain", "") == dom
                             and getattr(cc, "value", None) == value)
                            for cc in jar
                        )
                        if exists:
                            continue
                        dup = Cookie(
                            version=0, name=name, value=value,
                            port=None, port_specified=False,
                            domain=dom, domain_specified=True,
                            domain_initial_dot=dom.startswith("."),
                            path="/", path_specified=True,
                            secure=True,  # HTTPSのみに送信
                            expires=None, discard=False,
                            comment=None, comment_url=None, rest={}, rfc2109=False
                        )
                        try:
                            jar.set_cookie(dup)
                            log.debug("duplicated cookie %s for %s", name, dom)
                        except Exception as e:
                            log.debug("failed to set duplicated %s for %s: %s", name, dom, e)

            # ★ポイント：auth だけでなく twoFactorAuth も複製
            ensure_cookie("auth")
            ensure_cookie("authtoken")     # 互換名も拾う
            ensure_cookie("twoFactorAuth")  # これが無いと毎回OTPになる
        except Exception as e:
            log.debug("cookie domain duplication failed: %s", e)
        # 有効性検証
        self._auth = authentication_api.AuthenticationApi(self._api)
        try:
            me = self._auth.get_current_user()  # type: ignore[union-attr]
            log.info("クッキー再開ログイン成功: %s", getattr(me, "display_name", "me"))
            self._finalize_auth()
            return True
        except Exception as e:
            log.info("Cookie resume failed; fallback to normal login: %s", e)
            return False

    def save_cookies(self, path: str) -> bool:
        jar = _get_cookie_jar(self._api)
        if not jar:
            return False
        data = []
        for c in jar:
            try:
                data.append(_cookie_to_dict(c))
            except Exception:
                # 保険: 個々のcookie変換で失敗しても続ける
                continue
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"cookies": data, "ts": int(time.time())}, f)
        # 保存後に auth を含むかチェック（auth or authtoken）
        has_auth = any((c.get("name", "").lower() in (
            "auth", "authtoken") and c.get("value")) for c in data)
        log.info("Saved session cookies: %s (count=%d, has_auth=%s)", path, len(data), has_auth)
        return True

    def clear_cookie_file(self, path: str) -> None:
        try:
            if os.path.exists(path):
                os.remove(path)
                log.info("Cleared cookie file: %s", path)
        except Exception:
            pass
