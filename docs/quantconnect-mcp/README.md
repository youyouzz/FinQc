# QuantConnect MCP：Cursor 云端用法

这份说明按「只在 QuantConnect 云端回测、用浏览器看图、在 Cursor 里跟 Agent 说话」来写。官方原文副本在 `official/`，下载自 2026-11-14。

调研来自三路检索：[官方文档](5b2ad0fd-b474-4548-a208-8b7c7d997296)、[GitHub 仓库](2be3f3cc-814c-4e3a-9c87-d2a9db4b7e83)、[社区用法](b378b666-0a0b-4553-a7c6-b73333afff36)。

## 结论

用官方远程 MCP，不装 QuantConnect Code 插件，也不要自己跑本地 MCP 进程。

| 要做的事 | 用什么 |
| --- | --- |
| 改策略、编译、提交云端回测、读统计和订单 | Cursor Agent + 远程 MCP |
| 看净值、回撤、Benchmark、订单标注的价格图 | 浏览器里该项目的回测结果页 |
| 本机 Docker、本地行情、编辑器内结果页 | 不需要 |

插件和 MCP 是两条并列的客户端，都调用 QuantConnect 云端 API。插件底层是 LEAN CLI，负责本地同步和结果页，不是包在 MCP 外面的一层。两个同时写同一个项目会互相覆盖。官方对「本地 agent 在改文件」的建议是关掉插件同步。网页已经能看图，所以这里不装插件。

## 前置条件

远程端点：`https://www.quantconnect.com/api/v2/mcp`

组织里要有付费 agent node：A1-1、A4-8 或 A16-32。免费的 A-MICRO 连不上，报错原文是：

> The QuantConnect Remote MCP Server requires a dedicated Assistant node (A1-1, A4-8, or A16-32). Purchase nodes on the pricing page.

节点规格（官方 Deployments 页，不是价格）：

| 节点 | 核 | 内存 | 可同时跑的 agent |
| --- | --- | --- | --- |
| A-MICRO | 1 | 1 GB | 1 |
| A1-1 | 1 | 1 GB | 1 |
| A4-8 | 4 | 8 GB | 4 |
| A16-32 | 16 | 32 GB | 16 |

价格只在 https://www.quantconnect.com/pricing 。QuantConnect API 本身没有调用次数配额。Cursor 和模型有自己的用量上限。A-MICRO 的每月 10 万 token 是 QuantConnect 云端 agent 的额度，不是 Cursor 的额度，而且这个免费节点不能连远程 MCP。

授权是浏览器 OAuth，选定一个组织。配置文件里不要写 User Id 或 API Token。

## Cursor 配置

官方 Cursor 页写的是用户级 `~/.cursor/mcp.json`（Customize → MCPs → New MCP Server → User）：

```json
{
  "mcpServers": {
    "qc-mcp": {
      "url": "https://www.quantconnect.com/api/v2/mcp",
      "transport": {
        "type": "Streamable HTTP"
      }
    }
  }
}
```

Cursor 自己的远程 MCP 文档只要求 `url`，看到 URL 就会走 Streamable HTTP。若带 `transport` 的写法被拒绝，改成：

```json
{
  "mcpServers": {
    "qc-mcp": {
      "url": "https://www.quantconnect.com/api/v2/mcp"
    }
  }
}
```

不要把 Copilot 的格式抄进来。Copilot 用的是顶层 `servers` 和 `"type": "http"`，那是 VS Code 的文件，不是 Cursor 的。

保存后开一个 Agent 对话。弹出认证就点 Authenticate，在已登录 QuantConnect 的浏览器里选组织并 Authorize。

官方 Cursor 入门还要求先装 QuantConnect 扩展、登录、Pull Organization Workspace。那是为了让 `read_open_project` 看见「当前打开的项目」，以及在编辑器里看结果。云端优先、网页看图时跳过这几步。没有插件时，对话里说出项目名，让 Agent 用 `list_projects` 找 `projectId`。

Docker 只在 Deployment Target 设成本地时才要用。这条路径不装 Docker。

