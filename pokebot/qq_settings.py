"""Credential setup is local only: this module never makes network requests."""
import getpass
import json
from dataclasses import dataclass, field
from pathlib import Path

from .config import ROOT

SETTINGS_PATH = ROOT / "qq.local.json"


@dataclass(frozen=True)
class QQSettings:
    app_id: str
    app_secret: str = field(repr=False)
    allowed_groups: tuple[str, ...] = ()

    def __post_init__(self):
        if not isinstance(self.app_id, str) or not self.app_id.isascii() or not self.app_id.isdecimal():
            raise ValueError("AppID 应为后台显示的数字字符串，不是机器人分享链接或 QQ 号")
        if not isinstance(self.app_secret, str) or not self.app_secret.strip() or any(c.isspace() for c in self.app_secret):
            raise ValueError("AppSecret 不能为空，也不能包含空格或换行")
        if not isinstance(self.allowed_groups, (tuple, list)) or any(not isinstance(g, str) or not g for g in self.allowed_groups):
            raise ValueError("allowed_groups 应是群 OpenID 字符串列表，留空则服务已加入的群")

    @classmethod
    def load(cls, path: Path = SETTINGS_PATH):
        if not path.exists():
            raise ValueError("尚未填写凭证，请先双击 配置QQ机器人.cmd")
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (ValueError, UnicodeError):
            raise ValueError("qq.local.json 格式不正确，请重新运行配置入口") from None
        if not isinstance(data, dict) or set(data) - {"app_id", "app_secret", "allowed_groups"}:
            raise ValueError("QQ 配置包含未知字段，请使用 qq.example.json 中的格式")
        return cls(data.get("app_id", ""), data.get("app_secret", ""), data.get("allowed_groups", ()))

    def save(self, path: Path = SETTINGS_PATH):
        data = {"app_id": self.app_id, "app_secret": self.app_secret, "allowed_groups": list(self.allowed_groups)}
        temp = path.with_suffix(".tmp")
        try:
            temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            temp.replace(path)
        finally:
            temp.unlink(missing_ok=True)


def configure():
    print("QQ 官方机器人 · 本机凭证配置\n凭证只保存在本项目 qq.local.json 中，不上传、不打印。")
    old = None
    if SETTINGS_PATH.exists():
        try:
            old = QQSettings.load()
            print("已有配置，直接回车可保留原值。")
        except ValueError:
            print("原配置无效，请重新填写。")
    app_id = input("请粘贴 AppID：").strip() or (old.app_id if old else "")
    secret = getpass.getpass("请粘贴 AppSecret（输入时不显示字符），按回车确认：").strip() or (old.app_secret if old else "")
    settings = QQSettings(app_id, secret, old.allowed_groups if old else ())
    settings.save()
    print("\n配置已保存。下一步双击 检查QQ连接.cmd，检查成功后双击 启动QQ机器人.cmd。")
    print("请勿分享 qq.local.json。正常使用时保持启动窗口、电脑和网络在线。")


if __name__ == "__main__":
    try:
        configure()
    except (ValueError, OSError, EOFError) as exc:
        print(f"配置未完成：{exc}")
        raise SystemExit(1)
    except KeyboardInterrupt:
        print("\n已取消，未保存新配置。")
