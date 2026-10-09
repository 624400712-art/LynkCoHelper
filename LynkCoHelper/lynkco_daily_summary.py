# -*- coding: utf-8 -*-
"""每日汇总推送：合并当天签到、转发（分享）、AI 评论结果，通过 PushPlus 推送到微信。

由 daily-summary.yml 每天 21:30（北京时间）执行；读取仓库内
.daily_result.json（签到/转发，由 lynkco_daily_tasks.py 落盘）与
.comment_activity.json（评论活动，由 lynkco_comment.py 追加）。
"""
import json
from pathlib import Path

from lynkco_notify import send_pushplus_notification


def load_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def build_summary_markdown(daily, activity, target_day):
    lines = []
    lines.append("## 领克App · 今日汇总")
    lines.append(f"**日期**：{target_day}")

    if daily:
        lines.append("\n### ✅ 签到")
        if daily.get("already_signed"):
            lines.append("- 今日已签到，无需重复签到")
        elif daily.get("sign_success"):
            reward = daily.get("sign_reward")
            lines.append("- 签到成功" + (f"，奖励能量 **+{reward}**" if reward is not None else ""))
        else:
            lines.append(f"- 签到失败：{daily.get('sign_message') or '未知原因'}")
        if daily.get("continue_days") is not None:
            lines.append(f"- 连续签到：**{daily['continue_days']} 天**")
        if daily.get("sign_card") is not None:
            lines.append(f"- 签到卡剩余：**{daily['sign_card']} 张**")
        if daily.get("energy_before") != "?" and daily.get("energy_after") != "?":
            try:
                before = int(daily["energy_before"])
                after = int(daily["energy_after"])
                delta = after - before
                sign = "+" if delta >= 0 else ""
                lines.append(f"- 积分变化：**{sign}{delta}**（{before} → {after}）")
            except (TypeError, ValueError):
                lines.append(f"- 积分：{daily['energy_before']} → {daily['energy_after']}")

        lines.append("\n### 🔗 转发分享")
        if daily.get("share_ok"):
            share_title = daily.get("share_title")
            lines.append("- 分享上报成功" + (f"（{share_title}）" if share_title else ""))
        else:
            lines.append("- 分享未上报/失败")

    comments = (activity or {}).get(target_day) or []
    lines.append("\n### 💬 AI 评论")
    if comments:
        for c in comments:
            lines.append(f"- **{c.get('title', '领克动态')}**（{c.get('time', '')}）：{c.get('comment', '')}")
    else:
        lines.append("- 今日暂无评论发布")
    return "\n".join(lines)


def main():
    daily = load_json(".daily_result.json")
    activity = load_json(".comment_activity.json")

    # 无任何数据（例如部署初期/当日任务均未落盘）时不推送空汇总
    if not daily and not activity:
        print("仓库中暂无当日数据（daily / comment 记录均缺失），跳过推送。")
        return

    # 目标日期：优先取签到记录日期，否则取评论活动最新日期，否则当天北京日期
    target_day = daily.get("date") or ""
    if not target_day and activity:
        target_day = max(activity.keys())
    if not target_day:
        from datetime import datetime, timedelta, timezone
        target_day = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")

    markdown = build_summary_markdown(daily, activity, target_day)
    print("=== 汇总内容预览 ===")
    print(markdown)

    result = send_pushplus_notification(title="领克App · 每日汇总", markdown_body=markdown)
    if isinstance(result, dict):
        code = result.get("code")
        msg = result.get("msg")
    else:
        code, msg = "?", result
    print(f"PushPlus 推送结果 code={code} msg={msg}")
    if isinstance(result, dict) and result.get("code") != 200:
        raise SystemExit(f"PushPlus push failed: {result}")


if __name__ == "__main__":
    main()
