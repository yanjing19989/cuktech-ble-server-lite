# CUKTECH MQTT/BLE Server 架构设计

本文描述 `mqtt_server` 当前实现的模块边界、运行时数据流和协议状态机，供后续维护、故障排查和功能扩展使用。该服务面向 CUKTECH 10 GaN Charger（MiOT 产品 ID `0x660e`），只负责本地 BLE 通信和 MQTT 桥接，不提供网页、HTTP、SSE、数据库、云登录或充电历史功能。

## 1. 设计目标和边界

### 目标

- 一个进程管理一个充电器 BLE 连接。
- 认证、加密、分帧和 MiOT 命令严格串行，避免设备状态机被并发请求破坏。
- BLE 断开后自动清理会话并重连。
- 端口状态实时通过 MQTT 推送给 Home Assistant。
- 插拔没有发送明确的空闲通知时，使用“仅活动端口验证 + 最终过期清零”避免残留读数。

### 非目标

- 不复用旧 `ble_server` 的代码。
- 不通过 BLE key 认证；认证只使用设备对应的 12 字节 MiOT token。
- 不在服务内保存历史数据或执行能量积分。
- 不提供控制台之外的网页或 REST API。

## 2. 目录和模块职责

```text
mqtt_server/
├── pyproject.toml                 # 打包元数据和运行依赖
├── config.yaml.example            # 最小配置模板
├── Dockerfile                     # GHCR 使用的镜像构建定义
├── docker-compose.yml             # host 网络 + D-Bus + 特权运行示例
├── src/cuktech_server/
│   ├── __main__.py                # python -m cuktech_server 入口
│   ├── main.py                    # 进程生命周期、信号处理
│   ├── config.py                  # YAML/环境变量解析和校验
│   ├── protocol.py                # UUID、PIID、帧、MiOT、端口解析
│   ├── crypto.py                  # HKDF/HMAC/AES-CCM 会话密码
│   ├── device.py                  # Bleak 设备会话、认证和命令传输
│   ├── service.py                 # 连接 actor、状态缓存、重连和校正
│   └── mqtt.py                    # Paho MQTT 线程与 asyncio 桥接
└── tests/test_core.py             # 纯协议/加密核心测试
```

模块依赖方向保持单向：

```text
main → mqtt → service → device → protocol/crypto
                    ↘ protocol
```

`protocol.py` 和 `crypto.py` 不应依赖 MQTT、Bleak 或 Home Assistant；这样可以在没有蓝牙和 Broker 的环境中运行纯逻辑测试。

## 3. 运行时组件和并发模型

服务由两个并发部分组成：

1. `ChargerService.run()` 在主 asyncio 事件循环中运行，是 BLE 的唯一 owner。连接、认证、主动 GET、SET、断开和重连均由该任务串行执行。
2. `MqttBridge` 使用 Paho 的网络线程维护 MQTT。Paho 回调只解析 JSON，不直接访问 Bleak；命令通过 `asyncio.run_coroutine_threadsafe()` 投递到主事件循环的有界队列。

因此 BLE 永远不会被 MQTT 回调线程并发调用。命令队列容量为 64，满载时丢弃新命令并记录 warning，避免 Broker 堵塞导致内存无限增长。

### 生命周期

```text
main.run
  ├─ load_config
  ├─ MqttBridge.start
  ├─ create ChargerService.run task
  ├─ 等待 SIGINT/SIGTERM
  └─ service.stop → BLE disconnect → mqtt stop
```

收到停止信号后，服务设置 `_stop`，唤醒可能正在等待的任务，停止连接循环并断开 BLE。进程退出前会停止 Paho loop 和 MQTT 连接。

## 4. 配置加载

`config.py:load_config()` 先读取 `CUKTECH_CONFIG_PATH` 指定的 YAML；未指定时使用 `mqtt_server/config.yaml`。环境变量覆盖对应 YAML 字段：

| 环境变量 | 覆盖字段 |
|---|---|
| `CUKTECH_DEVICE_MAC` | `ble.mac` |
| `CUKTECH_DEVICE_TOKEN` | `ble.token` |
| `MQTT_HOST` / `MQTT_PORT` | `mqtt.host` / `mqtt.port` |
| `MQTT_USERNAME` / `MQTT_PASSWORD` | MQTT 认证 |
| `MQTT_TOPIC_PREFIX` | MQTT 主题前缀 |

token 会去除空格并转换为 bytes，必须严格为 12 字节；MAC 支持冒号或短横线格式。配置错误在启动阶段抛出，不进入无效重连循环。

`ServerConfig` 关键参数：

