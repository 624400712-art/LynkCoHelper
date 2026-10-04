# -*- coding: utf-8 -*-
"""
吉利 GRIC 网关（gric-hf-api.geely.com）X-SIGNATURE v2.1 签名模块 —— 独立于领克原生签名体系。

逆向结论（2026-10-01，Android 内存取证 + iOS HAR 双重验证，32/32 请求通过）：

    X-SIGNATURE = Base64( HMAC-SHA256(key=GRIC_SECRET, msg=STS) )

    STS（string-to-sign）各行以 "\\n" 连接、无尾随换行：
        accept-language:<值>                     ← 固定参与
        authorization:<JWT>                      ← 固定参与（与旧结论不同，签名包含 JWT！）
        <全部 x-* 请求头，按头名升序，"name:value">
        <Base64(MD5(body))>                      ← 仅当 body 非空
        <query string 原样>                      ← 仅当 URL 带 query（如 os=1）
        <HTTP 方法，如 POST/GET>
        <path（不含 query）>

    密钥 GRIC_SECRET 为 App 运行时解密的静态常量，iOS/Android 同值。
    密钥不硬编码在代码里：仅从环境变量 GEELY_GRIC_SECRET 或
    geely_env.json（不入 git）读取，缺失时抛出明确错误。

独立配置文件 geely_env.json（已入 .gitignore，参考 geely_env.json.example；环境变量优先级更高）：
    {
      "gricSecret": "HMAC 密钥（必填，不入 git，经环境变量 GEELY_GRIC_SECRET 或本文件提供）",
      "authorization": "JWT（Bearer token，gric-mid-inner get/token 接口换取）",
      "app": {"appId": "GEELYCNCH001M0001", "appVersion": "4.2.6"},
      "device": {"id": "...", "brand": "google", "model": "sdk_gphone64_arm64",
                  "osVersion": "Android 13 (API 33)", "platform": "Android"},
      "vehicle": {"identifier": "AES 加密的 VIN（密文 Base64，勿填明文）",
                  "series": "Base64(车型代码)", "brand": "LYNKCO"}

    vehicle 字段获取（按优先级）：
    a) 自动引导（推荐，免堆提取）：python3 geely_gric.py vehicle-init
       favorite-vehicles（无需车辆头）查 VIN/seriesCode 明文 -> 本地 AES 加密 VIN 得 identifier
       -> Base64(seriesCode) 得 series -> 写回 geely_env.json；
       加密口径：X-VEHICLE-IDENTIFIER = Base64(AES-128-CBC-PKCS5(key/IV 见 _VEH_AES_*，
       App 内静态常量，可经 geely_env.json 的 vehicle.aesKey/aesIv 或环境变量
       GEELY_VEH_AES_KEY/GEELY_VEH_AES_IV 覆盖，防算法升级）)
    b) 堆提取（备用，密文长期稳定）：python3 tools/extract_gric_key.py vehicle --write
    }

环境变量：GEELY_GRIC_SECRET / GEELY_GRIC_JWT / GEELY_DEVICE_ID / GEELY_VEHICLE_IDENTIFIER / GEELY_VEHICLE_SERIES /
GEELY_VEH_AES_KEY / GEELY_VEH_AES_IV

JWT 自动获取（auth 链路，免抓包）：
    1. 领克登录态（env.json 的 token/refreshToken，复用 lynkco_login.load_token）
       GET app-services.lynkco.com.cn/auth/oauth2/access-code?scope=openid&state=vehiclenet
       （原生 x-ca-* 签名 + token + appsecret 头）→ data.accessCode（一次性、短时效）
    2. POST gric-api.geely.com/ms-midground-user/api/v1.0/user/auth/get/token
       BODY {"authCode":..., "identityType":"1"}，GRIC 签名（此请求无 authorization 头）
       → JWT（约 7 天有效，成功后写回 geely_env.json 的 authorization/device.id）
    3. GET gric-api.geely.com/ms-vehicle-core/api/v1.0/vehicle/favorite-vehicles
       （无需车辆头）→ vin / seriesCode 等车辆明文信息

    命令行：python3 geely_gric.py auth   # 执行 1+2 并写回；load_jwt() 会自动调用

    已端到端验证（2026-10-02）：auth → favorite/hb/detail 全 200。两个实测坑：
    a) lynkcoAppsecret 必须与抓包逐字节一致——曾因第 75 位一个字符笔误，
       access-code 固定返回 HTTP 500 AIOOBE:3（服务端解析半途数组越界，
       与 token/date/请求头均无关），配置时务必从抓包原值完整复制；
    b) get/token 的租户头见 _DEFAULTS 注释（GEELY/LYNKCO 差异）。

完整逆向过程不随脚本发布；密钥失效后需按抓包重新提取并更新配置。
"""
import base64
import hashlib
import hmac
import json
import os
import time
import uuid