## 一次回测怎么走

1. 说明项目名，或让它新建项目。新建必须带入口文件：Python 是 `main.py`，C# 是 `main.cs`。
2. 改云端文件用 `edit_file`（替换唯一片段）或 `create_file`（整文件）。写超组织方案的单文件大小会被拒绝，要拆文件。改完应编译，工具是 `create_compile`。`create_file` 和 `edit_file` 也会编译并返回错误。
3. 提交回测用 `create_backtest`。它只返回回测 ID，计算在云端排队。官方要求不要轮询。`read_backtest` 是给已经完成、或上一轮已经送达的结果用的。提前调用会拿到空的或过期的数据。优化同理：`create_optimization` 之后不要用 `read_optimization` 去探完成没有。
4. 人去网页打开该项目的回测列表看图。结果页在节点上跑的时候会自己更新，刷新或关掉页面不会停掉回测。图不在聊天里。当前官方工具列表没有 `read_backtest_chart`。`jupyter_get_image` 只拿 Research 笔记本里的图，而且是 base64，官方写明为了省 token，非必要不要用。
5. 完成通知作为新消息送达之后，再让 Agent 总结统计、订单和日志。订单用 `read_backtest_orders`，日志用 `search_backtest_logs`。

网页上不要同时开 Ask Mia 改同一份代码。那是另一个 agent。

实盘相关工具能做的事要单独说清楚再让它调用：`create_live_algorithm` 是用 QuantConnect brokerage 做 paper；`stop_live_algorithm` 停止但保留仓位；`liquidate_live_algorithm` 按市价平仓并停止。

连通测试可以是：「列出我的项目，读出名叫某某的那一个。」官方 Hello World 是 `Read the open project.`，那依赖当前有一个已打开的项目。

## 官方工具（48 个）

全文在 `official/documentation/08 MCP Server/01 Key Concepts/04 Tools.html`。下面是分组，描述以该文件为准。

| 分组 | 工具 |
| --- | --- |
| 项目 | `create_project` `list_projects` `read_project` `read_open_project` `update_project` `read_project_templates` |
| 文件 | `create_file` `edit_file` `read_file` `delete_file` `search_file_content` |
| 编译 | `create_compile` |
| 回测 | `create_backtest` `read_backtest` `read_backtest_orders` `list_backtests` `update_backtest` `delete_backtest` `search_backtest_logs` |
| 优化 | `create_optimization` `read_optimization` `list_optimizations` `delete_optimization` |
| 实盘 | `create_live_algorithm` `read_live_algorithm` `read_live_holdings` `search_live_logs` `stop_live_algorithm` `liquidate_live_algorithm` |
| Research | `jupyter_create_cell` `jupyter_read_cell` `jupyter_update_cell` `jupyter_delete_cell` `jupyter_execute_cell` `jupyter_create_notebook` `jupyter_read_notebook` `jupyter_execute_notebook` `jupyter_get_image` |
| Object Store | `object_store_set` `object_store_pdf_to_markdown` |
| 数据与环境 | `list_datasets` `get_dataset_details` `environment_library_support` |
| Skills | `list_skills` `load_skill` |
| 通知 | `send_email_notification` `send_sms_notification` `send_telegram_notification` |

Claude Code 文档的示例 `CLAUDE.md` 提到 `read_compile`。Key Concepts 的工具表里没有这个名字。

## 开源仓库：哪一个还能用

官方仓库 https://github.com/QuantConnect/mcp-server 在 2026-10-26 被换成只有 README 和 LICENSE 的仓库。README 写明远程端点是首选，没有可安装的服务器。检索时约 80 star、32 fork，Apache-2.0，文档站指向 Key Concepts。副本在 `official/mcp-server/README.md`。