- `reconnect_base` / `reconnect_max`：指数退避范围。
- `command_timeout`：BLE 通知、分帧和命令响应的基础超时。
- `protocol_refresh_interval`：PIID17/18 协议能力校正周期，默认 600 秒。
- `port_verify_interval`：活动端口主动 GET 间隔，默认 15 秒。
- `port_stale_timeout`：活动数据长期无更新时的最终清零时间，默认 45 秒。
- `settings_interval`：历史兼容字段；当前运行逻辑不再使用它进行全量设置刷新。

## 5. BLE GATT 和连接建立

设备服务 UUID 为 `0000fe95-0000-1000-8000-00805f9b34fb`。实现通过完整 UUID 订阅特征，不依赖不同后端可能变化的 ATT handle：

| UUID | 用途 | 队列名 |
|---|---|---|
| `0000001c...` | 设备信息 | `info` |
| `00000010...` | 认证控制 | `ctrl` |
| `00000019...` | 认证数据 | `auth` |
| `0000001a...` | 命令发送确认 | `send` |
| `0000001b...` | 命令接收/推送 | `recv` |
| `00000004...` | 固件版本读取 | 无通知队列 |

`ChargerClient.connect()` 的顺序：

1. 创建 `BleakClient` 并连接。
2. 为上述通知特征创建有界 asyncio 队列并订阅回调。
3. 写设备信息 `00` 和 `03`，读取协议版本、芯片名和固件版本。
4. 调用 `authenticate()` 完成登录。

通知回调只做两件事：记录 `last_rx`、将 bytes 放入对应队列。队列满时丢弃最旧的一项，保证 BLE 回调不阻塞。

## 6. 认证状态机

认证实现位于 `device.py:ChargerClient.authenticate()`，每次新会话开始前清空 `auth` 和 `ctrl` 队列。

### Phase A：初始化和密钥交换

```text
写 auth_ctrl: A4
读 auth_data: init_resp
回写 init_resp，byte[2] + 1
读 auth_data: key_data
回写 00 00 05 01 + F2 填充
```

key exchange 后等待 500ms，清理可能残留的延迟 key 帧，再订阅 `auth_ctrl`。这是为了适配当前设备在状态切换期间发送重复/延迟通知的行为。

### Phase B：登录和随机数交换

```text
写 auth_ctrl: 24 00 00 00
写 auth_data: 00 00 00 0B 01 00
等待 RCV_RDY = 00 00 01 01（跳过无关帧）
写 auth_data: 01 00 || app_rand(16B)
等待 RCV_OK = 00 00 01 00
```

设备随后通过内联帧或多帧返回 `dev_rand(16B) || dev_hmac(32B)`。`_recv_payload()` 会处理两种封装并验证帧数（1–100）及序号。

### HKDF 和 HMAC

```text
HKDF-SHA256(
  IKM   = token,
  salt  = app_rand || dev_rand,
  info  = "mible-login-info",
  L     = 64
)

derived[0:16]  = dev_key
derived[16:32] = app_key
derived[32:36] = dev_iv
derived[36:40] = app_iv
```

服务验证 `HMAC-SHA256(dev_key, dev_rand || app_rand)`；验证失败直接终止本次会话。随后发送 `HMAC-SHA256(app_key, app_rand || dev_rand)` 完成客户端确认。

### 可选第二轮 challenge

部分固件会在确认后发送 `0x0d` challenge 和 `0x0c` response。服务会发送内联 ACK，按设备要求回写第二轮响应；没有第二轮数据时直接等待 `auth_ctrl` 结果。最终只接受：

- `0x21`：Login OK。
- `0x11`：激活成功。

`0x23`、`0x12` 或未知结果均视为认证失败。

## 7. 加密和分帧

### AES-CCM

`crypto.py:SessionCipher` 分别维护发送和接收计数器：

```text
nonce = iv || 00 00 00 00 || uint32_le(counter)
wire  = uint16_le(counter & 0xffff) || AES-CCM(ciphertext + 4B tag)
```

发送使用 `app_key/app_iv`，接收使用 `dev_key/dev_iv`。接收端根据低 16 位和当前高位重建最接近的完整计数器；CCM tag 校验失败时不推进接收计数器。

每次断开都会丢弃 `SessionCipher`，新认证从计数器 0 开始。

### 通用分帧

- 内联：`00 00 02 type payload...`，回 `00 00 03 00`。
- 多帧头：`00 00 00 data_id count_lo count_hi`，回 `RCV_RDY`；随后接收 `[seq_lo seq_hi payload...]`，拼接并回 `RCV_OK`。

异常包括未知帧头、帧数为 0 或超过 100、序号不连续、通知超时。异常会抛出 `ProtocolError`，由上层销毁会话并重连。

## 8. MiOT 命令处理

### 构帧