import requests

from lynkco_common import env_value

BASE_URL = "https://gric-hf-api.geely.com"
# 部分接口路由在另一网关域名（抓包确认 favorite-vehicles / get/token 走 gric-api）
BASE_URL_API = "https://gric-api.geely.com"
LYNKCO_ACCESS_CODE_URL = "https://app-services.lynkco.com.cn/auth/oauth2/access-code"
LYNKCO_ACCESS_CODE_PATH = "/auth/oauth2/access-code"
GRIC_GET_TOKEN_PATH = "/ms-midground-user/api/v1.0/user/auth/get/token"
ENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "geely_env.json")

DEFAULT_TIMEOUT = int(env_value("GEELY_TIMEOUT", "30"))
DEFAULT_RETRIES = int(env_value("GEELY_RETRIES", "2"))

# 非敏感公共字段默认值（密钥不硬编码：从环境变量/geely_env.json 读取）
# ⚠ tenantId/salesPlatform 必须是 LYNKCO：get/token 在 GEELY 租户下会把 authCode
# 解析成另一个用户身份（实测返回空 accessToken + 陌生 userId），只有 LYNKCO
# 租户能解析出绑车账号并发 JWT（与 JWT aud=auth_client_lynkco_phone 一致）。
_DEFAULTS = {
    "appId": "GEELYCNCH001M0001",
    "appVersion": "4.2.6",
    "tenantId": "LYNKCO",
    "salesPlatform": "LYNKCO",
    "tspPlatform": "2",
    "vehicleBrand": "LYNKCO",
}

# X-VEHICLE-IDENTIFIER 的 AES 加密口径（2026-10-02 双向验证命中）：
#   identifier = Base64(AES-128-CBC-PKCS5(key, iv, VIN))
# 密钥/IV 为 App 内静态常量（与 appId 同级别的设备无关常量，故内置默认；
# 支持经 geely_env.json 的 vehicle.aesKey/aesIv 或环境变量覆盖，防算法升级）。
_VEH_AES_KEY = "2cd8bafdd1c4789a"
_VEH_AES_IV = "765a61ce8a29a2e1"


def encrypt_vehicle_identifier(vin: str, key: str = None, iv: str = None) -> str:
    """由 VIN 明文计算 X-VEHICLE-IDENTIFIER 头的密文（Base64）。

    口径：AES-128-CBC-PKCS5Padding，key/iv 均为 16 字符 ASCII 字符串。
    依赖 pycryptodome（requirements.txt 已声明）。服务端若报
    "Decrypt X-VEHICLE-IDENTIFIER failed"，说明密钥已轮换：可用
    tools/extract_gric_key.py vehicle --write 从 App 堆中提取现成密文，
    或重新逆向密钥后经 geely_env.json 的 vehicle.aesKey/aesIv 覆盖。
    """
    try:
        from Crypto.Cipher import AES
    except ImportError:
        raise RuntimeError("缺少 pycryptodome（VIN 加密需要）：请 pip install pycryptodome")

    veh_cfg = _load_config().get("vehicle", {})
    key = (key or env_value("GEELY_VEH_AES_KEY") or veh_cfg.get("aesKey")
           or _VEH_AES_KEY).encode()
    iv = (iv or env_value("GEELY_VEH_AES_IV") or veh_cfg.get("aesIv")
          or _VEH_AES_IV).encode()
    if len(key) != 16 or len(iv) != 16:
        raise RuntimeError(f"VIN 加密 key/iv 必须各为 16 字节（当前 {len(key)}/{len(iv)}）")

    data = vin.encode()
    pad = 16 - len(data) % 16
    return base64.b64encode(AES.new(key, AES.MODE_CBC, iv).encrypt(data + bytes([pad]) * pad)).decode()


