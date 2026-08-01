"""Web UI入口文件：直接运行启动本地Web服务，自动打开浏览器。

使用方式：
    python web.py
启动后访问 http://localhost:5876 即可使用图形界面操作。
"""
from web.app import main

if __name__ == "__main__":
    main()