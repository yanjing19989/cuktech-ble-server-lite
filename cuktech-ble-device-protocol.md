# CUKTECH 充电器 BLE 对接参考

本文整理 CUKTECH 10 GaN Charger（产品 ID `0x660e`）中确认到的 BLE 交互，供独立客户端对接。这里区分设备协议与当前项目的实现策略：MQTT、HTTP、SSE、数据库、自动重连、会话统计、协议去抖和历史采样均不属于设备协议。

“协议事实”表示帧格式或字段含义；“设备特定观察”表示当前型号/抓包行为，不保证适用于其他 MiOT 设备。

## 1. 前置条件

- 设备 MAC 地址。
- 设备对应的 12 字节 MiOT token（通常为 24 个十六进制字符）。
- BLE key 可能出现在其他资料中，但本文认证和命令加密不使用它，不要用 BLE key 替代 token。
- BLE 客户端需支持 Write Without Response、Notify 和 Read。

需要编码到 MiOT 字段时，MAC 按字节逆序。例如 `AA:BB:CC:DD:EE:FF` 编为 `FF EE DD CC BB AA`；扫描设备时仍使用原始地址字符串。

## 2. GATT 服务和特征

服务 UUID：`0000fe95-0000-1000-8000-00805f9b34fb`。

| 短 UUID | 完整特征 UUID | 用途 | 操作 |
|---|---|---|---|
| `0x1c` | `0000001c-0000-1000-8000-00805f9b34fb` | 设备信息 | 写无响应、通知 |
| `0x10` | `00000010-0000-1000-8000-00805f9b34fb` | 认证控制 | 写无响应、通知 |
| `0x19` | `00000019-0000-1000-8000-00805f9b34fb` | 认证数据 | 写无响应、通知 |
| `0x1a` | `0000001a-0000-1000-8000-00805f9b34fb` | 命令发送 | 写无响应、通知 |
| `0x1b` | `0000001b-0000-1000-8000-00805f9b34fb` | 命令接收 | 写无响应、通知 |
| `0x04` | `00000004-0000-1000-8000-00805f9b34fb` | 固件版本 | 读 |

参考 ATT handle（不同后端可能变化，优先按 UUID 查找）：设备信息 `0x001f`、认证控制 `0x000d`、认证数据 `0x0010`、命令发送 `0x0019`、命令接收 `0x001c`、固件版本 `0x0008`。CCCD 应由 BLE 库自动配置，不要作为跨设备契约硬编码。

建议连接后先订阅命令接收、命令发送、设备信息、认证数据，再查询设备信息；认证控制通知可在密钥交换完成后、发送登录命令前订阅。每次重连都清空通知队列和计数器。

## 3. 设备信息（无需认证）

向设备信息特征写单字节命令并等待通知：

1. 写 `00`：`byte[1]` 为主协议版本，`byte[2]` 为子版本。
2. 写 `03`：`byte[1]` 为 ASCII 芯片名长度，从 `byte[2]` 读取该长度。
3. 读取固件版本特征：去除尾部 `0x00` 后按 ASCII 解码。

所有响应都应按实际长度解析。

## 4. 认证流程

所有认证写操作均为 Write Without Response。认证失败后应结束本次会话，断开并重新连接认证。

### 4.1 初始化和密钥交换

1. 认证控制写 `A4`。
2. 等待认证数据 `init_resp`。
3. 复制 `init_resp`，将 `byte[2]` 加 1 后写回认证数据。
4. 等待 `key_data`。当前设备通常使用 `byte[2] == 04` 且长度至少 20 字节。
5. **设备特定观察**：当前设备接受占位回写 `00 00 05 01 F2 F2 ...`，其中 `F2` 数量为 `len(key_data)-4`。这不是通用 MiOT 规范。

### 4.2 登录、密钥和 HMAC

1. 订阅认证控制；写 `24 00 00 00`。
2. 生成 16 字节随机数 `app_rand`。
3. 认证数据写 `00 00 00 0B 01 00`，等 `RCV_RDY = 00 00 01 01`。
4. 写 `01 00 || app_rand`，等 `RCV_OK = 00 00 01 00`。
5. 接收 `dev_rand`（取前 16 字节）和设备 HMAC（取前 32 字节）。认证数据支持第 5 节的内联或多帧封装。

HKDF-SHA256 参数：

```text
IKM  = 12-byte device token
salt = app_rand || dev_rand
info = ASCII "mible-login-info"
L    = 64
```

输出切分：`derived[0:16]=dev_key`、`derived[16:32]=app_key`、`derived[32:36]=dev_iv`、`derived[36:40]=app_iv`。

验证：`HMAC-SHA256(dev_key, dev_rand || app_rand)` 必须等于设备 HMAC。随后计算客户端 HMAC：`HMAC-SHA256(app_key, app_rand || dev_rand)`。

### 4.3 客户端确认和结果

1. 写认证数据 `00 00 00 0A 01 00`。
2. 等 `RCV_RDY`，写 `01 00 || app_hmac`（32 字节），等 `RCV_OK`。
3. 当前设备还可能发送第二轮挑战：`00 00 02 0D || 16B` 和 `00 00 02 0C || 32B`。分别 ACK `00 00 03 00`，再写 `00 00 00 0A 01 00`，收到 RCV_RDY 后回写 `01 00 0C || response[3:]`。这是抓包观察；挑战值在当前代码中未参与新的密码计算，不能当成通用认证算法。

最终等待认证控制通知：`0x21` Login OK，`0x11` 激活成功，`0x23` 登录失败，`0x12` 激活失败。只有 `0x21` 或 `0x11` 才能发送加密命令。

## 5. 通用分帧和确认

