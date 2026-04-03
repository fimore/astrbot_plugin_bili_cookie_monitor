# B站Cookie监控插件

AstrBot插件 - 定时检测B站Cookie有效性，失效时主动通知用户

## 功能

- 定时检测B站Cookie有效性
- Cookie失效时主动发送消息通知
- Cookie恢复时也会通知
- 支持从文件读取Cookie

## 配置

- cookie: B站Cookie字符串
- check_interval: 检测间隔（秒），默认3600
- cookie_file: Cookie文件路径
- notify_user_id: 通知用户ID

## 指令

- /bili_check: 手动检测Cookie状态
- /bili_status: 查看监控状态

## License

MIT
