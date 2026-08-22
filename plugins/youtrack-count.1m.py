#!/usr/bin/env python3
"""SwiftBar plugin for a YouTrack issue count and monthly leaderboard."""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"
PAGE_SIZE = 100


def load_config() -> dict:
    with CONFIG_PATH.open(encoding="utf-8") as config_file:
        config = json.load(config_file)

    required = ("base_url", "token", "query", "report_id")
    missing = [key for key in required if not str(config.get(key, "")).strip()]
    if missing:
        raise ValueError(f"config.json 缺少字段: {', '.join(missing)}")
    for key in required:
        config[key] = str(config[key]).strip()
    config["ranking_limit"] = max(1, min(int(config.get("ranking_limit", 10)), 50))
    config["expiration_field"] = str(
        config.get("expiration_field", "Expiration time")
    ).strip()
    config["timezone"] = str(config.get("timezone", "Asia/Shanghai")).strip()
    return config


def api_get(config: dict, path: str, parameters: dict | None = None):
    url = f"{config['base_url'].rstrip('/')}{path}"
    if parameters:
        url += "?" + urllib.parse.urlencode(parameters)
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {config['token']}",
            "Accept": "application/json",
            "User-Agent": "SwiftBar-YouTrack-Status/2.0",
        },
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


def request_page(config: dict, skip: int) -> list[dict]:
    result = api_get(
        config,
        "/api/issues",
        {
            "query": config["query"],
            "fields": "id,idReadable,summary,customFields(name,value)",
            "$skip": skip,
            "$top": PAGE_SIZE,
        },
    )
    if not isinstance(result, list):
        raise ValueError("YouTrack 返回了非预期的数据格式")
    return result


def get_incomplete_issues(config: dict) -> list[dict]:
    issues = []
    while True:
        page = request_page(config, len(issues))
        issues.extend(page)
        if len(page) < PAGE_SIZE:
            return sorted(issues, key=lambda issue: issue_expiration_sort_key(config, issue))


def issue_expiration(config: dict, issue: dict) -> int | None:
    field_name = config["expiration_field"].casefold()
    for field in issue.get("customFields") or []:
        if str(field.get("name") or "").casefold() != field_name:
            continue
        value = field.get("value")
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None
    return None


def issue_expiration_sort_key(config: dict, issue: dict) -> tuple:
    expiration = issue_expiration(config, issue)
    issue_id = str(issue.get("idReadable") or issue.get("id") or "")
    return (expiration is None, expiration or 0, issue_id)


def format_issue_expiration(config: dict, issue: dict) -> str:
    expiration = issue_expiration(config, issue)
    if expiration is None:
        return "无过期时间"
    timezone = ZoneInfo(config["timezone"])
    return datetime.fromtimestamp(expiration / 1000, timezone).strftime("%Y-%m-%d %H:%M")


def get_monthly_ranking(config: dict) -> tuple[list[dict], int | None, int]:
    current_user = api_get(
        config, "/api/users/me", {"fields": "id,login,fullName"}
    )
    report = api_get(
        config,
        f"/api/reports/{urllib.parse.quote(config['report_id'], safe='-')}",
        {
            "$top": -1,
            "fields": (
                "id,name,data(total(value,presentation),"
                "columns(name,size(value,presentation),queryUrl,issuesQuery))"
            ),
        },
    )
    report_data = report.get("data") or {}
    columns = report_data.get("columns") or []
    if not isinstance(columns, list):
        raise ValueError("月度排行返回了非预期的数据格式")

    ranking = []
    for column in columns:
        name = str(column.get("name") or "未知用户")
        size = column.get("size") or {}
        value = int(size.get("value") or 0)
        query_url = str(column.get("queryUrl") or "")
        ranking.append({"name": name, "value": value, "query_url": query_url})

    identities = {
        str(current_user.get(key) or "").casefold()
        for key in ("login", "fullName")
        if current_user.get(key)
    }
    my_rank = next(
        (
            index
            for index, row in enumerate(ranking, 1)
            if row["name"].casefold() in identities
        ),
        None,
    )
    total = int((report_data.get("total") or {}).get("value") or 0)
    return ranking, my_rank, total


