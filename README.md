# Traework GitHub 自动签到

每天定时在 GitHub Actions 上执行 Traework 签到，并将结果推送到飞书。

## 部署步骤

1. 在 GitHub 创建仓库，将本目录所有文件推送到仓库。
2. 在仓库 `Settings → Secrets and variables → Actions` 添加：
   - `TRAE_SESSION`：Traework accessToken
   - `TRAE_DEVICE_ID`：设备 ID
   - `FEISHU_WEBHOOK_URL`：飞书自定义机器人 Webhook
3. 在本机运行 `scripts/traework-refresh.sh` 获取 `TRAE_SESSION` 和 `TRAE_DEVICE_ID`，填入 Secret。
4. 在飞书群添加自定义机器人，复制 Webhook URL 填入 `FEISHU_WEBHOOK_URL`。
5. 手动触发一次 `Traework Daily Checkin` 工作流，验证。

## 文件说明

- `.github/workflows/traework-checkin.yml`：GitHub Actions 工作流
- `scripts/traework_checkin.py`：签到主逻辑
- `scripts/feishu_notify.py`：飞书推送
- `scripts/traework-refresh.sh`：本机凭证导出脚本（不要提交输出）

## 注意

- 本方案假设 Traework 签到接口为 `https://api.trae.cn/trae/api/v2/ug/checkin_credits/*`，如实际接口不同，请修改 `traework_checkin.py` 中的 `BASE` 和路径。
- 猫猫旅行模块为占位，Traework 暂无此功能。