`protocol.py:build_miot()` 构造单属性 GET/SET：

```text
[total_len][0x20][seq][00][opcode][count=1]
[siid=2][piid_lo][piid_hi][tl_lo][tl_hi][value]
```

值为 `<=0xff` 时使用 type 1、1 字节；更大值使用 type 5、UINT32 LE。GET 使用 1 字节零占位。sequence 从 1 开始 8 位回绕。

### 发送事务

`send_miot()` 保证一个请求的完整生命周期结束后才允许下一个请求：

1. AES-CCM 加密 plaintext。
2. 向 `cmd_send` 写单帧头。
3. 等待 `RCV_RDY`，写 `[01 00] + encrypted`。
4. 等待 `RCV_OK`。
5. 从 `cmd_recv` 读取响应、ACK、解密并按 sequence/SIID/PIID 匹配。

等待目标响应期间遇到端口推送，会立即解析并回调 `on_port`，不会吞掉实时遥测。

## 9. 端口数据模型和协议识别

端口 PIID 1–4 映射如下：

```text
1 → c1    2 → c2    3 → c3    4 → a
```

解密 MiOT payload 的最后 4 字节为：

```text
status / in_use | raw_protocol | current_raw | voltage_raw
```

`parse_port()` 输出统一 JSON 对象：

```json
{
  "active": true,
  "voltage": 18.3,
  "current": 2.3,
  "power": 42.09,
  "protocol": "PPS",
  "raw_protocol": 99
}
```

### 空闲判定

`status == 0` 时无条件输出零值和 `protocol="idle"`，即使电压/电流字节残留。活动状态要求 status 非零且电压或电流大于零。

### 协议判定优先级

1. PIID17/18 中已读取的硬件协议编号（1–10）。
2. 原始码本身是米家编号时直接映射：1/2=5V、3=QC、4=AFC、5=FCP、6=SCP、7=PD、8/9=PPS、10=UFCS。
3. C1/C2 遇到厂商 raw code 时，动态电压判定：非固定 5/9/12/15/20V 档位的 3–21V 电压视为 PPS；接近固定档位视为 PD。
4. C3 按混合口规则区分 5V/QC/PD；A 口按 5V 或 QC 区分。

未知协议仍保留 `raw_protocol`，便于新增型号规则时回溯抓包。

## 10. 状态缓存、推送和端口验证

`ChargerService` 保存：

- `ports`：四个端口最新 MQTT 对象。
- `settings`：PIID 字符串到整数的缓存。
- `_ports_to_verify`：当前 active 或曾经 active、尚未收到 idle 确认的端口集合。
- `_port_last_update`：每个端口最后一次收到/生成状态的 monotonic 时间。

### 连接成功后的读取

认证成功后按顺序 GET 一次 `SETTING_PIIDS`，读取设置并发布 retained `settings`。运行期间不再每 60 秒全量读取设置。

### 运行期间的读取

- `/set` 成功后直接更新本地设置缓存并重新发布。
- PIID16 端口控制写入前先 GET，修改目标 bit 后 SET，保留其它端口位。
- PIID21 协议开关由 HA 侧计算目标值后通过 `/set` 写入；维护该逻辑时必须保留未修改端口位和保留位。
- 仅按 `protocol_refresh_interval` 低频 GET PIID17/18，用于校正硬件协议码。
- 连接后持续轮询四个端口，每 `port_verify_interval` 验证一个端口；这样即使设备没有推送空闲端口，HA 也能获得明确的 idle 状态。
- `port_stale_timeout` 到期仍为 active 时发布零值，作为设备不发送拔出通知时的最终兜底。

## 11. 连接 actor 和重连策略

`ChargerService.run()` 是顶层状态机：

```text
enabled=false ──等待 /ble true──▶ enabled=true
      ▲                              │
      │                         connect + auth
      │                              │
      └──── stop /ble false ◀── session loop
                                     │异常/断开
                                     ▼
                           清密钥、清队列、端口归零
                                     │
                           指数退避后重新连接
```

每次 finally 都会：

1. 断开 `ChargerClient`。
2. 清空活动验证集合。
3. 四端口发布零值/idle。
4. 发布 retained `status.connected=false`。

自动重连等待时间从 `reconnect_base` 开始，每次失败翻倍，最高不超过 `reconnect_max`，并加入小幅随机抖动避免固定时间撞击设备或 Broker。

## 12. MQTT 接口和线程边界

默认主题前缀为 `cuktech/charger`。

### 发布主题

| 主题 | retain | 内容 |
|---|---:|---|
| `status` | 是 | `connected`、`authenticated`、设备型号、固件版本 |
| `settings` | 是 | 字符串 PIID 到整数的对象 |
| `port/c1`、`port/c2`、`port/c3`、`port/a` | 是 | 端口状态对象 |