def safe_menu_text(value: object) -> str:
    return str(value).replace("|", "-").replace("\n", " ").strip()


def absolute_url(base_url: str, relative_url: str) -> str:
    return urllib.parse.urljoin(base_url.rstrip("/") + "/", relative_url)


def issue_url(config: dict, issue: dict) -> str:
    issue_id = urllib.parse.quote(str(issue.get("idReadable") or issue["id"]), safe="-")
    return f"{config['base_url'].rstrip('/')}/issue/{issue_id}"


def open_all_incomplete() -> int:
    try:
        config = load_config()
        issues = get_incomplete_issues(config)
        urls = [issue_url(config, issue) for issue in issues]
        if urls:
            subprocess.run(["/usr/bin/open", *urls], check=True)
        return 0
    except Exception as error:
        print(f"打开失败: {safe_menu_text(error)}", file=sys.stderr)
        return 1


def main() -> int:
    try:
        config = load_config()
        issues = get_incomplete_issues(config)
        count = len(issues)
        ranking_error = None
        try:
            ranking, my_rank, monthly_total = get_monthly_ranking(config)
        except Exception as error:
            ranking, my_rank, monthly_total = [], None, 0
            ranking_error = safe_menu_text(error)
        search_url = (
            f"{config['base_url'].rstrip('/')}/issues?"
            + urllib.parse.urlencode({"q": config["query"]})
        )
        title = f"YT: {count}"
        if my_rank is not None:
            title += f" · 月榜 #{my_rank}"
        print(f"{title} | sfimage=checklist")
        print("---")
        print(f"搜索条件: {safe_menu_text(config['query'])}")
        print(f'在 YouTrack 中查看筛选结果 | href="{search_url}"')
        if issues:
            plugin_path = str(Path(__file__).resolve())
            print(
                f'打开全部未完成 Issue（{count}）'
                f' | bash="{plugin_path}" param1=--open-all terminal=false'
            )
            for issue in issues:
                expiration = format_issue_expiration(config, issue)
                readable_id = safe_menu_text(issue.get("idReadable") or issue.get("id"))
                summary = safe_menu_text(issue.get("summary") or "无标题")
                print(
                    f'{expiration} · {readable_id} · {summary}'
                    f' | href="{issue_url(config, issue)}"'
                )
        else:
            print("打开全部未完成 Issue（0） | color=#8E8E93")
            print("当前没有符合条件的未完成 Issue | color=#8E8E93")
        print("---")
        if ranking_error:
            print(f"月度排行加载失败: {ranking_error} | color=red")
        else:
            if my_rank is not None:
                mine = ranking[my_rank - 1]
                print(f"我的当月排名: #{my_rank} · {mine['value']} 条 | color=#007AFF")
            print(f"当月完成排行 Top {min(config['ranking_limit'], len(ranking))} · 共 {monthly_total} 条")
            medals = {1: "🥇", 2: "🥈", 3: "🥉"}
            for rank, row in enumerate(ranking[: config["ranking_limit"]], 1):
                prefix = medals.get(rank, f"{rank}.")
                row_url = absolute_url(config["base_url"], row["query_url"])
                print(
                    f'{prefix} {safe_menu_text(row["name"])} · {row["value"]} 条'
                    f' | href="{row_url}"'
                )
        print("立即刷新 | refresh=true")
        return 0
    except urllib.error.HTTPError as error:
        print("YT: ! | color=red sfimage=exclamationmark.triangle")
        print("---")
        print(f"YouTrack HTTP 错误: {error.code}")
        print("立即刷新 | refresh=true")
    except Exception as error:
        print("YT: ! | color=red sfimage=exclamationmark.triangle")
        print("---")
        print(f"错误: {safe_menu_text(error)}")
        print("立即刷新 | refresh=true")
    return 1


if __name__ == "__main__":
    if "--open-all" in sys.argv[1:]:
        sys.exit(open_all_incomplete())
    sys.exit(main())
