"""Run on the Ubuntu host. Prompts stay out of shell history and stdout."""
import getpass
import os
import secrets
from pathlib import Path


def main():
    root = Path(__file__).resolve().parent / "secrets"
    root.mkdir(mode=0o700, exist_ok=True)
    if any((root / name).exists() for name in ("hub_token.txt", "deepseek_key.txt")):
        raise SystemExit("已有凭据文件，未覆盖。需要更换时请自行在本机编辑。")
    key = getpass.getpass("DeepSeek API Key（隐藏输入）：").strip()
    if not key:
        raise SystemExit("Key 为空，未保存。")
    for name, value in (("hub_token.txt", secrets.token_urlsafe(36)), ("deepseek_key.txt", key)):
        path = root / name
        with path.open("x", encoding="utf-8") as file:
            file.write(value)
        path.chmod(0o400)
        if os.name == "posix" and os.geteuid() == 0:
            os.chown(path, 10001, 10001)
    print("凭据已保存在 deploy/secrets；没有输出密钥。将 hub_token.txt 的值在本机填入插件 dsh_token。")


if __name__ == "__main__":
    main()
