"""lark-twin: 以本人身份常驻飞书的数字人。

直接运行等价于 `larkx daemon`。首次使用先 `larkx auth qr` 扫码登录。
"""
import sys

from larkx.cli import main as cli_main

if __name__ == "__main__":
    sys.argv = [sys.argv[0], "daemon", *sys.argv[1:]]
    sys.exit(cli_main())
