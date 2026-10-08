#!/usr/bin/env python3
"""Traework 每日签到 —— 纯 HTTP，浏览器免登录。

认证链路：用导出的登录 cookie 调用 GetUserToken 换取短期 JWT，
再用该 JWT 调用签到接口。cookie 有效期以周计，过期后需在已登录机器
重新导出（见 README / 本地 sync 脚本）。
"""
import os, sys, json, time
import urllib.request
import urllib.error

API = "https://api.trae.cn"
BASE = f"{API}/trae/api/v2/ug/checkin_credits"
GET_TOKEN = f"{API}/cloudide/api/v3/common/GetUserToken"

# TRAE_COOKIE 存放导出的 sess.json 原文（含 cookies + device_id）
RAW = os.environ.get("TRAE_COOKIE", "").strip().lstrip("\ufeff")
# 兼容旧的纯 JWT 方式（若未配置 cookie 则回退）
LEGACY_JWT = os.environ.get("TRAE_SESSION", "").strip().lstrip("\ufeff")
LEGACY_DEVICE = os.environ.get("TRAE_DEVICE_ID", "").strip().lstrip("\ufeff")


def cookie_header_for(cookies, host):
    parts = []
    for c in cookies:
        dom = (c.get("domain") or "").lstrip(".")
        if not dom:
            continue
        if host == dom or host.endswith("." + dom):
            parts.append("%s=%s" % (c.get("name"), c.get("value")))
    return "; ".join(parts)


def get_token(cookies):
    req = urllib.request.Request(
        GET_TOKEN, data=b"{}", method="POST",
        headers={"Content-Type": "application/json",
                 "Cookie": cookie_header_for(cookies, "api.trae.cn")},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        return (data.get("Result") or {}).get("Token"), data
    except Exception as e:
        return None, {"_error": str(e)}


def make_poster(jwt, device_id):
    headers = {
        "Authorization": f"Cloud-IDE-JWT {jwt}",
        "Content-Type": "application/json",
        "X-Device-Id": device_id,
        "User-Agent": "TraeWork/1.0",
    }

    def post(path):
        req = urllib.request.Request(f"{BASE}/{path}", data=b"{}",
                                     headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            return {"_http_error": e.code, "_body": e.read().decode()[:500]}
        except Exception as e:
            return {"_error": str(e)}

    return post


def main():
    raw_log = {}
    result = {
        "checkin": {"success": False, "message": "", "credits": 0},
        "travel": {"success": False, "message": "未启用"},
    }

    jwt = None
    device_id = None
    cookies = None

    if RAW:
        try:
            sess = json.loads(RAW)
        except Exception as e:
            print(f"[AUTH] TRAE_COOKIE 解析失败: {e}", file=sys.stderr)
            sess = {}
        cookies = sess.get("cookies") or []
        device_id = sess.get("device_id") or LEGACY_DEVICE
        if cookies:
            jwt, tok_resp = get_token(cookies)
            raw_log["get_token"] = {"ok": bool(jwt), "resp": tok_resp if not jwt else "token issued"}
            if not jwt:
                print("[AUTH] cookie 换取 JWT 失败，登录态可能已过期，请重新导出 session", file=sys.stderr)

    if not jwt and LEGACY_JWT:
        # 回退到旧的直接 JWT 方式
        jwt = LEGACY_JWT
        device_id = device_id or LEGACY_DEVICE
        raw_log["mode"] = "legacy_jwt"

    if not jwt or not device_id:
        msg = "缺少有效登录态（TRAE_COOKIE 已过期或未配置 TRAE_SESSION）"
        print(f"[AUTH] {msg}", file=sys.stderr)
        result["checkin"] = {"success": False, "message": msg, "credits": 0}
        raw_log["action"] = "no_auth"
        _finish(result, raw_log)
        sys.exit(2)

    raw_log.setdefault("mode", "cookie")
    post = make_poster(jwt, device_id)

    # 1. 检查签到状态（API 返回顶层字段）
    status_resp = post("status")
    raw_log["status_check"] = status_resp
    checked_in = status_resp.get("checked_in", False)
    credits = status_resp.get("credits", 0)

    if checked_in:
        msg = f"今日已签到，当前积分: {credits}"
        print(f"[SKIP] {msg}")
        result["checkin"] = {"success": True, "message": msg, "credits": credits}
        raw_log["action"] = "already_checked_in"
    else:
        # 2. 领取积分
        claim_resp = post("claim")
        raw_log["claim"] = claim_resp
        print(f"[CLAIM] 领取响应: code={claim_resp.get('code')}, message={claim_resp.get('message')}")

        # 3. 回查确认
        time.sleep(2)
        verify_resp = post("status")
        raw_log["verify"] = verify_resp
        verified = verify_resp.get("checked_in", False)
        final_credits = verify_resp.get("credits", 0)

        if verified:
            msg = f"签到成功，当前积分: {final_credits}"
            print(f"[VERIFY] {msg}")
            result["checkin"] = {"success": True, "message": msg, "credits": final_credits}
            raw_log["action"] = "claimed_and_verified"
        else:
            msg = f"签到失败: {claim_resp.get('message', claim_resp.get('_error', '未知'))}"
            print(f"[VERIFY] {msg}")
            result["checkin"] = {"success": False, "message": msg, "credits": final_credits}
            raw_log["action"] = "claim_failed"

    result["travel"] = {"success": True, "message": "Traework 暂无猫猫旅行模块，已跳过"}
    _finish(result, raw_log)
    sys.exit(0 if result["checkin"]["success"] else 1)


def _finish(result, raw_log):
    with open("result.json", "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print("::RAWLOG::" + json.dumps(raw_log, ensure_ascii=False))


if __name__ == "__main__":
    main()
