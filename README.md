# astrbot_plugin_JMComic

JMComic 的 AstrBot 查询与异步下载插件。V1 支持搜索、详情查询、按 ID 下载，并导出 ZIP/PDF 文件。

## 功能

- `/jm搜索 <关键词> [页码]`：搜索 JMComic 条目。
- `/jm详情 <id>`：查看本子元数据和章节列表。
- `/jm下载 <id> [zip|pdf]`：创建异步下载任务，完成后发送导出文件。
- `/jm任务`：查看当前会话最近任务。
- `/jm取消 <task_id>`：取消当前会话中的运行任务。
- `/jm帮助`：查看指令。
> [!CAUTION]
- 默认导出 ZIP；PDF 导出依赖 `img2pdf`。
- base64 会让实际传输体积增加约 33%，大文件可能被协议端拒绝。
## todo
- 本子更新订阅功能

## 🔗 相关链接
- 感谢[MPython API for JMComic]("https://github.com/hect0x7/JMComic-Crawler-Python")提供 Python API 访问禁漫天堂

