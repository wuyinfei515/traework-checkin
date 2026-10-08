#!/usr/bin/env python3
"""将签到结果推送到飞书"""
import os, json, urllib.request

WEBHOOK = os.environ.get("FEISHU_WEBHOOK_URL", "")
if not WEBHOOK:
    print("FEISHU_WEBHOOK_URL 未配置，跳过飞书通知", flush=True)
    raise SystemExit(0)

try:
    with open("result.json", "r", encoding="utf-8") as f:
        result = json.load(f)
except Exception as e:
    result = {
        "checkin": {"success": False, "message": f"读取结果失败: {e}", "credits": 0},
        "travel": {"success": False, "message": "未知"}
    }

checkin = result.get("checkin", {})
travel = result.get("travel", {})

checkin_ok = checkin.get("success", False)
checkin_emoji = "✅" if checkin_ok else "❌"
checkin_text = checkin.get("message", "无消息")
credits = checkin.get("credits", 0)

travel_ok = travel.get("success", False)
travel_emoji = "🐱" if travel_ok else "⚠️"
travel_text = travel.get("message", "无消息")

payload = {
    "msg_type": "interactive",
    "card": {
        "header": {
            "title": {"tag": "plain_text", "content": "Traework 每日签到报告"},
            "template": "green" if checkin_ok else "red",
        },
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"**{checkin_emoji} 签到状态**\n{checkin_text}\n当前积分: {credits}",
                },
            },
            {"tag": "hr"},
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"**{travel_emoji} 猫猫旅行**\n{travel_text}",
                },
            },
        ],
    },
}

req = urllib.request.Request(
    WEBHOOK,
    data=json.dumps(payload).encode(),
    headers={"Content-Type": "application/json"},
    method="POST",
)
with urllib.request.urlopen(req, timeout=10) as resp:
    print(f"飞书推送: {resp.status}")
