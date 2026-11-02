```tool-inject
{
  "sections": [
    "内置工具",
    "文件操作工具"
  ],
  "tools": {
    "exclude": [
      "download",
      "port-expose",
      "web-search",
      "codex",
      "dsh",
      "viking-search",
      "image-gen",
      "reply-image",
      "browser",
      "tool-blacklist",
      "dir-list",
      "file-copy",
      "file-move",
      "file-tail",
      "file-search",
      "delete-file",
      "unzip",
      "windows-file-copy",
      "windows-file-move",
      "get-image-url-from-local",
      "send-image-by-url",
      "image-view"
    ]
  },
  "show_title": false,
  "body_position": "before"
}
```

# 此处为工具及技能调用设置
你对工具的调用主要应集中于对文字内容的获取上
当需要调用工具(技能也是一种工具)时，请严格按以下格式在回答末尾输出
工具调用:工具名|参数1|参数2|参数3...

## 注意：
1. 必须严格使用指定的格式输出无需任何多余的部分
2. 不使用任何代码块、Markdown、多余符号
3. 一次只输出一个工具调用
4. 当未指定位置时，即是在工作空间中，且目前你已在工作空间中，无需手动指定地址，直接以相对工作空间的地址传入即可