不要再按 2025 年的文章去 `docker pull quantconnect/mcp-server`。Docker Hub 上的旧镜像还在，那是本地 stdio 进程，要用 User Id 和 API Token，工具集和现在的远程服务器不是同一份。Cursor 上那些 output schema 报错（[#47](https://github.com/QuantConnect/mcp-server/issues/47) 等）大多是这个旧服务器留下的。

| 仓库 | 是什么 | 要不要用 |
| --- | --- | --- |
| [QuantConnect/mcp-server](https://github.com/QuantConnect/mcp-server) | 官方指针，指向远程 URL | 用它指向的端点，不要克隆来跑 |
| [QuantConnect/Documentation](https://github.com/QuantConnect/Documentation) | 官方文档源，Apache-2.0 | 已下载 MCP 章节 |
| [taylorwilsdon/quantconnect-mcp](https://github.com/taylorwilsdon/quantconnect-mcp) | 独立 FastMCP，约 61 个工具，含图表数据。stdio，`uvx`，要 API Token。检索时约 125 star | 只有在远程服务器缺图表工具、且你接受把 API Token 放进本机环境时才考虑 |
| [philkoerber/quantconnect-mcp-server](https://github.com/philkoerber/quantconnect-mcp-server) | 官方仓库被清空前的 fork | 过时 |
| [fschaeffler/quant-connect-mcp-lambda](https://github.com/fschaeffler/quant-connect-mcp-lambda) | TypeScript，本地或 Lambda，API Token | 官方远程出现之前的替代品 |
| [ababber/cursor-starter-kit-template](https://github.com/ababber/cursor-starter-kit-template) | 给旧 Docker 服务器绕过 Cursor 参数错误的 wrapper | 不适用于远程 URL |
| [TheFractalyst/QuantConnectMCP](https://github.com/TheFractalyst/QuantConnectMCP)、[lhstorm/mcp_server_quantconnect_docs](https://github.com/lhstorm/mcp_server_quantconnect_docs)、[noodlespoodles/qc-docs-mcp](https://github.com/noodlespoodles/qc-docs-mcp) | 搜文档，不连交易 API | 不能代替远程 MCP |
| [neosun100/lean-allinone](https://github.com/neosun100/lean-allinone) | 本机 LEAN Docker | 和「不做本地回测」相反 |

公开代码索引里几乎搜不到别人把 `quantconnect.com/api/v2/mcp` 写进 `mcp.json` 并提交到 GitHub。能核对的可用配置就是官方 Cursor 页上的那一段。社区论坛里大量「Cursor 配好了」的帖子，配的是 2025 年的 `uvx` 或 Docker，加上 API Token，不是现在这条远程路径。

还没做完的官方缺口：

- [#49](https://github.com/QuantConnect/mcp-server/issues/49) 希望回测结果变成 MCP resource，避免整份 JSON 灌进上下文。仍开放。远程工具列表里仍然没有图表工具。
- [#58](https://github.com/QuantConnect/mcp-server/issues/58) 是插件自带的 MCP 把 `extraData` 塞进响应，严格客户端校验失败。和远程 URL 不是同一个服务器。有人用未写入文档的设置 `quantConnect.mcp.mcpOnly: true` 绕过。不装插件就碰不到。

## 本地副本

| 路径 | 来源 |
| --- | --- |
| `official/mcp-server/README.md` | https://github.com/QuantConnect/mcp-server `master` |
| `official/documentation/08 MCP Server/` | https://github.com/QuantConnect/Documentation `22c9609c`（2026-11-13） |

Documentation 仓库该目录是 Apache-2.0。网页渲染版仍以 quantconnect.com 为准；这份副本是同一天的 `master` 源文件。Cursor 入门源文件里的端点已经是 `https://www.quantconnect.com/api/v2/mcp`，不是旧的 `localhost:3001`。

在线文档：

- https://www.quantconnect.com/mcp
- https://www.quantconnect.com/docs/v2/ai-assistance/mcp-server
- https://www.quantconnect.com/docs/v2/ai-assistance/mcp-server/key-concepts
- https://www.quantconnect.com/docs/v2/ai-assistance/mcp-server/cursor
- https://www.quantconnect.com/docs/v2/ai-assistance/deployments
- https://www.quantconnect.com/announcements/21675/remote-mcp-for-quantconnect/
- https://www.quantconnect.com/docs/v2/cloud-platform/backtesting/results
