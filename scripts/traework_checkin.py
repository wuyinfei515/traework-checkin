#!/usr/bin/env python3
"""Traework 每日签到 —— 纯 HTTP，不依赖本地登录态"""
import os, sys, json, time
import urllib.request
import urllib.error

BASE = "https://api.trae.cn/trae/api/v2/ug/checkin_credits"
SESSION = os.environ.get("TRAE_SESSION", "")
DEVICE_ID = os.environ.get("TRAE_DEVICE_ID", "")

if not SESSION or not DEVICE_ID:
    print("缺少 TRAE_SESSION 或 TRAE_DEVICE_ID", file=sys.stderr)
    sys.exit(2)

HEADERS = {
    "Authorization": f"Bearer {SESSION}",
    "Content-Type": "application/json",
    "X-Device-Id": DEVICE_ID,
    "User-Agent": "TraeWork/1.0",
}

def post(path: str, payload: dict | None = None) -> dict:
    url = f"{BASE}/{path}"
    data = json.dumps(payload or {}).encode()
    req = urllib.request.Request(url, data=data, headers=HEADERS, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return {"_http_error": e.code, "_body": e.read().decode()[:500]}
    except Exception as e:
        return {"_error": str(e)}

def main():
    raw_log = {}
    result = {
        "checkin": {"success": False, "message": "", "credits": 0},
        "travel": {"success": False, "message": "未启用"}
    }

    # 1. 检查签到状态
    status_resp = post("status")
    raw_log["status_check"] = status_resp
    checked_in = status_resp.get("data", {}).get("checked_in", False)
    credits = status_resp.get("data", {}).get("credits", 0)

    if checked_in:
        msg = f"今日已签到，当前积分: {credits}"
        print(f"[SKIP] {msg}")
        result["checkin"] = {"success": True, "message": msg, "credits": credits}
        raw_log["action"] = "already_checked_in"
    else:
        # 2. 领取积分
        claim_resp = post("claim")
        raw_log["claim"] = claim_resp
        print(f"[CLAIM] 领取响应: code={claim_resp.get('code')}, msg={claim_resp.get('msg')}")

        # 3. 回查确认
        time.sleep(2)
        verify_resp = post("status")
        raw_log["verify"] = verify_resp
        verified = verify_resp.get("data", {}).get("checked_in", False)
        final_credits = verify_resp.get("data", {}).get("credits", 0)

        if verified:
            msg = f"签到成功，当前积分: {final_credits}"
            print(f"[VERIFY] {msg}")
            result["checkin"] = {"success": True, "message": msg, "credits": final_credits}
            raw_log["action"] = "claimed_and_verified"
        else:
            msg = f"签到失败，响应: {claim_resp.get('msg', '未知')}"
            print(f"[VERIFY] {msg}")
            result["checkin"] = {"success": False, "message": msg, "credits": final_credits}
            raw_log["action"] = "claim_failed"

    # 猫猫旅行模块（占位，Traework 暂无此功能）
    result["travel"] = {"success": True, "message": "Traework 暂无猫猫旅行模块，已跳过"}

    # 写入结果文件供飞书脚本读取
    with open("result.json", "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    # 输出脱敏日志
    print("::RAWLOG::" + json.dumps(raw_log, ensure_ascii=False))

    sys.exit(0 if result["checkin"]["success"] else 1)

if __name__ == "__main__":
    main()
