# CUKTECH MQTT/BLE Server

这是一个面向 CUKTECH 10 GaN Charger（MiOT 产品 ID `0x660e`）的精简 BLE 服务。服务通过本地 Bluetooth LE 连接充电器，完成 MiOT 认证和加密通信，再将状态发布到 MQTT，供 Home Assistant 使用。

本服务不包含网页、HTTP API、SSE、数据库、历史记录或云端登录功能。

## 运行要求

- Linux 主机
- Python 3.10 或更高版本
- 已启用的 Bluetooth LE 适配器和 BlueZ
- 可访问的 MQTT Broker（例如 Mosquitto）
- 充电器 MAC 地址和对应的 12 字节 MiOT token（24 个十六进制字符）

BLE 连接所在主机需要能够访问蓝牙适配器。使用 Docker 时通常需要 host 网络、D-Bus socket 和蓝牙设备权限。

## 安装

```bash
cd mqtt_server
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
cp config.yaml.example config.yaml
```

编辑 `config.yaml`，至少填写 `ble.mac`、`ble.token` 和 MQTT Broker 地址：

```yaml
ble:
  mac: "AA:BB:CC:DD:EE:FF"
  token: "00112233445566778899aabb"

mqtt:
  host: "192.168.1.10"
  port: 1883
  username: "mqtt_user"
  password: "mqtt_password"
  topic_prefix: "cuktech/charger"
```

token 必须正好是 12 字节。不要把 BLE key 当作 token，也不要在公开仓库中提交真实 token 或 MQTT 密码。

## 启动与停止

在虚拟环境中启动：

```bash
cd mqtt_server
source .venv/bin/activate
cuktech-server
```

也可以使用模块方式启动：

```bash
python -m cuktech_server.main
```

服务启动后会连接 MQTT，然后持续执行以下流程：扫描/连接设备、订阅通知、认证、读取设置、处理端口推送，并在断联后按指数退避自动重连。收到 `SIGINT` 或 `SIGTERM` 会主动断开并退出。

### 使用环境变量

环境变量优先级高于 YAML 中的对应字段：

| 环境变量 | 说明 |
|---|---|
| `CUKTECH_CONFIG_PATH` | 配置文件路径 |
| `CUKTECH_DEVICE_MAC` | BLE MAC 地址 |
| `CUKTECH_DEVICE_TOKEN` | 12 字节 MiOT token，十六进制 |
| `MQTT_HOST` | MQTT 主机名或 IP |
| `MQTT_PORT` | MQTT 端口 |
| `MQTT_USERNAME` | MQTT 用户名 |
| `MQTT_PASSWORD` | MQTT 密码 |
| `MQTT_TOPIC_PREFIX` | MQTT 主题前缀 |

例如：

```bash
CUKTECH_CONFIG_PATH=/etc/cuktech/config.yaml \
CUKTECH_DEVICE_TOKEN=00112233445566778899aabb \
cuktech-server
```

## 配置项

`server` 部分用于控制稳定性参数：

| 字段 | 默认值 | 说明 |
|---|---:|---|
| `reconnect_base` | `1.0` | 首次重连等待秒数 |
| `reconnect_max` | `300.0` | 最大重连等待秒数 |
| `command_timeout` | `10.0` | BLE 命令和通知等待超时 |
| `settings_interval` | `60.0` | 设置状态低频校正周期 |
| `log_level` | `INFO` | `DEBUG`、`INFO`、`WARNING` 或 `ERROR` |

## MQTT 接口

默认前缀为 `cuktech/charger`，端口和设置状态使用 retained 消息，状态主题配置了 MQTT Last Will：

| 主题 | 方向 | 载荷 |
|---|---|---|
| `cuktech/charger/status` | 发布 | `connected`、`authenticated`、`device_model`、`firmware_version` |
| `cuktech/charger/settings` | 发布 | 以字符串 PIID 为键的整数对象 |
| `cuktech/charger/port/c1` | 发布 | 端口状态对象 |
| `cuktech/charger/port/c2` | 发布 | 端口状态对象 |
| `cuktech/charger/port/c3` | 发布 | 端口状态对象 |
| `cuktech/charger/port/a` | 发布 | 端口状态对象 |
| `cuktech/charger/set` | 订阅 | `{"piid": 5, "value": 1}` |
| `cuktech/charger/port` | 订阅 | `{"port": "c1", "action": "on"}` |
| `cuktech/charger/ble` | 订阅 | `{"enabled": true}` |

端口状态示例：

```json
{
  "active": true,
  "voltage": 9.0,
  "current": 2.1,
  "power": 18.9,
  "protocol": "PD",
  "raw_protocol": 7
}
```

当设备报告端口状态字节为 0 时，服务会发布 `active=false`、电压/电流/功率均为 0、`protocol="idle"`，避免拔出设备后残留旧读数。未知协议编号会报告为 `Unknown`，并保留 `raw_protocol`。

### 支持的控制

- `/set`：PIID 5、6、8–13、15–21。值会按设备字段范围校验。
- `/port`：`c1`、`c2`、`c3`、`a` 的 `on`/`off`。服务会先读取 PIID 16，再只修改目标端口 bit。
- `/ble`：启用或暂停 BLE 连接任务。

同一时间只执行一个 BLE 命令。非法 JSON、未知 PIID、越界值和未知端口会被忽略并记录日志。

## Home Assistant 对接

仓库中的 `ha_integration/custom_components/cuktech_charger` 已使用上述 MQTT 主题。安装该自定义集成后，在 HA 中添加 **CUKTECH Charger** 配置条目，并确保 HA MQTT 集成已连接到同一个 Broker。

新 server 不提供 HTTP 接口，因此不需要填写或开放旧版 `ble_server` 的 HTTP 端口。HA 的可用性和 BLE 连接状态由 retained `status` 消息驱动。

## systemd 示例

创建 `/etc/systemd/system/cuktech-mqtt-server.service`：

```ini
[Unit]
Description=CUKTECH BLE MQTT Server
After=bluetooth.target network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=/opt/cuktech-ble-ha/mqtt_server
ExecStart=/opt/cuktech-ble-ha/mqtt_server/.venv/bin/cuktech-server
Environment=CUKTECH_CONFIG_PATH=/etc/cuktech/config.yaml
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

启用服务：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now cuktech-mqtt-server
sudo journalctl -u cuktech-mqtt-server -f
```

## 故障排查

- **认证失败**：确认 token 是该设备对应的 12 字节 MiOT token，而不是 BLE key；检查设备是否已被其它客户端占用。
- **找不到设备**：确认充电器通电、MAC 地址正确，并检查 `bluetoothctl` 是否能看到设备。
- **频繁重连**：使用 `log_level: DEBUG` 查看认证或命令超时原因；避免同一设备同时运行多个 BLE 客户端。
- **HA 显示旧数据**：确认 HA 与 server 使用相同的 `topic_prefix`，并检查 Broker 中是否收到 `status` retained 消息。
- **端口协议为 Unknown**：这是设备上报了未定义的 raw 协议编号；查看 `raw_protocol`，服务不会用电压猜测覆盖该值。