内联帧：`00 00 02 <type> <payload...>`，负载从 `byte[4]` 开始；回 ACK `00 00 03 00`。

多帧头：`00 00 00 <data_id> <count_lo> <count_hi>`，count 为小端 16 位帧数。回 `RCV_RDY = 00 00 01 01`，读取 count 个 `[seq_lo seq_hi payload...]`，拼接每帧 `byte[2:]`，最后回 `RCV_OK = 00 00 01 00`。客户端必须设置最大帧数和总超时；代码中的 100 帧是防护策略，不是设备声明的上限。

## 6. AES-CCM 命令加密

发送方向：

```text
counter = little-endian uint32(send_it)
nonce   = app_iv || 00 00 00 00 || counter
ct      = AES-CCM(app_key, tag_length=4).encrypt(nonce, plaintext, None)
wire    = counter[0:2] || ct
```

`send_it` 每次发送后递增，线上只发送低 16 位。接收方向输入为 `counter_low16_le || ciphertext+4B tag`，使用 `dev_key` 和 `dev_iv`，nonce 为 `dev_iv || 00 00 00 00 || reconstructed_counter`。设备只上传低 16 位，客户端须根据回绕重建高 16 位并校验 CCM 标签。每次新会话重新认证，计数器从零开始。

## 7. 命令传输

一个请求的完整响应处理结束前不要发送下一个请求：

1. 加密 plaintext。
2. 命令发送写 `00 00 00 00 01 00`。
3. 等 `00 00 01 01`。
4. 写 `01 00 || encrypted_wire`。
5. 等 `00 00 01 00`。
6. 从命令接收通道读取响应，按第 5 节 ACK/分帧。

## 8. MiOT plaintext

单属性命令布局（多字节小端）：

```text
[total_len][0x20][seq][00][opcode][count]
[siid][piid_lo][piid_hi][tl_lo][tl_hi][value...]
```

`opcode`：SET=`0x00`，GET=`0x02`；当前充电器服务 `siid=0x02`；count=`0x01`。`tl=(type_id<<12)|value_length`。值小于等于 `0xFF` 使用 type 1、1 字节；4 字节值使用 type 5、UINT32 LE；GET 携带 1 字节零占位值；`total_len=11+value_length`。sequence 从 1 开始按 8 位回绕。

响应中当前设备使用 `byte[4]` 表示结果类型，`byte[6]` 为 SIID，`byte[7]` 为 PIID：SET 通常先 `0x01` ACK 再 `0x04` Result；GET 为 `0x03` Result。应校验 sequence、SIID、PIID 和长度，不要把固定偏移或 ACK-only 容错当成设备规范。

## 9. SIID 2 属性

| PIID | 含义 | 值/备注 |
|---:|---|---|
| 1-4 | C1/C2/C3/USB-A 端口数据 | 端口负载，见第 10 节 |
| 5 | 场景模式 | 1 AI、2 数码生态、3 单口、4 均衡 |
| 6 | 息屏时间 | 1=5m、2=10m、3=30m、4=常亮、5=1m |
| 7 | 协议控制 | 型号相关 |
| 8 | 总倒计时 | 型号相关 |
| 9-12 | C1/C2/C3/A 倒计时 | 分钟 |
| 13 | 语言 | 0 English、1 中文 |
| 14 | 进入界面 | 写入，型号相关 |
| 15 | USB-A 小电流 | 0/1 |
| 16 | 端口控制 | bit0=C1、bit1=C2、bit2=C3、bit3=A |
| 17/18 | 协议信息和能力 | 原始 32 位值 |
| 19 | 空闲息屏 | 0/1 |
| 20 | 屏幕方向锁 | 0/1 |
| 21 | 协议扩展控制 | 原始 32 位值 |

修改 PIID 16 时建议先 GET，再只修改目标位。PIID 17/18 的普通业务语义尚未完全确认。

## 10. 端口数据和协议识别

解密后的 PIID 1-4 负载至少 12 字节，末 4 字节为：

```text
byte[-4] status / in_use
byte[-3] raw protocol code
byte[-2] current_raw (0.1 A)
byte[-1] voltage_raw (0.1 V)
```

`current=current_raw/10`，`voltage=voltage_raw/10`，`power=voltage*current`。状态字节为 0 时应报告 idle，即使电压/电流字节残留；活动至少要求状态非零且电压或电流大于零。

米家协议号的型号相关名称参考：1/2=5V，3=QC，4=AFC，5=FCP，6=SCP，7=PD，8/9=PPS，10=UFCS。raw code、电压启发式和端口类型不能视为跨型号硬件协议定义。

当前型号 PIID 17 通常以最高字节作为 C1 协议代码、`value >> 8` 的低字节作为 C2；PIID 18 同理对应 C3/A。只有已知协议号才可直接采用，否则应结合实测 PDO 能力。

PIID 21 位定义（当前型号观察）：C1 bit0=PD、bit1=PPS、bit2=UFCS、bit3 保留；C2 对应 bit8-11；C3 bit16=UFCS、bit17=SCP；A bit24=UFCS、bit25=SCP。写入时不要清除其他端口位，保留位和未列位以设备实测为准。

## 11. 对接边界和安全要求

- 认证、加密、分帧、MiOT TLV 和 SIID/PIID 是设备交互；应用层重连、限流、watchdog 和数据存储由对接方自行设计。
- 通知可能在命令响应等待期间到达，必须按帧类型及 SIID/PIID 分流，不能简单丢弃。
- 断开时停止通知、清空队列、丢弃会话密钥和计数器，重新认证后再发命令。
- 校验所有长度、帧计数、帧序号、CCM 标签和响应标识；异常数据应失败关闭本次操作。
