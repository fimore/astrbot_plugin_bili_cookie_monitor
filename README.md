# B站Cookie监控插件

AstrBot插件 - 定时检测B站Cookie有效性，失效时主动通知用户

## 功能

- 定时检测B站Cookie有效性
- Cookie失效时主动发送消息通知
- Cookie恢复时也会通知
- 支持从文件读取Cookie

## 配置

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `cookie` | string | "" | B站Cookie字符串（二选一，`cookie_file`优先级更高） |
| `cookie_file` | string | "" | Cookie文件路径（优先级高于`cookie`） |
| `check_interval` | int | 3600 | 检测间隔（秒），最小60秒 |
| `notify_user_id` | string | "" | 接收通知的用户ID |
| `admin_whitelist` | array | [] | 管理员用户ID白名单，用于 `/bili_update` 指令 |
| `notify_cooldown` | int | 3600 | 通知冷却时间（秒），最小60秒 |
| `allowed_cookie_dirs` | array | [] | 允许读取Cookie文件的目录列表（留空则不做限制） |

## 指令

| 指令 | 说明 | 权限 |
|------|------|------|
| `/bili_check` | 手动检测Cookie状态 | 所有用户 |
| `/bili_status` | 查看监控状态 | 所有用户 |
| `/bili_update <文件路径>` | 更新Cookie文件路径（本次运行有效） | 仅管理员 |

## 安全说明

- Cookie文件路径必须包含"cookie"字样
- 仅支持 `.txt`、`.json`、`.cookie` 扩展名
- 可通过 `allowed_cookie_dirs` 限制允许读取的目录
- 敏感系统路径（如 passwd、shadow 等）会被拒绝访问

## License

MIT
