# B站Cookie监控插件

AstrBot插件 - 定时检测B站Cookie有效性，失效时主动通知用户

## 功能

- 定时检测B站Cookie有效性
- Cookie失效时主动发送消息通知
- Cookie恢复时也会通知
- 支持扫码登录自动获取Cookie

## 配置

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `cookie` | string | "" | B站Cookie字符串（也可通过 `/bili_login` 扫码登录自动获取） |
| `check_interval` | int | 3600 | 检测间隔（秒），最小60秒 |
| `notify_user_id` | string | "" | 接收通知的用户ID |
| `notify_cooldown` | int | 3600 | 通知冷却时间（秒），最小60秒 |

## 指令

| 指令 | 说明 | 权限 |
|------|------|------|
| `/bili_check` | 手动检测Cookie状态 | 所有用户 |
| `/bili_status` | 查看监控状态 | 所有用户 |
| `/bili_login` | 扫码登录B站，自动获取Cookie | 所有用户 |

## License

MIT