def _load_config() -> dict:
    """读取 geely_env.json（不存在则返回空 dict）。"""
    if not os.path.exists(ENV_FILE):
        return {}
    try:
        with open(ENV_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _get(name: str, env_var: str = "", section: str = "") -> str:
    """取值优先级：环境变量 > geely_env.json > 内置默认值（仅密钥/公共字段有默认）。"""
    value = env_value(env_var) if env_var else None
    if not value and section:
        value = _load_config().get(section, {}).get(name)
    if not value:
        value = _load_config().get(name)
    if not value:
        value = _DEFAULTS.get(name, "")
    return value


def build_gric_signature(method: str, path: str, headers: dict, body: str = "",
                         query: str = "", secret: str = None) -> str:
    """
    计算 GRIC X-SIGNATURE v2.1。

    参数：
        method:  HTTP 方法（POST/GET）
        path:    请求路径（不含 query），如 /ms-app-online-center/api/v2.0/app/hb
        headers: 实际发送的全部请求头（小写键名），参与签名的为
                 accept-language / authorization / 所有 x-*（不含 x-signature 自身）
        body:    请求体字符串；非空时参与签名（Base64(MD5(body))）
        query:   URL query string 原样（如 os=1）；非空时作为独立行参与签名
        secret:  HMAC 密钥，默认从配置读取

    返回：Base64 签名字符串（放入请求头 X-SIGNATURE）

    密钥未配置时抛出 RuntimeError（配置方式：环境变量
    GEELY_GRIC_SECRET 或 geely_env.json 的 gricSecret 字段）。
    """
    secret = secret or _get("gricSecret", env_var="GEELY_GRIC_SECRET")
    if not secret:
        raise RuntimeError("缺少 GRIC 密钥：请设置环境变量 GEELY_GRIC_SECRET，"
                           "或在 geely_env.json（参考 geely_env.json.example）中填写 gricSecret 字段。")

    lines = []
    for name in ("accept-language", "authorization"):
        if name in headers and headers[name]:
            lines.append(f"{name}:{headers[name]}")
    for k in sorted(k for k in headers if k.startswith("x-") and k != "x-signature" and headers[k]):
        lines.append(f"{k}:{headers[k]}")
    if body:
        lines.append(base64.b64encode(hashlib.md5(body.encode()).digest()).decode())
    if query:
        lines.append(query)
    lines.append(method.upper())
    lines.append(path)

    msg = "\n".join(lines)
    digest = hmac.new(secret.encode(), msg.encode(), hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


class GrwbClient:
    """吉利 GRIC 网关客户端：自动组装备注头 + 签名 + 重试。"""

    def __init__(self, authorization: str = None, device: dict = None, vehicle: dict = None,
                 accept_language: str = "zh_CN", session: requests.Session = None):
        cfg = _load_config()
        dev = device or cfg.get("device", {})
        veh = vehicle or cfg.get("vehicle", {})

        self.authorization = (authorization
                              or env_value("GEELY_GRIC_JWT")
                              or load_jwt())

        self.device = {
            "id": env_value("GEELY_DEVICE_ID") or dev.get("id") or "",
            "brand": dev.get("brand", "google"),
            "model": dev.get("model", "sdk_gphone64_arm64"),
            "osVersion": dev.get("osVersion", "Android 13 (API 33)"),
            "platform": dev.get("platform", "Android"),
        }
        self.vehicle = {
            "identifier": env_value("GEELY_VEHICLE_IDENTIFIER") or veh.get("identifier") or "",
            "series": env_value("GEELY_VEHICLE_SERIES") or veh.get("series") or "",
            "brand": veh.get("brand", "LYNKCO"),
        }
        self.accept_language = accept_language
        self.app_id = _get("appId", env_var="GEELY_APP_ID", section="app")
        self.app_version = _get("appVersion", env_var="GEELY_APP_VERSION", section="app")
        self.session = session or requests.Session()

    def build_headers(self) -> dict:
        """组装完整请求头（含全部 x-* 签名头，不含 x-signature）。"""
        return _base_gric_headers(self.authorization, self.device, self.vehicle,
                                  self.accept_language, self.app_id, self.app_version)

    def request(self, method: str, path: str, body: str = "", query: str = "",
                retries: int = DEFAULT_RETRIES, timeout: int = DEFAULT_TIMEOUT,
                base_url: str = None) -> dict:
        """
        发起带签名的 GRIC 请求并返回 JSON。

        method/path/query/body 需与签名口径一致：
            query 传原始字符串（如 "os=1"），不是 dict。
        """
        url = (base_url or BASE_URL) + path + (("?" + query) if query else "")
        last_exc = None
        for attempt in range(retries + 1):
            headers = self.build_headers()
            headers["x-signature"] = build_gric_signature(method, path, headers, body, query)
            headers["accept"] = "application/json"
            if body:
                headers["content-type"] = "application/json"
            try:
                resp = self.session.request(method, url, headers=headers, data=body.encode() if body else None,
                                            timeout=timeout)
                if resp.status_code != 200:
                    print(f"[警告] GRIC {method} {path} 返回 HTTP {resp.status_code}: {resp.text[:200]}")
                return {"status": resp.status_code, "data": _safe_json(resp)}
            except (requests.exceptions.ReadTimeout, requests.exceptions.ConnectionError) as e:
                last_exc = e
                if attempt < retries:
                    print(f"[警告] 请求超时/断连，3秒后重试（第 {attempt + 1}/{retries} 次）: {e}")
                    time.sleep(3)
                else:
                    print(f"[警告] 请求重试 {retries} 次后仍失败: {e}")
        if last_exc:
            raise last_exc
        return {}

    # ---- 常用业务接口 ----

    def heartbeat(self) -> dict:
        """心跳 POST /ms-app-online-center/api/v2.0/app/hb。"""
        body = json.dumps({"deviceType": 1, "enableWakeUp": False, "hbType": 3,
                           "ts": int(time.time() * 1000)}, separators=(",", ":"))
        return self.request("POST", "/ms-app-online-center/api/v2.0/app/hb", body=body)

    def vehicle_detail(self) -> dict:
        """车辆详情 GET /ms-vehicle-account/api/v1.0/vehicle-detail?os=1。"""
        return self.request("GET", "/ms-vehicle-account/api/v1.0/vehicle-detail", query="os=1")

    def vehicle_status_latest(self) -> dict:
        """车辆状态 GET /ms-vehicle-status/api/v2.0/vehicle/status/latest。"""
        return self.request("GET", "/ms-vehicle-status/api/v2.0/vehicle/status/latest")

    def favorite_vehicles(self) -> dict:
        """收藏车辆 GET /ms-vehicle-core/api/v1.0/vehicle/favorite-vehicles（gric-api 网关）。

        无需 vehicle 头，响应含 vin / seriesCode / 车牌号等车辆明文信息。"""
        return self.request("GET", "/ms-vehicle-core/api/v1.0/vehicle/favorite-vehicles", base_url=BASE_URL_API)

    def message_count(self) -> dict:
        """消息数 GET /ms-app-message-center/api/v1.0/car/message/messageCount。"""
        return self.request("GET", "/ms-app-message-center/api/v1.0/car/message/messageCount")


# ------------------------------------------------------ JWT 获取（auth 链路） ----

def _base_gric_headers(authorization: str, device: dict, vehicle: dict,
                       accept_language: str, app_id: str, app_version: str) -> dict:
    """GRIC 公共请求头（不含 x-signature）；空值头不发（如换取 JWT 前无 authorization）。"""
    headers = {
        "accept-language": accept_language,
        "authorization": authorization,
        "x-api-signature-nonce": str(uuid.uuid4()),
        "x-api-signature-version": "2.1",
        "x-app-id": app_id,
        "x-app-version": app_version,
        "x-device-brand": device.get("brand", "google"),
        "x-device-id": device.get("id", ""),
        "x-device-model": device.get("model", "sdk_gphone64_arm64"),
        "x-device-os-version": device.get("osVersion", "Android 13 (API 33)"),
        "x-platform": device.get("platform", "Android"),
        "x-sales-platform": _get("salesPlatform"),
        "x-tenant-id": _get("tenantId"),
        "x-timestamp": str(int(time.time() * 1000)),
        "x-tsp-platform": _get("tspPlatform"),
        "x-vehicle-brand": vehicle.get("brand", "LYNKCO"),
        "x-vehicle-identifier": vehicle.get("identifier", ""),
        "x-vehicle-series": vehicle.get("series", ""),
    }
    return {k: v for k, v in headers.items() if v}


def get_access_code(token: str = None, session=None) -> str:
    """第 1 步：领克登录态换 accessCode（一次性、短时效）。

    复用 env.json 的领克 token（token 不传则调 lynkco_login.load_token 自动续期），
    走原生 x-ca-* 签名 + appsecret 头（appsecret 从 geely_env.json 的
    lynkcoAppsecret 字段或环境变量 GEELY_LYNKCO_APPSECRET 读取）。
    """
    import lynkco_common
    from lynkco_login import load_token

    if token is None:
        token = load_token()
    if not token.startswith("bearer"):
        token = f"bearer{token}"

    appsecret = _get("lynkcoAppsecret", env_var="GEELY_LYNKCO_APPSECRET")
    if not appsecret:
        raise RuntimeError("缺少 appsecret 头的值：请在 geely_env.json 中配置 lynkcoAppsecret 字段"
                           "（从 App 抓包的 access-code 请求头提取）")

    query = {"scope": "openid", "state": "vehiclenet"}
    headers = lynkco_common.build_native_signature(
        "GET", LYNKCO_ACCESS_CODE_PATH, query=query,
        accept="application/json; charset=utf-8",
        content_type="application/x-www-form-urlencoded; charset=utf-8",
        signature_headers_order="x-ca-nonce,x-ca-timestamp,x-ca-key",
    )
    headers.update({
        "appsecret": appsecret,
        "token": token,
        "ca_version": "1",
        "appVersionCode": lynkco_common.APP_VERSION,
        "appVersionName": lynkco_common.APP_BUILD,
        **lynkco_common.build_native_app_headers(token=token),
    })

    sess = session or requests.Session()
    resp = sess.get(LYNKCO_ACCESS_CODE_URL, params=query, headers=headers, timeout=DEFAULT_TIMEOUT)
    data = _safe_json(resp)
    access_code = None
    if isinstance(data, dict) and isinstance(data.get("data"), dict):
        access_code = data["data"].get("accessCode")
    if resp.status_code != 200 or not access_code:
        detail = json.dumps(data, ensure_ascii=False)[:300] if isinstance(data, dict) else str(data)[:300]
        raise RuntimeError(f"access-code 获取失败（HTTP {resp.status_code}）: {detail}\n"
                           "常见原因：领克 token/refreshToken 已过期（refreshToken 约 30 天），"
                           "需重新登录领克（lynkco_login.py 的短信验证码流程）")
    return access_code


def get_gric_jwt(auth_code: str, device: dict = None, session=None) -> str:
    """第 2 步：accessCode 换 GRIC JWT（约 7 天有效）。

    POST gric-api.geely.com/ms-midground-user/api/v1.0/user/auth/get/token
    BODY {"authCode":..., "identityType":"1"}
    此请求无 authorization 头（JWT 尚未持有），STS 规则相同。
    已验证（2026-10-02）：tenantId=LYNKCO 时返回正确 userId + 有效 JWT；
    tenantId=GEELY 时 HTTP 200/code=0 但 accessToken 为空（authCode 被解析成
    另一个未绑车身份）——遇到空 token 先检查租户头。
    """
    cfg = _load_config()
    dev = device or cfg.get("device", {})
    dev = {
        "id": env_value("GEELY_DEVICE_ID") or dev.get("id") or "",
        "brand": dev.get("brand", "google"),
        "model": dev.get("model", "sdk_gphone64_arm64"),
        "osVersion": dev.get("osVersion", "Android 13 (API 33)"),
        "platform": dev.get("platform", "Android"),
    }
    headers = _base_gric_headers("", dev, cfg.get("vehicle", {}), "zh_CN",
                                 _get("appId", env_var="GEELY_APP_ID", section="app"),
                                 _get("appVersion", env_var="GEELY_APP_VERSION", section="app"))
    body = json.dumps({"authCode": auth_code, "identityType": "1"}, separators=(",", ":"))
    headers["x-signature"] = build_gric_signature("POST", GRIC_GET_TOKEN_PATH, headers, body)
    headers["accept"] = "application/json"
    headers["content-type"] = "application/json"

    sess = session or requests.Session()
    resp = sess.post(BASE_URL_API + GRIC_GET_TOKEN_PATH, headers=headers,
                     data=body.encode(), timeout=DEFAULT_TIMEOUT)
    data = _safe_json(resp)
    if not isinstance(data, dict):
        raise RuntimeError(f"get/token 响应异常（HTTP {resp.status_code}）: {str(data)[:300]}")
    if resp.status_code != 200 or data.get("code") not in ("0", 0, "success"):
        raise RuntimeError(f"get/token 换取 JWT 失败（HTTP {resp.status_code}）: "
                           f"{json.dumps(data, ensure_ascii=False)[:300]}")
    payload = data.get("data") or {}
    jwt = payload.get("token") or payload.get("accessToken") or payload.get("access_token")
    if not jwt:
        raise RuntimeError(f"get/token 响应中未找到 token 字段: {json.dumps(data, ensure_ascii=False)[:300]}")
    return jwt


def _jwt_exp(authorization: str) -> int:
    """解 JWT payload 的 exp（失败返回 0）。"""
    try:
        part = authorization.split(".")[1]
        part += "=" * (-len(part) % 4)
        return int(json.loads(base64.urlsafe_b64decode(part)).get("exp", 0))
    except Exception:
        return 0


def refresh_gric_jwt(token: str = None, device_id: str = None, write: bool = True) -> str:
    """执行完整 auth 链路（第 1+2 步）并写回 geely_env.json。"""
    access_code = get_access_code(token=token)
    print(f"[信息] accessCode 获取成功: {access_code[:8]}...")
    device = None
    if device_id:
        device = {"id": device_id, "brand": "google", "model": "sdk_gphone64_arm64",
                  "osVersion": "Android 13 (API 33)", "platform": "Android"}
    jwt = get_gric_jwt(access_code, device=device)
    exp = _jwt_exp(jwt)
    print(f"[信息] JWT 换取成功，有效期至 {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(exp))}")
    if write:
        cfg = _load_config()
        cfg["authorization"] = jwt
        if device_id:
            cfg.setdefault("device", {})["id"] = device_id
        with open(ENV_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        print(f"[信息] 已写回 {ENV_FILE} 的 authorization 字段")
    return jwt


def load_jwt() -> str:
    """JWT 获取优先级：环境变量 GEELY_GRIC_JWT > geely_env.json 缓存（未过期）> 自动续期。"""
    env_jwt = env_value("GEELY_GRIC_JWT")
    if env_jwt:
        return env_jwt.strip()
    cached = _load_config().get("authorization", "")
    if cached and _jwt_exp(cached) > time.time() + 60:
        return cached
    return refresh_gric_jwt()


def bootstrap_vehicle(write: bool = True) -> dict:
    """车辆配置自动引导：favorite-vehicles 查 VIN/seriesCode -> 本地算密文 -> 写回。

    免堆提取：favorite-vehicles 不需要车辆头，返回的 vin 明文经
    encrypt_vehicle_identifier() 本地加密即得 identifier，seriesCode 经
    Base64 即得 series。多辆车时取 defaultCarFlag=true 的那辆。
    返回 {"identifier", "series", "vin", "seriesCode", "plateNo"}。
    """
    client = GrwbClient(vehicle={"identifier": "", "series": "", "brand": "LYNKCO"})
    r = client.favorite_vehicles()
    items = (r["data"].get("data") or []) if isinstance(r.get("data"), dict) else []
    if r.get("status") != 200 or not items:
        raise RuntimeError(f"favorite-vehicles 查询失败: {json.dumps(r, ensure_ascii=False)[:300]}")
    car = next((v for v in items if v.get("defaultCarFlag")), items[0])
    vin = car.get("vin") or ""
    series_code = car.get("seriesCode") or ""
    if not vin:
        raise RuntimeError(f"favorite-vehicles 响应中没有 vin 字段: {json.dumps(car, ensure_ascii=False)[:300]}")

    identifier = encrypt_vehicle_identifier(vin)
    series = base64.b64encode(series_code.encode()).decode() if series_code else ""
    if write:
        cfg = _load_config()
        vehicle_config = dict(cfg.get("vehicle") or {})
        vehicle_config.update(identifier=identifier, series=series)
        vehicle_config.setdefault("brand", "LYNKCO")
        cfg["vehicle"] = vehicle_config
        with open(ENV_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        print(f"[信息] 已写回 {ENV_FILE} 的 vehicle 字段")
    print(f"[信息] 车辆: {car.get('plateNo')} {car.get('modelName')} VIN={vin} "
          f"seriesCode={series_code} identifier={identifier[:12]}...")
    return {"identifier": identifier, "series": series, "vin": vin,
            "seriesCode": series_code, "plateNo": car.get("plateNo")}


def _safe_json(resp) -> object:
    try:
        return resp.json()
    except ValueError:
        return resp.text[:500]


def _mask(data):
    """日志脱敏：userId/openId 等敏感字段。"""
    keys = {"userid", "user_id", "openid", "open_id", "accountid", "account_id"}
    if isinstance(data, dict):
        return {k: ("***" if k.replace("-", "_").lower() in keys and v else _mask(v))
                for k, v in data.items()}
    if isinstance(data, list):
        return [_mask(x) for x in data]
    return data


if __name__ == "__main__":
    import sys
    action = sys.argv[1] if len(sys.argv) > 1 else "hb"
    if action == "auth":
        # auth 链路：领克登录态 -> accessCode -> JWT（写回 geely_env.json），无需 GrwbClient
        jwt = refresh_gric_jwt()
        masked = f"{jwt[:20]}...{jwt[-10:]}"
        print(json.dumps({"authorization": masked, "expireAt": time.strftime(
            "%Y-%m-%d %H:%M:%S", time.localtime(_jwt_exp(jwt)))}, ensure_ascii=False))
    elif action == "vehicle-init":
        # 车辆配置自动引导：免堆提取，见 bootstrap_vehicle() 文档
        info = bootstrap_vehicle()
        print(json.dumps(info, ensure_ascii=False, indent=2))
    else:
        client = GrwbClient()
        actions = {
            "hb": client.heartbeat,
            "detail": client.vehicle_detail,
            "status": client.vehicle_status_latest,
            "favorite": client.favorite_vehicles,
            "msg": client.message_count,
        }
        if action not in actions:
            print(f"用法: python3 {sys.argv[0]} [auth|vehicle-init|hb|detail|status|favorite|msg]")
            sys.exit(1)
        print(json.dumps(_mask(actions[action]()), ensure_ascii=False, indent=2))
