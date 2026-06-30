<div align="center">


<img src="logo.png" width="256" alt="icon">

# JMComic查询下载
[![AstrBot](https://img.shields.io/badge/AstrBot-Plugin-ff69b4?style=for-the-badge)](https://github.com/AstrBotDevs/AstrBot)
[![Python](https://img.shields.io/badge/Python-3.10+-blue.svg?style=for-the-badge&color=76bad9)](https://www.python.org/)

_✨ JMComic 的 AstrBot 查询与异步下载插件。v0.2.0 支持搜索、分类热门榜、详情查询、按 ID 下载，以及 ZIP/PDF 群文件发送。✨_

</div>

## 功能

- `/jm搜索 <关键词> [页码]`：搜索 JMComic 条目。
- `/jm热门 [日|周|月] [分类] [页码]`：查看分类热门榜，别名 `/jm排行`。
- `更多` 或 `/jm更多`：继续显示最近一次搜索或热门榜结果。
- `/jm详情 <id>`：查看本子元数据和章节列表。
- `/jm下载 <id> [zip|pdf]`：创建异步下载任务，完成后上传 ZIP/PDF 到群文件。
- `/jm任务`：查看当前会话最近任务。
- `/jm取消 <task_id>`：取消当前会话中的运行任务。
- `/jm帮助`：查看指令。

热门榜分类支持：全部、同人、单本、短篇、其他、韩漫、美漫、cosplay、3d、英文。

搜索结果支持按配置分批展示；API 搜索缺少标签时，插件会为当前展示结果异步补全标签。

> [!CAUTION]
> - 默认导出 ZIP；PDF 导出依赖 `img2pdf`。
> - Base64 会让实际传输体积增加约 33%，大文件建议使用 NapCat HTTP Stream。
> - 群文件上传仅支持群聊，群聊需要加入插件白名单。

## 主要配置

- `access_control.enable_private_only`：默认仅私聊可用；群聊需配置白名单。
- `access_control.group_whitelist` / `private_whitelist`：群聊和私聊白名单。
- `network.proxy` / `client_impl` / `domains` / `cookies_avs`：JMComic 客户端网络配置。
- `query.search_page_size`：每次展示的搜索/热门榜结果数量，默认 10。
- `query.search_result_tag_limit`：多结果标签展示数量，默认 5；单结果显示全部标签。
- `query.search_enrich_tags`：自动获取详情补全 API 搜索标签，默认开启。
- `download.default_export_format`：默认导出格式，支持 `zip` / `pdf`。
- `download.file_delivery_mode`：支持 `auto`、`napcat_http_stream`、`onebot_group_file_base64`。
- `download.max_base64_file_mb`：`auto` 模式切换 HTTP Stream 的阈值，默认 80 MB。

## NapCat HTTP Stream

NapCat `4.8.115+` 支持用于大文件和跨容器传输的 Stream API。AstrBot 与 NapCat 分容器部署时，建议将两个容器加入同一 Docker network，并在 NapCat WebUI 中启用 HTTP Server。

插件配置示例：

```text
file_delivery_mode = auto
max_base64_file_mb = 80
napcat_http_api_base = http://napcat:3000
napcat_http_access_token = <NapCat HTTP Server Token>
```

`napcat` 需要替换为实际的 NapCat Docker 服务名。HTTP Server 建议监听容器内 `0.0.0.0:3000` 并设置 Token，无需暴露到公网。

## todo
- 本子收藏夹与更新订阅功能
- ~~分类/排行榜~~

## 🔗 感谢以下项目
### Python API for JMComic

<a href="https://github.com/hect0x7/JMComic-Crawler-Python">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://github-readme-stats.vercel.app/api/pin/?username=hect0x7&repo=JMComic-Crawler-Python&theme=radical" />
    <source media="(prefers-color-scheme: light)" srcset="https://github-readme-stats.vercel.app/api/pin/?username=hect0x7&repo=JMComic-Crawler-Python" />
    <img alt="Repo Card" src="https://github-readme-stats.vercel.app/api/pin/?username=hect0x7&repo=JMComic-Crawler-Python" />
  </picture>
</a>
