# Persona+ 插件

![:name](https://count.getloli.com/@astrbot_plugin_persona_plus?name=astrbot_plugin_persona_plus&theme=miku&padding=7&offset=0&align=top&scale=1&pixelated=1&darkmode=auto)

扩展 AstrBot 的人格管理能力，提供关键词自动切换、快速切换指令、以及与 QQ 头像/昵称的同步修改。

主要特性
- 使用命令直接 创建/更新 人格
- 基于关键词的自动切换
- 支持为人格上传头像，并在切换人格时同步切换QQ昵称和头像
- 切换人格时可选择清空当前会话上下文

安装
- 将插件目录放到 AstrBot 的 `data/plugins/` 或 `packages/` 下（通常在仓库 `data/plugins/astrbot_plugin_persona_plus/` 已包含）。
- 重启 AstrBot，插件将自动加载。

主要命令
（命令组：`/persona_plus`，别名：`/pp`、`/persona+`）

- /persona_plus help
  - 显示帮助与命令说明。

- /persona_plus list
  - 列出所有已注册的人格。

- /persona_plus view <persona_id>
  - 查看指定人格的 System Prompt、预设对话与工具配置。

- /persona_plus create <persona_id>
  - 创建新人格。发送此命令后，请直接在聊天中发送要作为 System Prompt 的纯文本。

- /persona_plus update <persona_id>
  - 更新现有人格。发送此命令后，请直接在聊天中发送新的纯文本 System Prompt。

- /persona_plus avatar <persona_id>
  - 上传或更新人格头像。发送此命令后，请在聊天中发送图片，插件会保存头像并在配置允许时尝试同步到 QQ。

- /persona_plus delete <persona_id>
  - 删除指定人格（管理员权限）。

- 快捷切换：/pp <persona_id>
  - 直接切换当前会话的人格，示例：`/pp assistant_v2`。

配置项（摘要）
- enable_keyword_switching: bool (默认: true)
  - 是否启用关键词自动切换。

- keyword_mappings: 文本 (多行，每行格式 `关键词:persona_id`)
  - 关键词匹配为包含匹配（大小写不敏感）。
  
- auto_switch_scope: string (默认: "conversation")
  - 自动切换生效范围：`conversation`、`session` 或 `global`。

- enable_auto_switch_announce: bool (默认: false)
  - 当自动切换发生时，是否发送提示消息（如 “已切换人格为 <id>”）。

- clear_context_on_switch: bool (默认: false)
  - 切换人格时是否清空当前会话历史。

- require_admin_for_manage: bool (默认: false)
  - 是否需要管理员权限才能执行创建/更新/删除等管理操作。

- manage_wait_timeout_seconds: int (默认: 60)
  - 在等待用户发送文本（用于 create/update/avatar）时的超时时间（秒）。

- sync_nickname_on_switch / sync_avatar_on_switch
  - 与 QQ 同步相关设置，仅适配了NapCat
  - 删除人格时会尝试删除对应的头像缓存文件。



