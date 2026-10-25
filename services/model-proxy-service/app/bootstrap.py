"""模型代理服务入口（新文件）。

Dockerfile 的 CMD 指向本模块：`python -m app.bootstrap`。

之所以多这一层：DSML 适配是"导入即安装"的包装（`app/dsml_adapter.py`），需要一个
在 `serve()` 之前执行、且**不动原入口**的位置来加载它。
"""

import app.dsml_adapter  # noqa: F401  导入即安装（包装 chat_completion 出口）
from app.server import serve

if __name__ == "__main__":
    serve()
