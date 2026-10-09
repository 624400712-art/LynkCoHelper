# -*- coding: utf-8 -*-
"""
领克App 每日任务编排入口（签到 + 分享 + 积分查询 + 结果通知）。

- run_daily_tasks()：编排"查积分 -> 签到 -> 分享 -> 再查积分对比"，返回结构化结果字典。
- run_and_notify()：加载 token -> 执行每日任务 -> 组装 Markdown -> 推送到 Bark。

签到/分享成功后 myEnergy 接口的积分有几秒异步延迟，故查询"之后积分"前会先 sleep。

用法:
    python3 lynkco_daily_tasks.py    # 执行每日任务（签到+分享）并推送结果
"""
import json
import os
import sys
import time

from lynkco_common import env_value, mask_sensitive
from lynkco_login import load_token
from lynkco_notify import (
    build_markdown_report,
    send_bark_notification,
    send_pushplus_notification,
)
from lynkco_sign import LynkCoSignClient
from lynkco_share import LynkCoShareClient

# 签到/分享完成后，等待多久再查询"之后积分"，单位秒。经真机验证 3~5 秒足够
# 让服务端把能量体变化同步到 myEnergy 接口，可通过环境变量覆盖。
ENERGY_REFRESH_DELAY_SECONDS = float(env_value("LYNKCO_ENERGY_DELAY", "5"))

EP_MY_ENERGY = "/app/energy/myEnergy"


def get_my_energy(client: LynkCoSignClient) -> dict:
    """查询当前账号的能量体积分（GET /app/energy/myEnergy，与签到共用同一网关/签名体系）。"""
    resp = client._request("GET", EP_MY_ENERGY)
    return resp.json()


def run_daily_tasks(token: str, do_share: bool = True) -> dict:
    """
    编排执行一次完整的"签到 + 分享"流程，返回汇总结果字典：
    {"energy_before", "day_info", "already_signed", "sign_result", "continue_info",
     "share_result", "energy_after"}（已签到/do_share=False 时对应字段为 None）。

    do_share 默认 True，供需要在测试/脚本中跳过分享的调用方使用；
    正式入口 run_and_notify() 始终以 do_share=True 调用。
    """
    result = {}

    sign_client = LynkCoSignClient(token)

    result["energy_before"] = get_my_energy(sign_client)

    day_info = sign_client.get_sign_day_info()
    result["day_info"] = day_info

    already_signed = (day_info.get("data") or {}).get("signStatus") == 1
    result["already_signed"] = already_signed

    if already_signed:
        result["sign_result"] = None
    else:
        result["sign_result"] = sign_client.do_sign()

    result["continue_info"] = sign_client.get_continue_days()

    if do_share:
        share_client = LynkCoShareClient(token)
        try:
            result["share_result"] = share_client.do_share()
        except Exception as e:
            result["share_result"] = {"ok": False, "detail": {"message": f"分享任务异常: {e}"}}
    else:
        result["share_result"] = None

    # 积分变化有异步延迟，等待片刻再查询，避免看到"没有变化"的假象。
    if not already_signed or (do_share and (result.get("share_result") or {}).get("ok")):
        time.sleep(ENERGY_REFRESH_DELAY_SECONDS)
    result["energy_after"] = get_my_energy(sign_client)

    return result


def run_and_notify() -> dict:
    """
    完整入口：加载 token -> 执行每日任务(run_daily_tasks) -> 组装 Markdown
    -> 推送到 Bark（lynkco_notify 模块）。

    每日任务固定包含"签到+分享"两项（分享接口每日有加分次数上限，重复
    调用不会重复加分，详见 docs/分享任务接口说明.md）。

    返回值同 run_daily_tasks()，并额外附带 "notify_result" 字段（Bark
    推送接口的响应，或 {"skipped": True}）。
    """
    token = load_token()

    print("=== 执行每日任务（签到+分享）===")
    result = run_daily_tasks(token, do_share=True)
    print(json.dumps(mask_sensitive(result), ensure_ascii=False, indent=2))

    markdown_body = build_markdown_report(result)
    print("\n=== 推送内容预览 ===")
    print(markdown_body)

    # 微信推送统一由每日汇总 workflow（daily-summary.yml）在当天任务结束后
    # 合并发送；这里只把当天签到/分享结果落盘到仓库，供汇总时读取。
    try:
        from datetime import datetime, timedelta, timezone
        beijing = datetime.now(timezone(timedelta(hours=8)))

        def _point(resp):
            return str((resp.get("data") or {}).get("point", "?"))

        daily_record = {
            "date": beijing.strftime("%Y-%m-%d"),
            "time": beijing.strftime("%H:%M"),
            "markdown": markdown_body,
            "already_signed": bool(result.get("already_signed")),
            "sign_success": bool((result.get("sign_result") or {}).get("success")),
            "sign_message": (result.get("sign_result") or {}).get("message", ""),
            # 能量奖励（能量体，来自签到响应 rewardEnergyNumber）
            "sign_reward": ((result.get("sign_result") or {}).get("data") or {}).get("rewardEnergyNumber"),
            "continue_days": ((result.get("continue_info") or {}).get("data") or {}).get("continueDays"),
            "sign_card": ((result.get("continue_info") or {}).get("data") or {}).get("signCardNumber"),
            "share_ok": bool((result.get("share_result") or {}).get("ok")),
            "share_title": (result.get("share_result") or {}).get("articleTitle", ""),
            # 积分（myEnergy 接口的 point 字段，签到+分享后的变化）
            "points_before": _point(result.get("energy_before") or {}),
            "points_after": _point(result.get("energy_after") or {}),
        }
        with open(".daily_result.json", "w", encoding="utf-8") as f:
            json.dump(daily_record, f, ensure_ascii=False, indent=2)
        print("=== 当日结果已落盘 .daily_result.json（汇总推送由 daily-summary 负责）===")
    except Exception as e:
        print(f"[警告] 当日结果落盘失败（不影响签到/分享结果）: {e}")

    result["notify_result"] = {"skipped": True}
    result["pushplus_result"] = {"skipped": True}
    return result


def main():
    try:
        run_and_notify()
    except RuntimeError as e:
        print(f"[错误] {e}")
        sys.exit(1)
    except Exception as e:
        print(f"[错误] 执行失败: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
