"""Explicit, standalone registration of QQ's native group command panel.

No gateway, collection database or group messages are used by this tool.
The default invocation only previews public configuration, without credentials.
"""
import argparse
import asyncio
import json
from urllib.parse import quote, urlencode

import aiohttp

from .qq_api import QQAPI, QQAPIError
from .qq_settings import QQSettings

MARKER = "qq-pokemon-bot:group-commands"
COMMANDS = (
    ("探索", "每日十连，自动收集"),
    ("我的收集", "查看收藏统计"),
    ("图鉴", "全国与世代完成度"),
    ("图鉴 全国 1", "打开全国收藏图鉴"),
    ("图鉴 1 1", "打开第一世代图鉴"),
    ("成就", "查看收集成就"),
    ("皮卡丘", "资料查询示例，可修改名字"),
    ("帮助", "查看全部指令"),
)

# Only documented structural keys and harmless enum values may appear in reports.
# Unknown keys can themselves contain identifiers, so never print them verbatim.
DIAGNOSTIC_KEYS = frozenset({
    "data", "result", "records", "panels", "list", "items", "panel", "panel_id",
    "scope", "target_type", "next_cursor", "is_end", "version", "code", "message",
    "remark", "name", "desc", "type", "only_admin", "link", "created_at", "updated_at",
    "user_openids", "group_openids", "total", "count", "limit", "cursor",
})


def response_shape(value, key="", depth=0):
    """Describe an API response without exposing values or arbitrary field names."""
    if depth >= 7:
        return "<深层内容已省略>"
    if isinstance(value, dict):
        return {(k if k in DIAGNOSTIC_KEYS else f"<未知字段{i}>"):
                response_shape(v, k, depth + 1)
                for i, (k, v) in enumerate(list(value.items())[:30])}
    if isinstance(value, list):
        return {"长度": len(value), "前两项结构": [response_shape(v, depth=depth + 1) for v in value[:2]]}
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        if (key == "scope" and value in ("group", "c2c", "channel", "dm")) or (
                key == "target_type" and value in ("all", "specific")):
            return value
        return "<空字符串>" if not value else "<字符串已隐藏>"
    return "<数值>" if isinstance(value, (int, float)) else "<其他类型>"


async def diagnose_panel(api):
    data = await api._request("GET", "/v2/panels?scope=group&limit=50")
    return "面板查询结构（已隐藏内容及标识）：\n" + json.dumps(
        response_shape(data), ensure_ascii=False, indent=2)


def panel_config():
    # QQ uses name itself as the inserted command, not a separate display label.
    return {"remark": MARKER, "items": [
        {"type": "command", "name": name, "desc": desc, "only_admin": False}
        for name, desc in COMMANDS
    ]}


def matches(panel):
    items = panel.get("items")
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        return False
    normalized = [{key: item.get(key, False if key == "only_admin" else None)
                   for key in ("type", "name", "desc", "only_admin")} for item in items]
    return panel.get("remark") == MARKER and normalized == panel_config()["items"]


async def list_group_panels(api):
    records, seen = [], set()
    cursor = ""
    for _ in range(20):
        query = {"scope": "group", "limit": 50}
        if cursor:
            query["cursor"] = cursor
        data = await api._request("GET", "/v2/panels?" + urlencode(query))
        page = data.get("records")
        if not isinstance(page, list) or any(
            not isinstance(r, dict) or r.get("scope") != "group"
            or r.get("target_type") not in ("all", "specific")
            or not isinstance(r.get("panel_id"), str) or not r["panel_id"]
            or not isinstance(r.get("panel"), dict) for r in page
        ):
            raise RuntimeError("平台返回的面板列表格式异常，已停止，未修改配置。")
        records.extend(page)
        cursor = data.get("next_cursor")
        if data.get("is_end") is True or cursor == "":
            return records
        if not isinstance(cursor, str) or not cursor or cursor in seen:
            break
        seen.add(cursor)
    raise RuntimeError("未能完整读取面板列表，已停止，未修改配置。")


async def configure_panel(api):
    records = await list_group_panels(api)
    global_panels = [r for r in records if r["target_type"] == "all"]
    owned = [r for r in global_panels if r["panel"].get("remark") == MARKER]
    if len(owned) > 1 or len(global_panels) > len(owned):
        raise RuntimeError("已有其他或重复的群聊全局面板，已停止以保留原配置；请先在平台核对。")
    desired = panel_config()
    if owned:
        current = owned[0]
        panel_id = current["panel_id"]
        if matches(current["panel"]):
            return "群聊指令面板已是最新，无需修改。"
        body = {"panel": desired}
        version = current.get("version", current["panel"].get("version"))
        if isinstance(version, int):
            body["panel"]["version"] = version
        await api._request("PUT", "/v2/panels/" + quote(panel_id, safe=""), body=body, retry=False)
    else:
        # A creation is not idempotent. On an uncertain response, re-list on the
        # next explicit run instead of blindly retrying POST and making duplicates.
        response = await api._request("POST", "/v2/panels", body={
            "scope": "group", "target_type": "all", "panel": desired,
        }, retry=False)
        panel_id = response.get("panel_id")
        if not isinstance(panel_id, str) or not panel_id:
            raise RuntimeError("创建响应未包含面板 ID，结果待确认；稍后重新执行会先查询现有面板。")
    verified = await list_group_panels(api)
    if not any(r["panel_id"] == panel_id and r["target_type"] == "all"
               and matches(r["panel"]) for r in verified):
        raise RuntimeError("平台已接收配置，但回读未确认生效；稍后重新执行核对，请勿连续重复运行。")
    return "群聊指令面板已配置，并已通过官方接口回读确认。"


async def apply_panel(diagnose=False):
    settings = QQSettings.load()
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
        api = QQAPI(session, settings)
        return await diagnose_panel(api) if diagnose else await configure_panel(api)


def main(argv=None):
    parser = argparse.ArgumentParser(description="配置 QQ 原生群聊指令面板（默认仅离线预览）")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="使用本机凭证向 QQ 注册或更新面板")
    mode.add_argument("--diagnose", action="store_true", help="只查询面板并显示脱敏结构，不修改配置")
    args = parser.parse_args(argv)
    if not args.apply and not args.diagnose:
        print(json.dumps({"scope": "group", "target_type": "all", "panel": panel_config()},
                         ensure_ascii=False, indent=2))
        print("当前仅预览，未读取凭证、未联网。添加 --apply 才会配置 QQ 面板。")
        return 0
    try:
        print(asyncio.run(apply_panel(diagnose=args.diagnose)))
    except QQAPIError as exc:
        print(f"面板配置未确认（HTTP {exc.status}，代码 {exc.code}）。")
        print("请核对 QQ 平台指令面板权限、服务器 IP 白名单和网络。")
        print("网络中断时可能已创建成功；稍后重新执行会先查询，不要同时在多处运行。")
        return 1
    except (ValueError, RuntimeError) as exc:
        print(str(exc))
        return 1
    except (OSError, aiohttp.ClientError):
        print("无法读取本机配置或连接 QQ，请检查文件权限及网络。")
        return 1
    if args.diagnose:
        print("诊断完成：仅查询面板，未创建或修改菜单，未发送群消息。")
        return 0
    print("请在 QQ 群输入框选择 @机器人验收；点击选项会填入指令，仍需点击发送。")
    print("显示位置与客户端版本有关；接口配置成功不代表客户端验收已通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
