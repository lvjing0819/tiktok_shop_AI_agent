"""Generate the token encryption key locally without printing it."""
import os
from pathlib import Path
from dotenv import dotenv_values
from cryptography.fernet import Fernet


def main():
    path = Path('.env')
    if path.is_symlink():
        raise SystemExit('拒绝写入符号链接 .env')
    if os.getenv('ADS_TOKEN_ENCRYPTION_KEY') or dotenv_values(path).get('ADS_TOKEN_ENCRYPTION_KEY'):
        print('已存在广告令牌加密密钥，保持不变。')
        return
    content = path.read_text() if path.exists() else Path('.env.example').read_text()
    # Replace the empty example entry, preserving all other configuration.
    import re
    content = re.sub(r'^ADS_TOKEN_ENCRYPTION_KEY=.*\n?', '', content, flags=re.M)
    content = content.rstrip()+'\nADS_TOKEN_ENCRYPTION_KEY='+Fernet.generate_key().decode()+'\n'
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'w') as f:
        f.write(content)
    os.chmod(path, 0o600)
    print('已在本机 .env 保存广告令牌加密密钥；请重启服务。')


if __name__ == '__main__':
    main()
