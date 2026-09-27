#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GLaDOS / Railgun 自动签到脚本（2026-09 修复版）

基于 Devilstore/Glados-Railgun-checkin，针对 2026 年 9 月 GLaDOS 会话变更做了适配。

修复点：
1. 适配新版 `gld:sess` Cookie（GLaDOS 已将签到会话从 koa:sess 迁移到 gld:sess）。
2. 新增 `GLADOS_USER_AGENT` 环境变量 —— GLaDOS 会据此识别"自动签到"，若不传可能被判为机器人。
3. 签到域名可配置（`GLADOS_DOMAINS`），默认 `glados.cloud`（GLaDOS 官方已迁移至此域名）。

用法（GitHub Actions 里由 workflow 注入 secrets，无需手动跑）：
    GLADOS_COOKIES       必填，Cookie 串，多账号用 & 连接
    GLADOS_EXCHANGE_PLAN  可选，plan100/plan200/plan500（默认不自动兑换）
    GLADOS_USER_AGENT     可选，浏览器 UA 字符串（建议填真实浏览器 UA）
    GLADOS_DOMAINS        可选，逗号分隔，默认 glados.cloud
    PUSHDEER_SENDKEY      可选，PushDeer 推送 key
"""
import os
import sys
import json
import logging
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Dict, List, Optional

import requests

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

DEFAULT_DOMAINS = ["glados.cloud"]
# GLaDOS 2026-09 起校验「设备指纹」：签到请求的 UA 必须与你登录时所用设备一致，
# 否则返回 code=4 device-mismatch / "Automated check-in detected"。
# 本账号实测登录设备为 macOS，故默认 UA 用 macOS；若你用其它设备登录，
# 请通过环境变量 GLADOS_USER_AGENT 覆盖为对应设备的 UA。
DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

EXCHANGE_PLANS = {
    "plan100": 100,
    "plan200": 200,
    "plan500": 500,
}
# 默认不自动兑换积分；如需开启，设置 GLADOS_EXCHANGE_PLAN=plan100/plan200/plan500


class Config:
    def __init__(self) -> None:
        raw = os.environ.get("GLADOS_COOKIES", "")
        # 多账号用 & 分隔；过滤空串
        self.cookies: List[str] = [c.strip() for c in raw.split("&") if c.strip()]
        self.exchange_plan = os.environ.get("GLADOS_EXCHANGE_PLAN", "")
        self.pushdeer_sendkey = os.environ.get("PUSHDEER_SENDKEY", "")
        self.user_agent = os.environ.get("GLADOS_USER_AGENT", DEFAULT_UA)
        domains_raw = os.environ.get("GLADOS_DOMAINS", ",".join(DEFAULT_DOMAINS))
        self.domains: List[str] = [d.strip() for d in domains_raw.split(",") if d.strip()]
        self.verbose = os.environ.get("GLADOS_VERBOSE", "false").lower() in ("1", "true", "yes")


# ---------------------------------------------------------------------------
# API 封装
# ---------------------------------------------------------------------------

class APIEndpoint(StrEnum):
    CHECKIN = "/api/user/checkin"
    STATUS = "/api/user/status"
    POINTS = "/api/user/points"
    EXCHANGE = "/api/user/exchange"


class API:
    def __init__(self, domain: str, user_agent: str) -> None:
        self.domain = domain
        self.user_agent = user_agent

    def _headers(self, cookie: str) -> Dict[str, str]:
        return {
            "origin": f"https://{self.domain}",
            "user-agent": self.user_agent,
            "cookie": cookie,
        }

    def _request(self, method: str, path: str, cookie: str, data: Optional[Dict] = None):
        url = f"https://{self.domain}{path}"
        if method.upper() == "GET":
            return requests.get(url, headers=self._headers(cookie), timeout=(60, 120))
        return requests.post(url, headers=self._headers(cookie), data=data, timeout=(60, 120))

    def get_status(self, cookie: str):
        try:
            r = self._request("GET", APIEndpoint.STATUS, cookie)
            data = r.json()
            left = (data.get("data") or {}).get("leftDays")
            if left is not None:
                return f"{int(float(left))} 天", data.get("code")
            return "未知", data.get("code")
        except Exception:
            return "查询失败", -1

    def checkin(self, cookie: str):
        try:
            r = self._request("POST", APIEndpoint.CHECKIN, cookie, data={"token": self.domain})
            data = r.json()
            return {
                "code": data.get("code", -2),
                "message": data.get("message", ""),
                "points": data.get("points", 0),
            }
        except Exception as e:
            return {"code": -1, "message": str(e), "points": 0}

    def get_points(self, cookie: str):
        try:
            r = self._request("GET", APIEndpoint.POINTS, cookie)
            data = r.json()
            p = data.get("points")
            if p is not None:
                return f"{int(float(p))} 积分", int(float(p))
            return "未知", 0
        except Exception:
            return "查询失败", 0

    def exchange(self, cookie: str, plan: str):
        try:
            r = self._request("POST", APIEndpoint.EXCHANGE, cookie, data={"planType": plan})
            data = r.json()
            if data.get("code") == 0:
                return f"兑换成功: {plan}"
            return f"兑换失败: {data.get('message', '')}"
        except Exception as e:
            return f"兑换异常: {e}"


# ---------------------------------------------------------------------------
# 结果与主流程
# ---------------------------------------------------------------------------

@dataclass
class CheckinResult:
    account_idx: int
    domain: str
    status: str = ""
    days: str = ""
    points_total: str = ""
    exchange: str = ""
    code: int = -2


def run_one(cfg: Config, cookie: str, idx: int, domain: str) -> CheckinResult:
    result = CheckinResult(account_idx=idx, domain=domain)
    api = API(domain, cfg.user_agent)

    result.days, _ = api.get_status(cookie)

    ck = api.checkin(cookie)
    result.code = ck["code"]
    if ck["code"] == 0:
        result.status = "签到成功"
    elif ck["code"] == 1:
        result.status = "重复签到"
    else:
        result.status = f"签到失败({ck['message']})"

    result.points_total, _ = api.get_points(cookie)

    # 只有显式配置了兑换计划（plan100/plan200/plan500）才自动兑换，默认不兑换
    if cfg.exchange_plan in EXCHANGE_PLANS:
        result.exchange = api.exchange(cookie, cfg.exchange_plan)
    else:
        result.exchange = "未开启自动兑换"

    return result


def push(cfg: Config, title: str, content: str) -> None:
    if not cfg.pushdeer_sendkey:
        return
    try:
        # pypushdeer 官方包
        import pypushdeer
        pypushdeer.PushDeer(pushkey=cfg.pushdeer_sendkey).send_text(title, desp=content)
    except Exception as e:
        logging.warning("推送失败: %s", e)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = Config()

    if not cfg.cookies:
        logging.error("GLADOS_COOKIES 为空，无法签到")
        sys.exit(1)

    results: List[CheckinResult] = []
    for idx, cookie in enumerate(cfg.cookies, start=1):
        for domain in cfg.domains:
            r = run_one(cfg, cookie, idx, domain)
            results.append(r)
            logging.info("[账号%d] %s: %s | 剩余: %s | 积分: %s | %s",
                         idx, domain, r.status, r.days, r.points_total, r.exchange)

    # 汇总输出
    lines = []
    for r in results:
        lines.append(f"账号{r.account_idx} [{r.domain}] {r.status} | 剩余{r.days} | {r.points_total} | {r.exchange}")
    title = "GLaDOS 签到结果"
    content = "\n".join(lines)
    logging.info("===== 汇总 =====\n%s", content)
    push(cfg, title, content)


if __name__ == "__main__":
    main()
