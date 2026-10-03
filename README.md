<div align="center">


<img src="logo.png" width="256" alt="icon">

# JMComic查询下载
[![AstrBot](https://img.shields.io/badge/AstrBot-Plugin-ff69b4?style=for-the-badge)](https://github.com/AstrBotDevs/AstrBot)
[![Python](https://img.shields.io/badge/Python-3.10+-blue.svg?style=for-the-badge&color=76bad9)](https://www.python.org/)

_✨ JMComic 的 AstrBot 查询与异步下载插件。支持 JM 号识别、可配置热门范围和排除标签的每日封面推荐与定时推送、搜索、分类热门榜、详情查询、按 ID 下载，以及 ZIP/PDF 群文件发送。✨_

</div>

## 功能

- `/jm搜索 <关键词> [页码]`：搜索 JMComic 条目。
- `/jm热门 [日|周|月] [分类] [页码]`：查看分类热门榜，别名 `/jm排行`。
- `更多` 或 `/jm更多`：继续显示最近一次搜索或热门榜结果。
- `/jm详情 <id>`：查看封面、标题、JM ID 和完整标签，并附带下载指令；别名 `/jminfo`。
- `/jm识别 <JM号或纯数字>`：查询并发送同样的封面卡片，别名 `/jmlookup`。
- `/jm推荐`：获取今日统一推荐，包含封面、标题、JM ID、完整标签及下载指令，别名 `/jmrecommend`。
- `/jm重置推荐`：AstrBot 管理员清空今日共享推荐及推送记录，下一次查询重新抽取，不立即群发；别名 `/jmresetrecommend`。
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
- `query.auto_recognize_jm`：自动识别普通消息中的 JM 号，默认开启；修改后需重载插件。
- `download.default_export_format`：默认导出格式，支持 `zip` / `pdf`。
- `download.file_delivery_mode`：支持 `auto`、`napcat_http_stream`、`onebot_group_file_base64`。
- `download.max_base64_file_mb`：`auto` 模式切换 HTTP Stream 的阈值，默认 80 MB。

## 每日推荐与定时推送

`/jm推荐` 默认从**全部分类周榜、Most Viewed（最多观看）**中推荐近期热门，排除“韩漫”后，在前 N 个合格作品中随机推荐一本；默认 N 为 10，可以自行填写任意正整数。热门时间范围可选**日榜、周榜、月榜或全部时间**；也可以选择 **Most Recent（最新发布）**，此排序沿用全部时间的最新发布，不应用榜单时间范围。上游对应时间为 `t` / `w` / `m` / `a`，排序为 `mr` / `mv`，具体榜单窗口以 JMComic 上游为准。[上游排序与时间常量](https://github.com/hect0x7/JMComic-Crawler-Python/blob/v2.7.6/src/jmcomic/jm_config.py)

`recommendation.exclude_tags` 可配置要排除的完整标签，默认 `['韩漫']`，兼容“韓漫”，留空列表关闭过滤。过滤同时检查榜单返回的分类；列表缺少标签或只有部分标签时，以作品详情校验，标题包含同样文字不会被误排除。过滤后不足 N 个会继续按排名翻页补足，因此“前 N 个”指过滤后的前 N 个合格候选。

当天首次手动查询或定时执行时选定，之后所有会话共享该作品；重启、重载和再次查询不会重新抽取，次日重新选择，不同日期允许重复。当天修改排序、时间范围、排除标签或 N 后，先重载插件，再执行 `/jm重置推荐` 才会在下一次查询中采用新配置；也可以等待次日自动采用新配置。如果今日缓存的作品标签命中新排除规则，会停止返回该作品并提示管理员重置。推荐卡片按选定时保存的来源显示时间范围和排序。

推荐只获取元数据与封面，不下载整本。图片通过 jmcomic 的异步客户端获取，复用 `network` 中的代理与 Cookie 配置，再以图片字节发送，适用于 AstrBot 与 NapCat 分容器部署，无需为封面配置共享目录或 HTTP Stream。标签完整显示；标签为空时显示“暂无标签”，封面获取失败时降级为文字并注明“封面暂不可用”。

在 WebUI 的“每日推荐”配置组中设置以下项目，然后重载插件：

| 配置 | 默认值 | 说明 |
| --- | --- | --- |
| `recommendation.enabled` | `false` | 开启自动推送；关闭时仍可手动使用 `/jm推荐` |
| `recommendation.time` | `09:00` | 每日推送时间，24 小时制 `HH:MM` |
| `recommendation.timezone` | `Asia/Shanghai` | IANA 时区，同时决定每日推荐的日期 |
| `recommendation.order_by` | `Most Viewed` | 最多观看；配合日、周、月榜推荐近期热门，也可改为全部时间最新发布 `Most Recent` |
| `recommendation.time_range` | `周榜` | 最多观看的榜单范围；可选日榜、周榜、月榜或全部时间 |
| `recommendation.exclude_tags` | `['韩漫']` | 完整标签匹配，兼容“韓漫”，同时检查返回的分类；留空列表关闭过滤 |
| `recommendation.top_n` | `10` | 过滤后的前 N 个合格候选；自行填写正整数，必要时自动翻页 |
| `recommendation.targets` | `[]` | 单独指定接收会话，不自动向全部白名单推送 |

配置接收会话的方法：

1. 在目标群聊或私聊发送 AstrBot 的 `/sid`，复制返回的完整 **UMO**，不要复制 UID，也不要包含外层引号。
2. 将每个 UMO 作为 `recommendation.targets` 的一个条目，例如 `机器人实例ID:GroupMessage:群号` 或 `机器人实例ID:FriendMessage:QQ号`。实例 ID 必须使用 `/sid` 返回的实际值；支持配置多个机器人实例。
3. 确保目标符合插件原有访问控制：默认群聊需加入 `access_control.group_whitelist`；私聊白名单非空时，接收用户也必须在其中。
4. 设置推送时间、排序、榜单范围、排除标签、候选数量和时区，开启 `enabled`，重载插件。先手动执行 `/jm推荐` 确认图文，再设置一个尚未到达的时间验证推送。

合格作品不足 N 条时从实际结果中抽取，跨页重复的作品会去重；空页或重复页停止继续获取，整页被排除时会继续查询下一页。N 越大或排除越多，首次生成当天推荐所需的请求越多。过滤开启时会查询候选详情，并复用选中作品的标题和标签；页面或候选详情获取失败时不保存新选择，也不会从尚未校验完整的范围中抽取。关闭过滤时仍先保存选定 ID，详情失败后重试查询同一作品。空列表或所有作品被排除时不发送推荐。封面缓存放在插件数据目录的 `recommendations` 中，在初始化及获取推荐时清理往日封面。

**重置今日推荐：** 在允许使用插件的会话中，由 AstrBot 管理员执行 `/jm重置推荐`。命令会清空所有会话共享的今日选择、发送尝试及成功记录，并清理当天封面缓存；如果正在推送，会等当前批次结束后再重置。下一次 `/jm推荐` 或尚未到达的定时任务重新抽取，仍可能随机抽中相同作品。重置本身不会发送推荐，不改变每日推送时间，也不会补发已错过的时刻。普通群管理员如果不是 AstrBot 管理员，不能执行此命令。

**已有配置升级：** 原有 `recommendation.order_by` 值会保留；此前使用默认 `Most Recent` 的用户，需要在 WebUI 将它改为 `Most Viewed` 才会按热门排序。新增时间范围默认周榜，排除标签默认韩漫。当天旧缓存仍按原来源显示，不会被误标为新周榜；管理员重置后立即采用新配置，或等待次日。从 v0.3.0 升级的日榜缓存仍显示“日榜（旧版本缓存）”。

定时任务不补发停机期间错过的推荐，休眠或事件循环延迟超过一分钟也跳过该次推送。夏令时导致设定时刻不存在时跳过当天，重复时刻只执行第一次。无效时间或时区会停用定时推送并记录日志；无效时区下手动推荐按 UTC+8 计日。

每个接收会话在每轮每日选择中最多自动尝试发送一次（管理员重置后清除记录），手动 `/jm推荐` 不占用定时推送次数。发送尝试和成功记录均会持久化；失败、超时或结果不明确的发送不自动重试，避免重复推送，单个会话失败不影响其余会话。必要时可手动获取推荐。关闭插件时定时任务随之取消。

升级时需安装新增依赖 `tzdata`（尤其是 Windows 环境），正常重新安装插件依赖即可。功能沿用 `jmcomic>=2.7.0` 的接口。

## JM 号识别

`/jm识别 <JM号或纯数字>`（别名 `/jmlookup`）和 `/jm详情 <id>`（别名 `/jminfo`）都会返回封面、标题、JM ID、完整标签和下载指令。一次性查询不会改变每日推荐状态。

开启 `query.auto_recognize_jm` 后，普通消息中出现的第一个 `JM` 加可选空格和数字会自动触发同一张卡片；匹配不区分大小写。命令消息、机器人自己发送的消息和未通过现有访问控制的消息会跳过，自动识别不发送前置提示。自动查询失败只记录日志，不向会话发送错误；封面获取失败时仍发送文字卡片，并注明“封面暂不可用”。修改开关后需重载插件。此开关只控制普通消息的自动识别，显式 `/jm识别`、`/jmlookup`、`/jm详情` 和 `/jminfo` 命令仍可使用。

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