MQTT status 配置 LWT：Broker 发现客户端异常断开时发布 `{"connected": false}`。

### 订阅主题

- `set`：`{"piid": 5, "value": 1}`。按 `VALUE_RANGES` 校验后进入 BLE 命令队列。
- `port`：`{"port": "c1", "action": "on"}`。只接受四个已知端口和 `on/off`。
- `ble`：`{"enabled": true}`。改变连接 actor 的运行意图。

Paho 回调中的 JSON 错误、未知字段、越界值和未知端口直接忽略并记录；不得在回调线程中调用 `BleakClient`。

## 13. Home Assistant 对接

`ha_integration/custom_components/cuktech_charger` 订阅相同主题：

- `status.connected` 驱动可用性和 BLE 连接实体。
- `port/{name}` 驱动电压、电流、功率、协议传感器。
- `settings` 驱动 select、switch、number 和协议开关实体。
- 控制实体通过 MQTT 发布 `set`、`port`、`ble`。

新服务不提供 HTTP，因此 HA 协调器不应恢复 `/api/status` 或 `/api/enable` 回退逻辑。`charge_event` 实体可以继续存在，但该服务不会产生充电历史或完成事件。

## 14. Docker、systemd 和 CI/CD

### Docker

镜像基于 `python:3.11-slim`，安装 `bluez` 和 `dbus`，通过 `pip install .` 安装包。运行容器通常需要：

- `network_mode: host`，使 MQTT 和本地 BLE 网络行为简单可预测。
- 挂载 `/var/run/dbus/system_bus_socket`。
- `privileged: true` 或等价的蓝牙设备权限。
- 将实际配置挂载到 `/data/config.yaml`，并设置 `CUKTECH_CONFIG_PATH`。

### GitHub Actions

`.github/workflows/mqtt-server.yml`：

1. Pull Request 安装 `mqtt_server` 并执行测试。
2. `main` 推送、`v*` tag 或手动触发时构建 Docker 镜像。
3. 使用 Buildx 构建 `linux/amd64` 和 `linux/arm64`。
4. 使用 `GITHUB_TOKEN` 登录 GHCR 并发布版本、latest、SHA 标签。

变更协议或 Docker 构建时，应同时检查测试 job 和 image metadata 的标签结果。

## 15. 故障排查手册

### 认证超时

将 `server.log_level` 设为 `DEBUG`，重点查看：

- `Starting BLE authentication`。
- `BLE notify auth:` 是否收到 init/key/RCV 帧。
- `BLE notify ctrl:` 是否收到 `0x21/0x11`。

常见原因是 token 错误、设备被其它客户端占用、通知订阅失败或设备状态机仍停留在上一次会话。服务会在异常后断开并重试，不应手动复用旧会话密钥。

### MQTT 无数据

先确认 Broker 可达，再检查 retained 主题：

```bash
mosquitto_sub -h <broker> -t 'cuktech/charger/#' -v
```

若 Paho 线程出现异常，先修复回调异常；MQTT 回调线程崩溃会同时影响所有控制命令。检查 `status` 是否为 connected，以及 HA 和 server 的 `topic_prefix` 是否一致。

### 拔出后仍有旧读数

确认端口曾收到 active 状态后是否进入 `_ports_to_verify`。若设备不发送 status=0，主动 GET 应在 `port_verify_interval` 内触发；最终由 `port_stale_timeout` 清零。若每次验证都失败，应检查命令响应超时和 BLE 链路质量。

### 协议显示 Unknown

记录 MQTT 载荷中的 `raw_protocol` 和电压。对 C1/C2，动态电压（如 18.3V）应为 PPS；固定档位应为 PD。若 PIID17/18 已提供硬件编号，应优先检查其编码是否符合当前型号观察。

## 16. 维护约定

- 任何 BLE 协议改动先更新 `protocol.py` / `crypto.py` 的纯函数测试，再改 `device.py` 时序。
- 不在 MQTT 回调线程中加入 BLE 调用。
- 不绕过 `send_miot()` 直接写命令通道；所有请求必须经过统一加密、分帧和响应匹配。
- 新增 PIID 时同时更新 `SETTING_PIIDS`、`VALUE_RANGES`、HA 对应实体和连接后的首次读取策略。
- 修改端口解析时必须保留 `status==0` 的零值语义和 `raw_protocol` 字段。
- 连接断开路径必须保持幂等：可重复调用 `disconnect()`，且不能遗留会话密钥、计数器或旧通知。
- 任何新建的后台任务都必须在 `stop()` 或会话 finally 中可取消，避免重连后任务叠加。
