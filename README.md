<div align="center">


<img src="logo.png" width="256" alt="icon">

# JMComic查询下载
[![AstrBot](https://img.shields.io/badge/AstrBot-Plugin-ff69b4?style=for-the-badge)](https://github.com/AstrBotDevs/AstrBot)
[![Python](https://img.shields.io/badge/Python-3.10+-blue.svg?style=for-the-badge&color=76bad9)](https://www.python.org/)

_✨ JMComic 的 AstrBot 查询与异步下载插件。v0.3.0 支持每日封面推荐与定时推送、搜索、分类热门榜、详情查询、按 ID 下载，以及 ZIP/PDF 群文件发送。✨_

</div>

## 功能

- `/jm搜索 <关键词> [页码]`：搜索 JMComic 条目。
- `/jm热门 [日|周|月] [分类] [页码]`：查看分类热门榜，别名 `/jm排行`。
- `更多` 或 `/jm更多`：继续显示最近一次搜索或热门榜结果。
- `/jm详情 <id>`：查看本子元数据和章节列表。
- `/jm推荐`：获取今日统一推荐，包含封面、标题、JM ID、完整标签及下载指令，别名 `/jmrecommend`。
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

## 每日推荐与定时推送

`/jm推荐` 从全部分类的日榜前 3 或前 10 名中随机推荐一本，默认候选为前 10 名。这里的日榜是上游今日热门榜，并非今天新发布的作品。当天首次手动查询或定时执行时选定，之后所有会话共享该作品；重启、重载和再次查询不会重新抽取，次日重新选择，不同日期允许重复。

推荐只获取元数据与封面，不下载整本。图片通过 jmcomic 的异步客户端获取，复用 `network` 中的代理与 Cookie 配置，再以图片字节发送，适用于 AstrBot 与 NapCat 分容器部署，无需为封面配置共享目录或 HTTP Stream。标签完整显示；标签为空时显示“暂无标签”，封面获取失败时降级为文字并注明“封面暂不可用”。

在 WebUI 的“每日推荐”配置组中设置以下项目，然后重载插件：

| 配置 | 默认值 | 说明 |
| --- | --- | --- |
| `recommendation.enabled` | `false` | 开启自动推送；关闭时仍可手动使用 `/jm推荐` |
| `recommendation.time` | `09:00` | 每日推送时间，24 小时制 `HH:MM` |
| `recommendation.timezone` | `Asia/Shanghai` | IANA 时区，同时决定每日推荐的日期 |
| `recommendation.top_n` | `10` | 可选 `3`、`10`；当天已有选择时，修改候选数量在次日生效 |
| `recommendation.targets` | `[]` | 单独指定接收会话，不自动向全部白名单推送 |

配置接收会话的方法：

1. 在目标群聊或私聊发送 AstrBot 的 `/sid`，复制返回的完整 **UMO**，不要复制 UID，也不要包含外层引号。
2. 将每个 UMO 作为 `recommendation.targets` 的一个条目，例如 `机器人实例ID:GroupMessage:群号` 或 `机器人实例ID:FriendMessage:QQ号`。实例 ID 必须使用 `/sid` 返回的实际值；支持配置多个机器人实例。
3. 确保目标符合插件原有访问控制：默认群聊需加入 `access_control.group_whitelist`；私聊白名单非空时，接收用户也必须在其中。
4. 设置时间、候选数量和时区，开启 `enabled`，重载插件。先手动执行 `/jm推荐` 确认图文，再设置一个尚未到达的时间验证推送。

榜单不足 N 条时从实际结果中抽取，空榜或详情失败时不发送错误推荐。选定 ID 会先保存，详情失败后重试仍查询同一作品。封面缓存放在插件数据目录的 `recommendations` 中，在初始化及获取推荐时清理往日封面。

定时任务不补发停机期间错过的推荐，休眠或事件循环延迟超过一分钟也跳过该次推送。夏令时导致设定时刻不存在时跳过当天，重复时刻只执行第一次。无效时间或时区会停用定时推送并记录日志；无效时区下手动推荐按 UTC+8 计日。

每个接收会话每天最多自动尝试发送一次，手动 `/jm推荐` 不占用定时推送次数。发送尝试和成功记录均会持久化；失败、超时或结果不明确的发送不自动重试，避免重复推送，单个会话失败不影响其余会话。必要时可手动获取推荐。关闭插件时定时任务随之取消。

升级时需安装新增依赖 `tzdata`（尤其是 Windows 环境），正常重新安装插件依赖即可。功能沿用 `jmcomic>=2.7.0` 的接口。

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
