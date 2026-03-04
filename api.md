# API 文档

Base URL: `http://<host>:<port>`

---

## 1. 查询单房间缓存

### 请求

`GET /fans`

### Query 参数

- `room_id`：房间号（整数）
- `uid`：主播 UID（整数）

> 约束：`room_id` 与 `uid` **必须且只能传一个**。

### 成功响应

`200 OK`

```json
{
  "code": 0,
  "msg": "ok",
  "uid": 1048135385,
  "room_id": 1820703922,
  "medal": {
    "1048135385": 12,
    "123456789": 3
  }
}
```

说明：
- `uid` 为主播 UID（即房间所属主播）。
- `room_id` 为命中的房间号。
- `medal` 的 key 为粉丝 UID（JSON 输出为字符串），value 为粉丝牌等级。

### 失败响应

- `400 Bad Request`：参数同时为空或同时存在。

```json
{
  "detail": "room_id 和 uid 必须且只能传一个"
}
```

- `404 Not Found`：传入 `uid` 但未找到对应房间。

```json
{
  "detail": "未找到该 uid 对应的房间"
}
```

- `503 Service Unavailable`：目标房间缓存尚未初始化。

```json
{
  "detail": "该房间粉丝牌缓存尚未初始化，请稍后重试"
}
```

---

## 2. 查询用户在所有缓存中的粉丝牌情况

### 请求

`GET /search?uid=<粉丝uid>`

### Query 参数

- `uid`：粉丝 UID（整数，必填）

### 成功响应

`200 OK`

```json
{
  "code": 0,
  "msg": "ok",
  "uid": 123456,
  "medal": {
    "小礼猫": 22,
    "酷萨": 21
  }
}
```

说明：
- `medal` key 为 `rooms.json` 中配置的粉丝牌名称。
- 若目标 UID 在某些房间不存在粉丝牌，不会出现在 `medal` 中。
- 若目标 UID 在所有缓存中都不存在，`medal` 返回空对象 `{}`。

---

## 3. 轮询与缓存机制

- 服务启动后加载 `rooms.json`。
- 后台任务持续轮询所有房间：
  1. 按主播 UID 拉取分页粉丝牌数据；
  2. 更新对应 `room_id` 的内存缓存；
  3. 等待 `REQUEST_INTERVAL_SECONDS`（默认 3 秒）后处理下一个房间；
  4. 处理完全部房间后立即进入下一轮，不做分钟级额外等待。
