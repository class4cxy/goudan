#!/usr/bin/env python3
"""
一次性登录 Roborock 云账号，将 token 写入 .roborock_token.json，供 main.py 使用。
使用邮箱验证码登录，不向三方 SDK 传密码。

用法：
  cd platform && python login_once.py

会向 ROBOROCK_USERNAME 邮箱发送验证码，输入后换 token。ROBOROCK_REGION 同 main（默认 cn）。
"""

import asyncio
import json
import os
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=Path(__file__).parent.parent / ".env")
except ImportError:
    pass  # 无 python-dotenv 时仅使用已导出的环境变量

from roborock.data import UserData
from roborock.web_api import RoborockApiClient

TOKEN_FILE = Path(__file__).parent / ".roborock_token.json"
REGION_URLS = {
    "cn": "https://cniot.roborock.com",
    "eu": "https://euiot.roborock.com",
    "us": "https://usiot.roborock.com",
    "ru": "https://ruiot.roborock.com",
}


def _to_json_safe(obj):  # 递归转成可 JSON 序列化的类型（含 RRiot、Reference 等嵌套对象）
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, (list, tuple)):
        return [_to_json_safe(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _to_json_safe(v) for k, v in obj.items()}
    if hasattr(obj, "value") and not isinstance(obj, type):  # Enum 等（排除类型本身）
        return _to_json_safe(obj.value)
    if hasattr(obj, "model_dump"):
        return _to_json_safe(obj.model_dump())
    if hasattr(obj, "dict"):
        return _to_json_safe(obj.dict())
    # RRiot、Reference 等：用 __dict__ 转成嵌套 dict，便于 UserData.from_dict 正确还原
    try:
        d = vars(obj)
        if isinstance(d, dict):
            return {k: _to_json_safe(v) for k, v in d.items()}
    except TypeError:
        pass
    return str(obj)


def _user_data_to_dict(user_data: UserData) -> dict:
    if hasattr(user_data, "model_dump"):
        raw = user_data.model_dump()
    elif hasattr(user_data, "dict"):
        raw = user_data.dict()
    elif hasattr(user_data, "__dict__"):
        raw = dict(getattr(user_data, "__dict__", {}))
    else:
        raise AttributeError("UserData 无法序列化为 dict，请检查 python-roborock 版本")
    return _to_json_safe(raw)


async def main() -> None:
    region = os.environ.get("ROBOROCK_REGION", "cn").strip()
    base_url = REGION_URLS.get(region, REGION_URLS["cn"])
    username = os.environ.get("ROBOROCK_USERNAME", "").strip()

    if not username:
        username = input("Roborock 账号（邮箱）: ").strip()
    if not username:
        print("未输入账号，退出")
        sys.exit(1)

    print(f"正在使用区域 {region} ({base_url})，向 {username} 发送验证码…")
    api = RoborockApiClient(username=username, base_url=base_url)
    try:
        await api.request_code()
    except Exception as e:
        print(f"发送验证码失败: {e}")
        sys.exit(1)
    code = input("请输入邮箱收到的验证码: ").strip()
    if not code:
        print("未输入验证码，退出")
        sys.exit(1)
    try:
        user_data = await api.code_login(code)
    except Exception as e:
        err = str(e).strip()
        print(f"验证码登录失败: {err}")
        if "user agreement" in err.lower() or "mi home" in err.lower():
            print()
            print("常见原因与处理：")
            print("  1. 用户协议未同意 — 请打开「Roborock / 石头」App，重新登录或进入设置确认已同意用户协议。")
            print("  2. 当前为米家账号 — 本 SDK 需使用「Roborock / 石头」App 绑定的同一邮箱；若设备只绑在米家，请先用该邮箱在石头 App 注册/登录并绑定设备后再试。")
        sys.exit(1)

    try:
        user_data_dict = _user_data_to_dict(user_data)
    except Exception as e:
        print(f"序列化 user_data 失败: {e}")
        sys.exit(1)

    payload = {
        "username": username,
        "base_url": base_url,
        "user_data": user_data_dict,
    }
    TOKEN_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"✅ Token 已写入 {TOKEN_FILE}")
    print("   Bridge 启动时将优先使用该文件（无需密码，仅需 ROBOROCK_USERNAME 用于识别账号）。")


if __name__ == "__main__":
    asyncio.run(main())
